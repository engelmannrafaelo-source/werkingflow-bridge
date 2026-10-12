"""CHATFWDR R2 (Codex, 12.10.2026): Gegenproben unveraendert uebernommen aus
local-storage/chatfwdr-r2/test_chatfwdr_r2_review.py — Abnahme fuer P2-Rest."""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from tests.unit.test_chat_weiterleitung_chatfwd import welt, _post, FRIST_HEADER
import src.main as main
import src.chat_weiterleitung as cw


def test_worker_only_keeps_deadline_across_calls_and_hops(monkeypatch):
    clock = SimpleNamespace(time=lambda: 1000.)
    monkeypatch.setattr(cw, 'time', clock)
    monkeypatch.setattr(cw, 'CHAT_GESAMTFRIST_S', 3300.)
    first = SimpleNamespace(headers={FRIST_HEADER: '9999999999'}, state=SimpleNamespace())
    assert cw.frist_bis(first) == 4300.
    clock.time = lambda: 1050.
    assert cw.frist_bis(first) == 4300.
    peer = SimpleNamespace(headers={FRIST_HEADER: str(cw.frist_bis(first))}, state=SimpleNamespace())
    assert cw.frist_bis(peer) == 4300.
    short = SimpleNamespace(headers={FRIST_HEADER: '1100'}, state=SimpleNamespace())
    assert cw.frist_bis(short) == 1100.


@pytest.mark.asyncio
async def test_real_metrics_safety_margin_is_not_weekly_limit(monkeypatch):
    # Use the actual worker metrics producer, with idle, measured and healthy inputs.
    import src.middleware.adaptive_limiter as al
    import src.middleware.capacity_lock as cl
    monkeypatch.setattr(al, 'SAFETY_MARGIN_PCT', 85.)
    lim = SimpleNamespace(
        _refresh_account_usage=lambda: None,
        state=SimpleNamespace(cap_tokens=1000000, last_rate_limit_ts=None),
        _current_inflight_tokens=lambda: 0,
        _account_usage=dict(known=True, session_pct=0., weekly_pct=0.,
                            source_ts=time.time(), budget_multiplier=1.), worker='worker1')
    monkeypatch.setattr(main, 'get_adaptive_limiter', lambda: lim)
    tracker = MagicMock()
    tracker.get_retry_after.return_value = None
    tracker.is_rate_limited.return_value = False
    tracker.is_hard_limited.return_value = False
    monkeypatch.setattr(main, 'rate_limit_tracker', tracker)
    lock = MagicMock()
    lock.remaining_s.return_value = 0
    lock.get_lock_info.return_value = None
    lock.is_locked.return_value = False
    monkeypatch.setattr(cl, 'get_capacity_lock', lambda: lock)
    snapshot = await main.get_account_pool_state()
    assert snapshot['available'] is True
    assert snapshot['effective_cap_tokens'] == 850000
    assert snapshot['adaptive_cap_tokens'] == 1000000
    print('REAL SNAPSHOT', {k: snapshot[k] for k in ('available','usage_known','session_percent','weekly_percent','budget_multiplier','effective_cap_tokens','adaptive_cap_tokens')})
    ok = cw.pool_lage({'private-account': snapshot}, benoetigt_tokens=800000, ausgeschlossen=set())
    assert ok['ziel'] == 'worker1'
    lage = cw.pool_lage({'private-account': snapshot}, benoetigt_tokens=900000, ausgeschlossen=set())
    print('ACTUAL POOL VERDICT', lage)
    assert lage['grund'] != cw.REASON_POOL_KONTEN_LIMIT, '0% usage and multiplier=1 is not an account limit'
