"""SMOKE_POOL_REFUSED_ONLY (scripts/bridge_smoke.py, BR6S 2026-10-10).

The smoke still exits 1 when no pool-gated probe passed — it cannot tell an
empty pool from an image that starves its router. The marker only states that
the failure set is nothing but pool-gate refusals, so bridge-deploy.sh can
measure the pool and decide. Any other failure in the set = no marker.
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


def _refused(name, ep):
    return smoke.ProbeResult(name, ep, False, "pool capacity unavailable (pool_exhausted)",
                             429, 5, capacity_reason="pool_exhausted", retry_after_s=1)


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
