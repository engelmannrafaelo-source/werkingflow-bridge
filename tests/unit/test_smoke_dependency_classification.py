"""
Unit tests for the deploy smoke's dependency classification and repro
attribution (scripts/bridge_smoke.py).

Guards the 2026-08-01 incident from the reporting side: the privacy service
that document_convert / smart_anonymize / convert_html_to_* proxy to became
unreachable for ~23 minutes, and the smoke reported those four probes as
"probes failed", which rolled a good image back. Rolling back cannot fix an
unreachable dependency — the previous image proxies to the same
PRIVACY_SERVICE_URL and fails identically. Proven that day: the endpoints
recovered ~30s BEFORE the rollback started, on the condemned image.

The classification must stay honest in BOTH directions, so these tests pin
down the refusals too: an unproven dependency, a dependency reporting itself
healthy, or a failing check must all leave the failures HARD.
"""

import importlib.util
import os
import sys

import pytest

_SMOKE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts", "bridge_smoke.py",
)
_spec = importlib.util.spec_from_file_location("bridge_smoke_under_test", _SMOKE_PATH)
smoke = importlib.util.module_from_spec(_spec)
sys.modules["bridge_smoke_under_test"] = smoke
_spec.loader.exec_module(smoke)


class _Resp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status_code=200, payload=None, raise_on_json=False):
        self.status_code = status_code
        self._payload = payload
        self._raise = raise_on_json

    def json(self):
        if self._raise:
            raise ValueError("not json")
        return self._payload


def _ctx():
    return smoke.Ctx(base_url="http://bridge.invalid", api_key="k")


def _fail(name, endpoint="/v1/x", detail="HTTP 503: capacity"):
    return smoke.ProbeResult(name, endpoint, False, detail, 503, 134000)


# ---------------------------------------------------------------------------
# privacy_dependency_down — only a PROVEN outage may excuse anything
# ---------------------------------------------------------------------------
class TestPrivacyDependencyDown:
    def test_detects_unreachable_dependency(self, monkeypatch):
        monkeypatch.setattr(smoke.requests, "get", lambda *a, **k: _Resp(
            200, {"privacy": {"available": False,
                              "error": "All connection attempts failed"}}))
        reason = smoke.privacy_dependency_down(_ctx())
        assert reason is not None
        assert "unreachable" in reason
        assert "All connection attempts failed" in reason

    def test_healthy_dependency_is_not_an_excuse(self, monkeypatch):
        """If the dependency is up but the probes still fail, that IS the image."""
        monkeypatch.setattr(smoke.requests, "get", lambda *a, **k: _Resp(
            200, {"privacy": {"available": True, "enabled": True}}))
        assert smoke.privacy_dependency_down(_ctx()) is None

    def test_missing_available_field_is_not_an_excuse(self, monkeypatch):
        monkeypatch.setattr(smoke.requests, "get", lambda *a, **k: _Resp(
            200, {"privacy": {"enabled": True}}))
        assert smoke.privacy_dependency_down(_ctx()) is None

    def test_not_ready_but_reachable_is_not_an_excuse(self, monkeypatch):
        """available:false WITHOUT an error string means the service answered
        and reported itself not-ready — it is reachable, so a failing convert
        is not explained by unreachability and must stay a hard failure."""
        monkeypatch.setattr(smoke.requests, "get", lambda *a, **k: _Resp(
            200, {"privacy": {"enabled": False, "available": False}}))
        assert smoke.privacy_dependency_down(_ctx()) is None

    def test_non_200_status_is_not_an_excuse(self, monkeypatch):
        monkeypatch.setattr(smoke.requests, "get", lambda *a, **k: _Resp(500, None))
        assert smoke.privacy_dependency_down(_ctx()) is None

    def test_non_json_body_is_not_an_excuse(self, monkeypatch):
        monkeypatch.setattr(smoke.requests, "get",
                            lambda *a, **k: _Resp(200, None, raise_on_json=True))
        assert smoke.privacy_dependency_down(_ctx()) is None

    def test_check_itself_failing_is_not_an_excuse(self, monkeypatch):
        """A broken check proves nothing about the dependency — stay hard."""
        def _boom(*a, **k):
            raise OSError("connection refused")
        monkeypatch.setattr(smoke.requests, "get", _boom)
        assert smoke.privacy_dependency_down(_ctx()) is None


