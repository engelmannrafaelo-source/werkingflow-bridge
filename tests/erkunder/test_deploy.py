"""Real HTTP gate protocol with synthetic reports; never touches Docker/SSH."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.erkunder import deploy
from src.erkunder.gesundheit import PORTS


@pytest.fixture
def gate_server(monkeypatch):
    states = []
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append("POST")
            assert self.path == "/deploy/pruefen"
            assert self.headers["X-Erkunder-Intern"] == "synthetic"
            state = states.pop(0) if len(states) > 1 else states[0]
            self.send_response(state if isinstance(state, int) else 200)
            self.end_headers()
            self.wfile.write(json.dumps(state).encode())

        def do_DELETE(self):
            requests.append("DELETE")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"freigegeben": true}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("localhost", 0), Handler)
    monkeypatch.setitem(PORTS, "leitstand", server.server_port)
    monkeypatch.setenv("ERKUNDER_INTERNAL_TOKEN", "synthetic")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield states, requests
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_running_report_finishes_before_deploy(gate_server, capsys):
    states, requests = gate_server
    states.extend(
        [
            {"bereit": False, "berichte": ["bericht-123"]},
            {"bereit": True, "berichte": []},
        ]
    )
    deploy.wait_until_idle(2, poll_s=0)
    assert requests == ["POST", "POST"]
    assert "bericht-123" in capsys.readouterr().out


def test_running_report_deadline_releases_and_fails(gate_server):
    states, requests = gate_server
    states.append({"bereit": False, "berichte": ["bericht-123"]})
    with pytest.raises(TimeoutError, match="nothing stopped"):
        deploy.wait_until_idle(0)
    assert requests == ["POST", "DELETE"]


def test_idle_needs_no_wait(gate_server):
    states, requests = gate_server
    states.append({"bereit": True, "berichte": []})
    deploy.wait_until_idle(0)
    assert requests == ["POST"]


@pytest.mark.parametrize("state", [403, 503, {"bereit": True, "berichte": ["x"]}])
def test_unavailable_legacy_or_invalid_response_fails(gate_server, state):
    states, requests = gate_server
    states.append(state)
    with pytest.raises((deploy.urllib.error.HTTPError, ValueError)):
        deploy.wait_until_idle(0)
    assert requests == ["POST", "DELETE"]


def test_legacy_404_has_distinct_exit_without_release(gate_server):
    states, requests = gate_server
    states.append(404)
    with pytest.raises(
        deploy.LegacyProtocolMissing, match="ERKUNDER_DEPLOY_EINFUEHRUNG=1"
    ):
        deploy.wait_until_idle(0)
    assert requests == ["POST"]


def test_legacy_idle_requires_existing_empty_directory(tmp_path):
    deploy.assert_legacy_idle(tmp_path)
    (tmp_path / "report").mkdir()
    with pytest.raises(RuntimeError, match="not empty"):
        deploy.assert_legacy_idle(tmp_path)
    with pytest.raises(FileNotFoundError):
        deploy.assert_legacy_idle(tmp_path / "missing")


@pytest.mark.parametrize(
    "state, exit_code",
    [(404, 3), (403, 1), (503, 1), ({"bereit": True, "berichte": []}, 0)],
)
def test_stdin_probe_works_without_new_modules(gate_server, tmp_path, state, exit_code):
    import os
    import subprocess
    import sys
    from pathlib import Path

    states, _ = gate_server
    states.append(state)
    result = subprocess.run(
        [sys.executable, "-", "0", str(PORTS["leitstand"])],
        input=Path(deploy.__file__).read_text(),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": ""},
        timeout=10,
    )
    assert result.returncode == exit_code, result.stderr
    if exit_code == 3:
        assert "ERKUNDER_DEPLOY_EINFUEHRUNG=1" in result.stderr
