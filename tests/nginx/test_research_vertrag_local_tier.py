"""P6-VERTRAG: GET /v1/research/vertrag is answered by THIS bridge's workers.

Incident 06.11.2026 (dev deploy b81281b, rollback to 4a80030): the smoke probe
`research_vertrag` got `[Bridge worker-erk] Not Found` from the PROD bridge.
Cause: without an exact location the path matched
`location ~ ^/v1/(chat/completions|research)`, whose access phase runs the
operator account picker; a picked account on a non-local worker routes to
`bridge_policy_peer` (pool_router.lua), and X-Bridge-Hop does not prevent that.

Pinned here:
  * static: an exact `location = /v1/research/vertrag` exists, runs no Lua and
    proxies to a group that is local-only in BOTH generated topologies (fresh
    generator output and committed files) — no ${BRIDGE_BACKUP_HOST}, no backup;
  * real OpenResty (no network): the exact location wins over the real regex
    location header for the contract path with and without X-Bridge-Hop and
    with a query string, while /v1/research and deeper paths keep riding the
    regex location. Skipped without docker / the openresty image.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKER = REPO_ROOT / "docker"
GENERATOR = REPO_ROOT / "scripts" / "generate-bridge-upstreams.sh"
IMAGE = "openresty/openresty:1.27.1.1-alpine"
EP = "/v1/research/vertrag"
REGEX_HEAD = "location ~ ^/v1/(chat/completions|research)"


def _block(text: str, head: str) -> str:
    """Brace-balanced block starting at `head` (location bodies nest)."""
    start = text.find(head)
    assert start >= 0, f"missing: {head}"
    i = text.index("{", start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
    raise AssertionError(f"unbalanced: {head}")


def _route() -> str:
    return _block((DOCKER / "nginx.conf").read_text(), f"location = {EP} ")


def _upstream_name(route: str) -> str:
    targets = re.findall(r"proxy_pass\s+http://([A-Za-z0-9_]+)\s*;", route)
    assert len(targets) == 1, targets
    return targets[0]


def _generated(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ, UPSTREAMS_OUT_DIR=str(tmp_path))
    subprocess.run(["bash", str(GENERATOR), "all"], cwd=REPO_ROOT, env=env,
                   check=True, capture_output=True)
    return {t: (tmp_path / f"upstreams-{t}.conf").read_text() for t in ("primary", "prod")}


def test_exact_location_is_static_and_lua_free():
    route = _route()
    assert "lua" not in route
    assert "$" not in re.search(r"proxy_pass\s+([^;]+);", route).group(1)
    nginx = (DOCKER / "nginx.conf").read_text()
    assert REGEX_HEAD in nginx  # the location it must win against still exists


@pytest.mark.parametrize("source", ["committed", "generated"])
@pytest.mark.parametrize("topology", ["primary", "prod"])
def test_upstream_is_local_only_in_every_topology(tmp_path, source, topology):
    name = _upstream_name(_route())
    if source == "committed":
        include = (DOCKER / f"upstreams-{topology}.conf").read_text()
    else:
        include = _generated(tmp_path)[topology]
    up = _block(include, f"upstream {name} ")
    servers = re.findall(r"^\s*server\s+([^;]+);", up, re.M)
    assert servers, up
    assert "BRIDGE_BACKUP_HOST" not in up
    assert not any("backup" in s.split()[1:] for s in servers), up


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE],
                          capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker_ok(), reason="docker/openresty image not available")
@pytest.mark.parametrize("topology", ["primary", "prod"])
def test_openresty_exact_location_wins_over_account_picker(tmp_path, topology):
    route = _route()
    name = _upstream_name(route)
    up = _block((DOCKER / f"upstreams-{topology}.conf").read_text(), f"upstream {name} ")
    # Every repository target becomes the local stub; a cross-bridge entry
    # would become the peer stub (the static test already forbids it).
    up = re.sub(r"server\s+\$\{BRIDGE_BACKUP_HOST\}:\d+[^;]*;", "server 127.0.0.1:9002;", up)
    up = re.sub(r"server\s+(?!127\.0\.0\.1:9002)[^\s;]+([^;]*);", r"server 127.0.0.1:9001\1;", up)
    work = Path(tempfile.mkdtemp(dir=tmp_path))
    (work / "t.conf").write_text(f"""
