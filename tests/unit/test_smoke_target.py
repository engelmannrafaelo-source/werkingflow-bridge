"""Deploy smoke: did the DEPLOYED bridge answer? (scripts/bridge_smoke.py, BR6 E1)

Until 2026-10-10 the hetzner smoke's research/chat probes went to the dev URL
without X-Bridge-Hop and were answered by the PROD workers (ADR-0010) — a green
smoke for every dev build that proved nothing about it (BR2D §7). The probes
now read the worker's X-Bridge-Served-By stamp. These tests pin the verdicts:
wrong bridge = FAIL, missing stamp = FAIL when required / UNPROVEN gap when
optional, right bridge = pass with the answering worker in the log line.
"""

import importlib.util
import os
import sys

_SMOKE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts", "bridge_smoke.py",
)
_spec = importlib.util.spec_from_file_location("bridge_smoke_target_under_test", _SMOKE_PATH)
smoke = importlib.util.module_from_spec(_spec)
sys.modules["bridge_smoke_target_under_test"] = smoke
_spec.loader.exec_module(smoke)


class _Resp:
    def __init__(self, headers):
        self.headers = headers
        self.status_code = 200


def _ctx(bridge="dev", required=True):
    return smoke.Ctx(base_url="http://bridge.invalid", api_key="k",
                     expect_bridge=bridge, served_by_required=required)


def _ok(name="chat_completions"):
    return smoke.ProbeResult(name, "/v1/chat/completions", True, "completion returned", 200, 12)


def test_answer_from_expected_bridge_passes_and_names_the_worker():
    r = smoke.check_target(_ctx(), _ok(), _Resp({"X-Bridge-Served-By": "dev/worker3",
                                                 "X-Target-Worker": "worker3"}))
    assert r.ok
    assert r.served_by == "dev/worker3"
    assert "served_by=dev/worker3" in r.detail and "X-Target-Worker=worker3" in r.detail


def test_answer_from_other_bridge_fails_hard():
    # The exact pre-fix situation: dev deploy, prod worker answered.
    r = smoke.check_target(_ctx("dev"), _ok(), _Resp({"X-Bridge-Served-By": "prod/worker-kurt"}))
    assert not r.ok and not r.target_reason
    assert "prod/worker-kurt" in r.detail and "'dev'" in r.detail
    _, gaps, failures = smoke.partition_results([r])
    assert failures == [r] and gaps == []


def test_prod_deploy_answered_by_dev_backup_fails_hard():
    r = smoke.check_target(_ctx("prod", required=False), _ok(),
                           _Resp({"X-Bridge-Served-By": "dev/worker1"}))
    assert not r.ok and not r.target_reason


def test_missing_stamp_fails_when_required():
    r = smoke.check_target(_ctx(required=True), _ok(), _Resp({}))
    assert not r.ok and not r.target_reason
    assert "X-Bridge-Served-By" in r.detail


def test_missing_stamp_is_unproven_gap_when_optional_never_green():
    r = smoke.check_target(_ctx("prod", required=False), _ok(), _Resp({}))
    assert not r.ok and r.target_reason
    passed, gaps, failures = smoke.partition_results([r])
    assert passed == [] and failures == [] and gaps == [r]


def test_unproven_target_still_proves_the_pool_gate_for_capacity_refusals():
    unproven = smoke.check_target(_ctx("prod", required=False), _ok("chat_completions"), _Resp({}))
    refused = smoke.ProbeResult("research", "/v1/research", False, "pool capacity unavailable",
                                429, 5, capacity_reason="pool_exhausted", retry_after_s=30)
    _, gaps, failures = smoke.partition_results([unproven, refused])
    assert failures == [] and set(g.name for g in gaps) == {"chat_completions", "research"}


def test_failed_probe_keeps_its_own_reason():
    bad = smoke.ProbeResult("chat_completions", "/v1/chat/completions", False, "HTTP 500", 500, 3)
    assert smoke.check_target(_ctx(), bad, _Resp({"X-Bridge-Served-By": "prod/x"})) is bad


def test_no_expectation_means_no_check():
    r = smoke.check_target(_ctx(bridge=""), _ok(), _Resp({}))
    assert r.ok
