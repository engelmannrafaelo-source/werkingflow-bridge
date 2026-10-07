"""Real HTTP gate protocol with synthetic reports; never touches Docker/SSH."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.erkunder import deploy


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
    monkeypatch.setitem(deploy.PORTS, "leitstand", server.server_port)
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


@pytest.mark.parametrize("state", [404, 503, {"bereit": True, "berichte": ["x"]}])
def test_unavailable_legacy_or_invalid_response_fails(gate_server, state):
    states, requests = gate_server
    states.append(state)
    with pytest.raises((deploy.urllib.error.HTTPError, ValueError)):
        deploy.wait_until_idle(0)
    assert requests == ["POST", "DELETE"]
