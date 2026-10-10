"""SMOKE_POOL_REFUSED_ONLY (scripts/bridge_smoke.py, BR6S 2026-10-10).

The smoke still exits 1 when no pool-gated probe passed — it cannot tell an
empty pool from an image that starves its router. The marker only states that
the failure set is nothing but the LB pool router's own refusals
(source=bridge_nginx, bridge_type=pool_exhausted — written before the request
reaches the image), so bridge-deploy.sh can measure the pool and decide. Any
other failure in the set, or a refusal written by a worker (bridge_account, the
image under test itself), = no marker (BR6Sb, BR6SR MUSS 1).
"""

import importlib.util
import os
import sys

import pytest

_SMOKE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts", "bridge_smoke.py",
)
_spec = importlib.util.spec_from_file_location("bridge_smoke_marker_under_test", _SMOKE_PATH)
smoke = importlib.util.module_from_spec(_spec)
sys.modules["bridge_smoke_marker_under_test"] = smoke
_spec.loader.exec_module(smoke)


def _refused(name, ep, source="bridge_nginx", bridge_type="pool_exhausted"):
    return smoke.ProbeResult(name, ep, False, "pool capacity unavailable (pool_exhausted)",
                             429, 5, capacity_reason="pool_exhausted", retry_after_s=1,
                             capacity_source=source, capacity_bridge_type=bridge_type)


def _ok(name, ep):
    return smoke.ProbeResult(name, ep, True, "fine", 200, 5)


def _run(monkeypatch, capsys, results):
    for r in results:
        r.repro = ""  # set by the @probe decorator on real runs
    monkeypatch.setattr(smoke, "run", lambda *a, **k: results)
    monkeypatch.setattr(sys, "argv", ["bridge_smoke.py", "--base-url", "http://x.invalid",
                                      "--expect-bridge", "dev"])
    with pytest.raises(SystemExit) as e:
        smoke.main()
    out = capsys.readouterr()
    return e.value.code, out.out + out.err


def test_only_pool_refusals_exit_1_with_marker(monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, [
        _refused("research", "/v1/research"),
        _refused("chat_completions", "/v1/chat/completions"),
        _ok("document_convert", "/v1/document/convert"),
    ])
    assert code == 1
    assert "SMOKE_POOL_REFUSED_ONLY: research(pool_exhausted), chat_completions(pool_exhausted)" in out


def test_additional_real_failure_suppresses_marker(monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, [
        _refused("research", "/v1/research"),
        _refused("chat_completions", "/v1/chat/completions"),
        smoke.ProbeResult("document_convert", "/v1/document/convert", False, "HTTP 415", 415, 5),
    ])
    assert code == 1
    assert "SMOKE_POOL_REFUSED_ONLY" not in out


def test_refusal_excused_by_passing_pool_probe_needs_no_marker(monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, [
        _refused("research", "/v1/research"),
        _ok("chat_completions", "/v1/chat/completions"),
    ])
    assert code == 0
    assert "SMOKE_CAPACITY:" in out and "SMOKE_POOL_REFUSED_ONLY" not in out


def test_worker_written_refusal_gets_no_marker(monkeypatch, capsys):
    # bridge_account = a worker of the image under test refused — not the LB.
    code, out = _run(monkeypatch, capsys, [
        _refused("research", "/v1/research", source="bridge_account", bridge_type="account_exhausted"),
        _refused("chat_completions", "/v1/chat/completions"),
    ])
    assert code == 1
    assert "SMOKE_POOL_REFUSED_ONLY" not in out


def test_nginx_upstream_envelope_gets_no_marker(monkeypatch, capsys):
    # @bridge_full: nginx relays an UPSTREAM (worker) 429/5xx — the image answered.
    code, out = _run(monkeypatch, capsys, [
        _refused("research", "/v1/research", bridge_type="worker_unavailable"),
        _refused("chat_completions", "/v1/chat/completions", bridge_type="worker_unavailable"),
    ])
    assert code == 1
    assert "SMOKE_POOL_REFUSED_ONLY" not in out


class _Resp:
    status_code = 429

    def __init__(self, err):
        self._err = err

    def json(self):
        return {"error": self._err}


def test_capacity_result_records_who_refused():
    nginx = smoke.capacity_result("chat_completions", "/v1/chat/completions", _Resp({
        "retryable": True, "bridge_type": "pool_exhausted", "source": "bridge_nginx",
        "reason": "all_pool_exhausted", "retry_after_s": 30}), 5)
    assert (nginx.capacity_source, nginx.capacity_bridge_type) == ("bridge_nginx", "pool_exhausted")
    worker = smoke.capacity_result("research", "/v1/research", _Resp({
        "retryable": True, "bridge_type": "account_exhausted", "source": "bridge_account"}), 5)
    assert (worker.capacity_source, worker.capacity_bridge_type) == ("bridge_account", "account_exhausted")


def test_only_accepts_a_comma_list(monkeypatch):
    # smoke-nachholen re-runs exactly the probes .bridge-smoke-unproven lists.
    seen = []
    for p in smoke.PROBES:
        monkeypatch.setattr(p, "fn", lambda ctx, n=p.name, ep=p.endpoint: (seen.append(n) or
                                                                            smoke.ProbeResult(n, ep, True, "ok")))
    monkeypatch.setattr(smoke, "classify_dependency_failures", lambda results, ctx: None)
    monkeypatch.setattr(smoke, "resolve_api_key", lambda: "k")
    smoke.run("http://x.invalid", "hetzner", "research,chat_completions", {}, 1)
    assert sorted(seen) == ["chat_completions", "research"]
