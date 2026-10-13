"""Version delivery: repository nginx stanza against loopback-only stubs."""
import ipaddress
import json
from pathlib import Path
import re
import socket
import subprocess
import time
from urllib.request import build_opener, ProxyHandler
from urllib.error import HTTPError, URLError
import pytest
from scripts import bridge_smoke as smoke
import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from scripts import bridge_smoke_coverage as coverage

ROOT = Path(__file__).parents[1]


def block(text, head):
    match = re.search(re.escape(head) + r"\s*\{([^{}]*)\}", text)
    assert match, head
    return match.group(0)


@pytest.mark.parametrize("topology", ["primary", "prod"])
@pytest.mark.parametrize("healthy", [False, True])
def test_version_nginx_preserves_worker_response(tmp_path, topology, healthy):
    route = block((ROOT / "docker/nginx.conf").read_text(), "location = /v1/version")
    upstream = block((ROOT / f"docker/upstreams-{topology}.conf").read_text(), "upstream claude_version")
    assert "backup" not in upstream
    assert "proxy_next_upstream off;" in route
    assert "proxy_intercept_errors off;" in route
    assert "error_page" not in route
    assert re.findall(r"proxy_pass\s+([^;]+);", route) == ["http://claude_version"]
    # Replace repository topology addresses with local stubs before nginx sees it.
    sockets = [socket.socket() for _ in range(3)]
    for sock in sockets:
        sock.bind(("127.0.0.1", 0))
    front, bad, good = [sock.getsockname()[1] for sock in sockets]
    count = 0
    def local_server(match):
        nonlocal count
        count += 1
        port = good if healthy and count % 2 == 0 else bad
        return f"server 127.0.0.1:{port} max_fails=0;"
    upstream = re.sub(r"server [^;]+;", local_server, upstream)
    bad_body = json.dumps({"error": {"message": "Build metadata unavailable", "retryable": False}})
    good_body = json.dumps({"git_sha": "b" * 40})
    config = f"""daemon off;
master_process off;
pid {tmp_path}/nginx.pid;
error_log {tmp_path}/error.log;
events {{}}
http {{
access_log off;
{upstream}
server {{ listen 127.0.0.1:{bad}; location / {{ return 503 '{bad_body}'; }} }}
server {{ listen 127.0.0.1:{good}; location / {{ return 200 '{good_body}'; }} }}
server {{ listen 127.0.0.1:{front};
proxy_next_upstream error timeout http_503;
proxy_intercept_errors on;
error_page 503 = @bridge_full;
location @bridge_full {{ return 429 'capacity'; }}
{route}
}}
}}
"""
    # Fail closed before starting a process or making a request.
    for address in re.findall(r"(?:server|listen)\s+([^;\s{}]+)", config):
        assert ipaddress.ip_address(address.rsplit(":", 1)[0]).is_loopback, "Non-stub address"
    assert "${" not in config
    conf = tmp_path / "nginx.conf"
    conf.write_text(config)
    for sock in sockets:
        sock.close()
    opener = build_opener(ProxyHandler({}))
    process = subprocess.Popen(["/usr/sbin/nginx", "-p", str(tmp_path), "-c", str(conf)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        results = []
        for attempt in range(50):
            if process.poll() is not None:
                pytest.fail(process.stderr.read().decode())
            try:
                with opener.open(f"http://127.0.0.1:{front}/v1/version", timeout=1) as response:
                    results.append((response.status, json.load(response)))
            except HTTPError as response:
                results.append((response.code, json.load(response)))
            except URLError:
                time.sleep(0.02)
            if len(results) == 8:
                break
        assert len(results) == 8
        assert any(code == 503 for code, _ in results)
        assert all(body == json.loads(bad_body) for code, body in results if code == 503)
        assert {code for code, _ in results} == ({200, 503} if healthy else {503})
    finally:
        process.terminate()
        process.communicate(timeout=5)


@pytest.mark.parametrize("actual,status,expected,ok", [
    ("a" * 40, 200, "a" * 40, True),
    ("b" * 40, 200, "a" * 40, False),
    (None, 503, "a" * 40, False),
    ("a" * 40, 200, "", False),
])
def test_version_smoke_checks_deployed_sha(monkeypatch, actual, status, expected, ok):
    from types import SimpleNamespace
    calls = []
    def get(url, **kwargs):
        assert url == "http://stub.invalid/v1/version"
        calls.append(url)
        return SimpleNamespace(status_code=status, json=lambda: {"git_sha": actual})
    monkeypatch.setattr(smoke.requests, "get", get)
    ctx = smoke.Ctx("http://stub.invalid", "unused")
    ctx.expected_git_sha = expected
    probe = next(p for p in smoke.PROBES if p.name == "version")
    result = probe.fn(ctx)
    # Without named workers even all-agreeing samples are no proof per worker:
    # their own state (version_reason), never ok (BRUMBAU2R P2-4).
    assert result.ok is False
    assert (result.version_reason is not None) is ok
    # Every sample is asked when all agree; the first wrong answer ends it.
    assert len(calls) == (smoke.VERSION_SAMPLES if ok else int(bool(expected)))


def _version_answers(monkeypatch, answers):
    from types import SimpleNamespace
    seq = iter(answers)
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        answer = next(seq)
        if isinstance(answer, Exception):
            raise answer
        status, sha = answer
        return SimpleNamespace(status_code=status, json=lambda: {"git_sha": sha})

    monkeypatch.setattr(smoke.requests, "get", get)
    monkeypatch.setattr(smoke, "resolve_api_key", lambda: "")
    monkeypatch.setattr(smoke.time, "sleep", lambda *_: None)
    return calls


@pytest.mark.parametrize("stale_at", [0, 1, 3, 7])
def test_version_one_stale_worker_fails_wherever_it_answers(monkeypatch, stale_at):
    """BRUMBAU1R P2a: one worker of the pool on an old image is a failure, even
    when other samples (or later attempts) hit up-to-date workers."""
    answers = [(200, "a" * 40)] * smoke.VERSION_SAMPLES * 3
    answers[stale_at] = (200, "d" * 40)
    calls = _version_answers(monkeypatch, answers)
    res = smoke.run("http://stub.invalid", "hetzner", "version", {}, 3,
                    expected_git_sha="a" * 40)
    assert res[0].ok is False
    assert "mismatch" in res[0].detail and "d" * 40 in res[0].detail
    assert len(calls) == stale_at + 1  # no further attempt after a worker's own answer


def test_version_transport_error_is_asked_again(monkeypatch):
    import requests
    answers = [requests.ConnectionError("reset")] + [(200, "a" * 40)] * smoke.VERSION_SAMPLES
    _version_answers(monkeypatch, answers)
    res = smoke.run("http://stub.invalid", "hetzner", "version", {}, 1,
                    expected_git_sha="a" * 40)
    assert res[0].version_reason and "no answer" not in res[0].detail


def test_version_lasting_transport_error_fails(monkeypatch):
    import requests
    _version_answers(monkeypatch, [requests.ConnectionError("down")] * 10)
    res = smoke.run("http://stub.invalid", "hetzner", "version", {}, 3,
                    expected_git_sha="a" * 40)
    assert res[0].ok is False and "no answer" in res[0].detail


def test_coverage_includes_version_router():
    assert "/v1/version" in coverage.discover_routes()
    assert "/v1/version" in coverage.covered_endpoints()


@pytest.mark.parametrize("profile", ["hetzner", "server2"])
def test_public_version_run_needs_no_credentials(monkeypatch, profile):
    from types import SimpleNamespace
    def forbidden():
        pytest.fail("Public version must not resolve credentials")
    monkeypatch.setattr(smoke, "resolve_api_key", forbidden)
    def get(url, **kwargs):
        assert url == "http://stub.invalid/v1/version"
        assert kwargs["allow_redirects"] is False
        assert "headers" not in kwargs
        return SimpleNamespace(status_code=200, json=lambda: {"git_sha": "a" * 40})
    monkeypatch.setattr(smoke.requests, "get", get)
    result = smoke.run("http://stub.invalid", profile, "version", {}, 1, expected_git_sha="a" * 40)
    assert len(result) == 1 and result[0].version_reason


def test_generator_version_pool_has_no_backup(tmp_path):
    import os
    result = subprocess.run(["bash", str(ROOT / "scripts/generate-bridge-upstreams.sh"), "all"],
                            env={**os.environ, "UPSTREAMS_OUT_DIR": str(tmp_path)},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    for topology in ("primary", "prod"):
        generated = (tmp_path / f"upstreams-{topology}.conf").read_text()
        upstream = block(generated, "upstream claude_version")
        assert "backup" not in upstream
        assert "BRIDGE_BACKUP_HOST" not in upstream
        assert generated == (ROOT / f"docker/upstreams-{topology}.conf").read_text()


WORKERS = ("worker1", "worker2", "worker3", "worker4")


def _route_answers(monkeypatch, by_path):
    """GET answers by path; records every path asked."""
    from types import SimpleNamespace
    asked = []

    def get(url, **kwargs):
        path = url.removeprefix("http://stub.invalid")
        asked.append(path)
        status, sha = by_path.get(path, by_path["default"])
        return SimpleNamespace(status_code=status, json=lambda: {"git_sha": sha})

    monkeypatch.setattr(smoke.requests, "get", get)
    monkeypatch.setattr(smoke, "resolve_api_key", lambda: "")
    monkeypatch.setattr(smoke.time, "sleep", lambda *_: None)
    return asked


def test_version_proves_each_worker_through_its_direct_route(monkeypatch):
    asked = _route_answers(monkeypatch, {"default": (200, "a" * 40)})
    res = smoke.run("http://stub.invalid", "hetzner", "version", {}, 3,
                    expected_git_sha="a" * 40, version_workers=WORKERS)
    assert res[0].ok is True, res[0].detail
    assert asked == [f"/{w}/v1/version" for w in WORKERS] + ["/v1/version"]


@pytest.mark.parametrize("stale", WORKERS)
def test_version_stale_worker_fails_whatever_the_pool_says(monkeypatch, stale):
    """BRUMBAU1R P2 (step 2): with nginx worker_processes > 1 the pool order is
    per process — pool samples may all hit current workers. The direct route
    still finds the stale one."""
    asked = _route_answers(monkeypatch, {"default": (200, "a" * 40),
                                         f"/{stale}/v1/version": (200, "d" * 40)})
    res = smoke.run("http://stub.invalid", "hetzner", "version", {}, 3,
                    expected_git_sha="a" * 40, version_workers=WORKERS)
    assert res[0].ok is False
    assert stale in res[0].detail and "d" * 40 in res[0].detail
    assert asked[-1] == f"/{stale}/v1/version"


def test_version_pool_route_still_has_to_answer(monkeypatch):
    _route_answers(monkeypatch, {"default": (200, "a" * 40), "/v1/version": (503, None)})
    res = smoke.run("http://stub.invalid", "hetzner", "version", {}, 1,
                    expected_git_sha="a" * 40, version_workers=WORKERS)
    assert res[0].ok is False and "pool sample" in res[0].detail


def test_version_refuses_a_worker_name_outside_the_direct_route(monkeypatch):
    asked = _route_answers(monkeypatch, {"default": (200, "a" * 40)})
    res = smoke.run("http://stub.invalid", "hetzner", "version", {}, 1,
                    expected_git_sha="a" * 40, version_workers=("../admin",))
    assert res[0].ok is False and asked == []


def test_version_without_workers_is_its_own_state_not_a_pass(monkeypatch):
    """BRUMBAU2R P2-4: pool samples prove nothing per worker — not ok, not a
    code failure, its own UNPROVEN state."""
    _route_answers(monkeypatch, {"default": (200, "a" * 40)})
    res = smoke.run("http://stub.invalid", "hetzner", "version", {}, 1, expected_git_sha="a" * 40)
    assert res[0].ok is False and "no proof per worker" in res[0].detail
    assert res[0].version_reason
    passed, gaps, failures = smoke.partition_results(res)
    assert gaps == res and not passed and not failures


def test_version_without_workers_never_prints_smoke_ok(monkeypatch, capsys):
    _route_answers(monkeypatch, {"default": (200, "a" * 40)})
    import sys
    monkeypatch.setattr(sys, "argv", ["smoke", "--base-url", "http://stub.invalid",
                                   "--only", "version", "--expected-git-sha", "a" * 40])
    with pytest.raises(SystemExit):
        smoke.main()
    out = capsys.readouterr()
    assert "SMOKE_OK" not in out.out
    assert "SMOKE_VERSION_PER_WORKER_UNPROVEN: no proof per worker" in out.err
    assert "[VERS]" in out.out


def test_version_stale_pool_without_workers_is_a_failure(monkeypatch):
    _route_answers(monkeypatch, {"default": (200, "d" * 40)})
    res = smoke.run("http://stub.invalid", "hetzner", "version", {}, 1, expected_git_sha="a" * 40)
    assert res[0].ok is False and not res[0].version_reason
    assert smoke.partition_results(res)[2] == res


def test_version_workers_reach_the_probe_from_the_command_line(monkeypatch):
    seen = {}

    def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return [smoke.ProbeResult("version", "/v1/version", True, "stub")]

    monkeypatch.setattr(smoke, "run", fake_run)
    import sys
    monkeypatch.setattr(sys, "argv", ["smoke", "--base-url", "http://stub.invalid", "--only", "version",
                                   "--expected-git-sha", "a" * 40,
                                   "--version-workers", "worker1,worker2,worker3,worker4"])
    with pytest.raises(SystemExit):
        smoke.main()
    assert seen["version_workers"] == WORKERS


def test_direct_worker_route_exists_in_nginx():
    """The proof per worker relies on this route; it must stay."""
    conf = (Path(__file__).parents[1] / "docker/nginx.conf").read_text()
    assert "location ~ ^/(worker[a-zA-Z0-9_-]+)/(.*)$ {" in conf