# ---------------------------------------------------------------------------
# classify_dependency_failures — scope and gating
# ---------------------------------------------------------------------------
class TestClassifyDependencyFailures:
    def test_marks_only_privacy_dependent_probes(self, monkeypatch):
        monkeypatch.setattr(smoke.requests, "get", lambda *a, **k: _Resp(
            200, {"privacy": {"available": False, "error": "unreachable"}}))
        results = [
            _fail("convert_html_to_pdf"),
            _fail("document_convert"),
            _fail("chat_completions"),   # NOT privacy-dependent
        ]
        smoke.classify_dependency_failures(results, _ctx())
        assert results[0].dependency_reason
        assert results[1].dependency_reason
        assert results[2].dependency_reason is None, (
            "a non-privacy probe must never be excused by a privacy outage"
        )

    def test_no_check_when_no_privacy_probe_failed(self, monkeypatch):
        """A healthy deploy must not pay for the extra call."""
        calls = []
        monkeypatch.setattr(smoke.requests, "get",
                            lambda *a, **k: calls.append(1) or _Resp(200, {}))
        results = [_fail("chat_completions"), smoke.ProbeResult("research", "/v1/research", True, "ok")]
        smoke.classify_dependency_failures(results, _ctx())
        assert calls == []

    def test_capacity_refusals_are_left_alone(self, monkeypatch):
        """Capacity already has its own classification; don't double-label."""
        monkeypatch.setattr(smoke.requests, "get", lambda *a, **k: _Resp(
            200, {"privacy": {"available": False, "error": "unreachable"}}))
        r = _fail("smart_anonymize")
        r.capacity_reason = "pool_exhausted"
        smoke.classify_dependency_failures([r], _ctx())
        assert r.dependency_reason is None


# ---------------------------------------------------------------------------
# partition_results — a dependency outage must not block the deploy
# ---------------------------------------------------------------------------
class TestPartitionResults:
    def _passing_pool_probes(self):
        return [
            smoke.ProbeResult("research", "/v1/research", True, "ok"),
            smoke.ProbeResult("chat_completions", "/v1/chat/completions", True, "ok"),
        ]

    def test_dependency_gaps_do_not_become_failures(self):
        dep = _fail("convert_html_to_pdf")
        dep.dependency_reason = "privacy-service unreachable (ConnectError)"
        passed, gaps, failures = smoke.partition_results(self._passing_pool_probes() + [dep])
        assert failures == [], "a dependency outage must not roll the deploy back"
        assert dep in gaps

    def test_dependency_gap_survives_even_without_a_healthy_pool(self):
        """The pool guard is about CAPACITY refusals; a proven-down dependency
        is independent evidence and must not be re-hardened by it."""
        dep = _fail("document_convert")
        dep.dependency_reason = "privacy-service unreachable"
        cap = _fail("chat_completions")
        cap.capacity_reason = "pool_exhausted"
        passed, gaps, failures = smoke.partition_results([dep, cap])
        assert dep in gaps
        assert cap in failures, "capacity without a healthy pool probe stays hard"

    def test_real_regression_still_fails(self):
        """The whole point of the gate: an unexplained failure still blocks."""
        bad = _fail("convert_html_to_pdf", detail="200 but no PDF magic")
        passed, gaps, failures = smoke.partition_results(self._passing_pool_probes() + [bad])
        assert bad in failures
        assert gaps == []


# ---------------------------------------------------------------------------
# Repro attribution — printed repros must not reproduce a phantom 400
# ---------------------------------------------------------------------------
# AttributionEnforcementMiddleware rejects a POST to any ENFORCED_PATHS route
# that carries Authorization but no X-User-ID. On 2026-08-01 the four failing
# probes printed repros without it, so the first command the operator ran
# returned 400 missing_user_attribution instead of the failure being
# investigated — the exact trap a comment in the file already warned about.
class TestReproAttribution:
    def test_every_registered_probe_repro_carries_attribution(self):
        missing = [p.name for p in smoke.PROBES
                   if p.repro and "X-User-ID" not in p.repro]
        assert not missing, (
            "these probes print a repro that reproduces a phantom 400 instead "
            f"of the real failure: {missing}"
        )

    def test_injection_is_idempotent(self):
        once = smoke.with_attribution("curl -XPOST $AI_BRIDGE_URL/v1/document/convert")
        twice = smoke.with_attribution(once)
        assert once == twice
        assert once.count("X-User-ID") == 1

    def test_empty_repro_stays_empty(self):
        assert smoke.with_attribution("") == ""

    @pytest.mark.parametrize("name", [
        "document_convert", "smart_anonymize",
        "convert_html_to_pdf", "convert_html_to_docx",
    ])
    def test_the_four_incident_probes_specifically(self, name):
        probe = next(p for p in smoke.PROBES if p.name == name)
        assert "X-User-ID: anonymous:bridge-deploy-smoke" in probe.repro
