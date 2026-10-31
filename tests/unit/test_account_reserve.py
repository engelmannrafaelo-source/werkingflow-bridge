import json
import time
from unittest.mock import MagicMock
import pytest
from src.middleware.account_reserve import reserve_reason, require_account_reserve

@pytest.mark.parametrize("pct,blocked", [(0,False),(89.9,False),(90,True),(99,True),(100,True)])
def test_weekly_boundary(pct, blocked):
    assert bool(reserve_reason({"known":True,"weekly_pct":pct})) is blocked

@pytest.mark.parametrize("pct", [None, float('nan'),float('inf'),-1,True,'40'])
def test_invalid_is_not_free(pct):
    assert reserve_reason({"known":True,"weekly_pct":pct})

def test_unknown_is_not_free():
    assert reserve_reason({"known":False,"weekly_pct":0})

def test_direct_provider_guard_and_reset(monkeypatch):
    from src.middleware import adaptive_limiter as al
    from src.middleware.bridge_error import BridgeError
    lim = MagicMock()
    lim._account_usage = {"known":True,"weekly_pct":90}
    monkeypatch.setattr(al, 'get_adaptive_limiter', lambda: lim)
    with pytest.raises(BridgeError) as e: require_account_reserve()
    assert e.value.response.status_code == 503
    assert json.loads(e.value.response.body)['error']['limit_window'] == 'weekly_reserve'
    lim._account_usage = {"known":True,"weekly_pct":0}
    require_account_reserve()

@pytest.mark.asyncio
async def test_admission_reserve_independent_of_predictive_throttle(tmp_path, monkeypatch):
    from src.middleware import adaptive_limiter as al
    monkeypatch.setattr(al,'METRICS_DIR',str(tmp_path))
    monkeypatch.setattr(al,'WEEKLY_PREDICTIVE_THROTTLE_ENABLED',False)
    lim=al.AdaptiveLoadLimiter()
    monkeypatch.setattr(lim,'_refresh_account_usage',lambda:None)
    for pct,known,blocked in [(89.9,True,False),(90,True,True),(None,False,True),(0,True,False)]:
        lim._account_usage={'known':known,'weekly_pct':pct,'session_pct':None,'budget_multiplier':1.0,'ts':time.time()}
        ok,reason,snapshot,waited = await lim.acquire_with_wait(1,timeout_s=20)
        assert ok is not blocked
        assert snapshot['reserve_blocked'] is blocked
        assert waited < 1

@pytest.mark.asyncio
async def test_cli_start_and_native_fallback_cannot_bypass_reserve(monkeypatch):
    from src.claude_cli import ClaudeCodeCLI
    from src.middleware import adaptive_limiter as al
    from src.middleware.bridge_error import BridgeError
    limiter=MagicMock()
    limiter._account_usage={'known':True,'weekly_pct':90}
    monkeypatch.setattr(al,'get_adaptive_limiter',lambda:limiter)
    cli=object.__new__(ClaudeCodeCLI)
    # Guard executes before any SDK/subprocess, files or per-run budget are touched.
    with pytest.raises(BridgeError):
        await anext(cli.run_completion('test'))
    with pytest.raises(BridgeError):
        await cli.run_native_cli('test')