events {{}}
http {{
  access_log off;
  map $http_x_none $bridge_origin_out {{ default ""; }}
  {up}
  server {{ listen 127.0.0.1:9001; location / {{ return 200 "local\\n"; }} }}
  server {{ listen 127.0.0.1:9002; location / {{ return 200 "peer\\n"; }} }}
  server {{
    listen 8080;
    {route}
    {REGEX_HEAD} {{ return 200 "picker\\n"; }}
    location @bridge_full {{ return 429 "full\\n"; }}
    location / {{ return 200 "default\\n"; }}
  }}
}}
""")
    cases = [
        ("", EP, "local"),
        ("1", EP, "local"),
        ("", EP + "?probe=1", "local"),
        ("", "/v1/research", "picker"),
        ("1", "/v1/research", "picker"),
        ("", EP + "/x", "picker"),
        ("", "/v1/research/async/abc", "picker"),
    ]
    script = ["mkdir -p /tmp/logs",
              "openresty -t -c /w/t.conf -p /tmp >/dev/null 2>&1 || { openresty -t -c /w/t.conf -p /tmp; exit 7; }",
              "openresty -c /w/t.conf -p /tmp", "sleep 0.3"]
    for hop, path, _ in cases:
        hdr = f"--header 'X-Bridge-Hop: {hop}'" if hop else ""
        script.append(f"wget -qO- {hdr} 'http://127.0.0.1:8080{path}' || echo ERR")
    r = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{work}:/w:ro",
                        IMAGE, "sh", "-c", "\n".join(script)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    got = r.stdout.strip().splitlines()
    assert len(got) == len(cases), r.stdout
    for (hop, path, want), line in zip(cases, got):
        assert line == want, (topology, hop, path, line)


# --- Real Lua: the mechanism of the incident, with and without the fix -------
# The real pool_router / bridge_account_selection / pool_pick modules run in
# the access phase of a stand-in for the chat/research regex location (same
# access_by_lua call and proxy_pass http://$affinity_upstream as nginx.conf).
# The policy stub releases ONE account on worker-erk — not in BRIDGE_WORKERS
# (worker1..4), i.e. a prod account seen from dev. That is the smoke's state:
# X-Bridge-Hop: 1 is sent, but only a real peer's X-Bridge-Policy-Hop limits the
# pick to local workers, so the request goes to bridge_policy_peer.
_POLICY = ('{"revision":1,"accounts":{"erk":{"worker":"worker-erk",'
           '"bridge_enabled":true,"bridge_priority":1,"bridge_policy_revision":1,'
           '"available":true,"cooldown_remaining_s":0,"effective_cap_tokens":100000,'
           '"current_in_flight_tokens":0,"usage_known":true,"weekly_percent":10}}}')


def _lua_conf(with_exact: bool) -> str:
    route = _route() if with_exact else ""
    locals_ = "\n".join(
        f"  upstream {n} {{ server 127.0.0.1:9001; }}"
        for n in ["claude_workers", "claude_version"] + [f"bridge_policy_worker{i}" for i in range(1, 5)])
    return f"""
env BRIDGE_WORKERS=worker1,worker2,worker3,worker4;
env BRIDGE_ACCOUNT_POLICY_URL=http://127.0.0.1:9003;
env BRIDGE_ACCOUNT_POLICY_TOKEN=probe;
env METRICS_READER_TARGET;
events {{}}
http {{
  access_log off;
  lua_package_path "/w/lua/?.lua;/w/lua-vendor/?.lua;;";
  lua_shared_dict pool_state 1m;
  map $http_x_none $bridge_origin_out {{ default ""; }}
  map $http_x_bridge_policy_hop $bridge_policy_hopped {{ default 0; }}
{locals_}
  upstream bridge_policy_peer {{ server 127.0.0.1:9002; }}
  server {{ listen 127.0.0.1:9001; location / {{ return 200 "local\\n"; }} }}
  server {{ listen 127.0.0.1:9002; location / {{ return 200 "peer\\n"; }} }}
  server {{ listen 127.0.0.1:9003; location = /runtime {{ return 200 '{_POLICY}'; }} }}
  server {{
    listen 8080;
    {route}
    {REGEX_HEAD} {{
      set $target_worker "pending";
      set $x_pool_decision "";
      set $pool_exhausted "";
      set $affinity_upstream claude_workers;
      set $x_cache_affinity "";
      access_by_lua_block {{
        require("pool_router").choose({{ overflow_capable = false }})
        if ngx.var.target_worker == "unavailable" then return ngx.exit(419) end
      }}
      proxy_pass http://$affinity_upstream;
    }}
    location @bridge_full {{ return 429 "full\\n"; }}
  }}
}}
"""


@pytest.mark.skipif(not _docker_ok(), reason="docker/openresty image not available")
@pytest.mark.parametrize("with_exact,want", [(False, "peer"), (True, "local")])
def test_real_account_picker_sends_contract_to_peer_without_exact_location(tmp_path, with_exact, want):
    work = Path(tempfile.mkdtemp(dir=tmp_path))
    (work / "t.conf").write_text(_lua_conf(with_exact))
    shutil.copytree(DOCKER / "lua", work / "lua")
    shutil.copytree(DOCKER / "lua-vendor", work / "lua-vendor")
    work.chmod(0o755)  # nginx workers run as nobody and must traverse /w
    script = ["mkdir -p /tmp/logs",
              "openresty -t -c /w/t.conf -p /tmp >/dev/null 2>&1 || { openresty -t -c /w/t.conf -p /tmp; exit 7; }",
              "openresty -c /w/t.conf -p /tmp", "sleep 0.3",
              f"wget -qO- --header 'X-Bridge-Hop: 1' 'http://127.0.0.1:8080{EP}' || echo ERR",
              "wget -qO- --header 'X-Bridge-Hop: 1' 'http://127.0.0.1:8080/v1/research' || echo ERR"]
    r = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{work}:/w:ro",
                        IMAGE, "sh", "-c", "\n".join(script)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    got = r.stdout.strip().splitlines()
    # /v1/research itself keeps the operator's pick (peer) in both variants:
    # the fix changes the contract GET only, never LLM routing.
    assert got == [want, "peer"], r.stdout + r.stderr[-1500:]
