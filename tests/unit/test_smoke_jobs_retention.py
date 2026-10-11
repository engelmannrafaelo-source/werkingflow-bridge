"""Deploy smoke: /v1/metrics/jobs-retention (BR11) is covered (BR11b).

BR11 added the route without a probe or EXCLUDED entry, so the coverage gate
(scripts/bridge_smoke_coverage.py) aborted every deploy of develop. These tests
pin that the gate is green again and that the probe judges shape, not counts.
"""

import importlib.util
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SCRIPTS = os.path.join(_REPO, "scripts")


def _load(name, filename):
    path = os.path.join(_SCRIPTS, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, _SCRIPTS)
smoke = _load("bridge_smoke", "bridge_smoke.py")
coverage = _load("bridge_smoke_coverage_under_test", "bridge_smoke_coverage.py")


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = str(body)
        self.headers = {}

    def json(self):
        return self._body


def _run(monkeypatch, status, body):
    seen = {}

    def fake_get(url, headers, timeout):
        seen["url"] = url
        return _Resp(status, body)

    monkeypatch.setattr(smoke.requests, "get", fake_get)
    probe = next(p for p in smoke.PROBES if p.name == "metrics_jobs_retention")
    r = probe.fn(smoke.Ctx(base_url="http://bridge.invalid", api_key="k"))
    return r, seen


def test_coverage_gate_has_no_gaps():
    routes = coverage.discover_routes()
    assert "/v1/metrics/jobs-retention" in routes
    assert sorted(routes - coverage.covered_endpoints()) == []


def test_probe_registered_for_hetzner_with_attributed_repro():
    p = next(p for p in smoke.PROBES if p.name == "metrics_jobs_retention")
    assert p.endpoint == "/v1/metrics/jobs-retention"
    assert "hetzner" in p.profiles
    assert "X-User-ID" in p.repro


def test_valid_shape_passes_even_with_lost_rows(monkeypatch):
    # Counts are per-worker STATE; a rollback would not bring rows back.
    r, seen = _run(monkeypatch, 200, {"since_epoch": 1.0,
                                      "row_missing_by_op": {"mark_done": 2},
                                      "retention": {}})
    assert r.ok
    assert seen["url"].endswith("/v1/metrics/jobs-retention")
    assert "mark_done" in r.detail


def test_http_error_fails(monkeypatch):
    r, _ = _run(monkeypatch, 403, {"detail": "forbidden"})
    assert not r.ok and "HTTP 403" in r.detail


def test_missing_key_fails(monkeypatch):
    r, _ = _run(monkeypatch, 200, {"since_epoch": 1.0, "retention": {}})
    assert not r.ok and "row_missing_by_op" in r.detail
