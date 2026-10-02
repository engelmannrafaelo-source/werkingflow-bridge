"""
Sandbox-Kontovergabe: Prod-Worker-Konten zuerst, Dev als Ersatz
(Rafael 02.10.2026, Entscheidung e-tester-agent-bridge-20261002).

pick_account liest zwei Pool-States — Prod-Bridge (Stufe 1) und den eigenen
metrics-reader (Stufe 2) — und vergibt Dev erst, wenn kein Prod-Konto
zulaessig ist. Jeder Stufenwechsel hat einen Grund in den Logs bzw. in
NoCapacityError.reasons.
"""
import logging

import pytest

from src.sandbox import account_router as ar

PROD_URL = "http://prod-lb.test:8000"
DEV_URL = "http://metrics-reader.test:8000"


def _row(headroom=80.0, *, available=True, known=True, cooldown=0, **extra):
    row = {
        "worker": "w",
        "available": available,
        "headroom_percent": headroom,
        "cooldown_remaining_s": cooldown,
        "usage_known": known,
    }
    row.update(extra)
    return row


def _prod(**over):
    base = {n: _row() for n in ("coach", "erk", "kurt", "sahori")}
    base.update(over)
    return base


def _dev(**over):
    base = {n: _row() for n in ("engelmann", "office", "werking")}
    base.update(over)
    return base


@pytest.fixture
def pools(monkeypatch):
    """Configurable fake sources. Set state['prod'/'dev'] to a dict or an
    Exception; state['penalties'] likewise."""
    state = {"prod": _prod(), "dev": _dev(), "penalties": {}, "calls": []}

    async def fake_fetch(base_url):
        key = {PROD_URL: "prod", DEV_URL: "dev"}[base_url]
        state["calls"].append(key)
        val = state[key]
        if isinstance(val, Exception):
            raise val
        return val

    async def fake_penalties():
        val = state["penalties"]
        if isinstance(val, Exception):
            raise val
        return val

    monkeypatch.setattr(ar, "_PROD_POOL_STATE_URL", PROD_URL)
    monkeypatch.setattr(ar, "_METRICS_READER_URL", DEV_URL)
    monkeypatch.setattr(ar, "_last_good_state", {})
    monkeypatch.setattr(ar, "_fetch_pool_state", fake_fetch)
    monkeypatch.setattr(ar, "_fetch_observed_penalties", fake_penalties)
    return state


def _all_tokens(_acct):
    return True


async def test_prod_account_wins_over_a_better_dev_account(pools):
    pools["prod"] = _prod(coach=_row(15.0), erk=_row(12.0), kurt=_row(11.0), sahori=_row(20.0))
    pools["dev"] = _dev(engelmann=_row(99.0))
    picked = await ar.pick_account(has_token=_all_tokens)
    assert picked.tier == ar.TIER_PROD
    assert picked.account_id == "sahori"
    # Dev state is not even read when a prod account is eligible.
    assert pools["calls"] == ["prod"]


async def test_round_robin_by_lease_count_inside_the_prod_tier(pools):
    counts = {"coach": 5, "erk": 1, "kurt": 3, "sahori": 2, "engelmann": 0}
    picked = await ar.pick_account(lease_counts=counts, has_token=_all_tokens)
    assert (picked.tier, picked.account_id) == (ar.TIER_PROD, "erk")


async def test_all_prod_locked_falls_to_dev_with_reasons_logged(pools, caplog):
    pools["prod"] = _prod(
        coach=_row(0.0, available=False, capacity_lock_remaining_s=300),
        erk=_row(50.0, available=False, is_hard_limited=True),
        kurt=_row(5.0),  # under headroom threshold
        sahori=_row(50.0, available=False, cooldown=120),
    )
    pools["dev"] = _dev(office=_row(70.0), werking=_row(60.0), engelmann=_row(10.0))
    with caplog.at_level(logging.WARNING, logger=ar.logger.name):
        picked = await ar.pick_account(has_token=_all_tokens)
    assert (picked.tier, picked.account_id) == (ar.TIER_DEV, "office")
    assert "no measured prod-worker account eligible" in caplog.text
    assert "prod:coach" in caplog.text


async def test_everything_locked_raises_no_capacity_with_both_tiers(pools):
    locked = _row(0.0, available=False, cooldown=90)
    pools["prod"] = {n: dict(locked) for n in ("coach", "erk", "kurt", "sahori")}
    pools["dev"] = {n: _row(0.0, available=False, cooldown=45) for n in ("engelmann", "office")}
    with pytest.raises(ar.NoCapacityError) as ei:
        await ar.pick_account(has_token=_all_tokens)
    reasons = ei.value.reasons
    assert {"prod:coach", "prod:erk", "prod:kurt", "prod:sahori", "dev:engelmann", "dev:office"} <= set(reasons)
    assert ei.value.retry_after_s == 45


async def test_prod_state_unreachable_falls_to_dev_loudly(pools, caplog):
    pools["prod"] = RuntimeError("account-pool-state unreachable (prod)")
    with caplog.at_level(logging.ERROR, logger=ar.logger.name):
        picked = await ar.pick_account(has_token=_all_tokens)
    assert picked.tier == ar.TIER_DEV
    assert "prod-worker pool state unavailable" in caplog.text


async def test_prod_unreachable_and_dev_locked_is_503_with_prod_reason(pools):
    pools["prod"] = RuntimeError("boom")
    pools["dev"] = {"engelmann": _row(0.0, available=False)}
    with pytest.raises(ar.NoCapacityError) as ei:
        await ar.pick_account(has_token=_all_tokens)
    assert "pool state unavailable" in ei.value.reasons["prod"]


async def test_prod_account_without_token_file_is_excluded_loudly(pools, caplog):
    have = {"kurt"}
    with caplog.at_level(logging.ERROR, logger=ar.logger.name):
        picked = await ar.pick_account(has_token=lambda a: a in have)
    assert (picked.tier, picked.account_id) == (ar.TIER_PROD, "kurt")
    assert "has NO token file" in caplog.text


async def test_no_prod_token_at_all_means_dev(pools):
    picked = await ar.pick_account(has_token=lambda a: False)
    assert picked.tier == ar.TIER_DEV


async def test_sandbox_observed_penalty_locks_a_prod_account(pools):
    pools["prod"] = _prod(coach=_row(99.0))
    pools["penalties"] = {"coach": 120}
    picked = await ar.pick_account(has_token=_all_tokens)
    assert picked.tier == ar.TIER_PROD
    assert picked.account_id != "coach"


async def test_penalties_on_every_prod_account_fall_to_dev(pools):
    pools["penalties"] = {n: 60 for n in ("coach", "erk", "kurt", "sahori")}
    picked = await ar.pick_account(has_token=_all_tokens)
    assert picked.tier == ar.TIER_DEV


async def test_penalties_unreadable_does_not_lease_prod_blind(pools):
    pools["penalties"] = RuntimeError("penalties unreachable")
    picked = await ar.pick_account(has_token=_all_tokens)
    assert picked.tier == ar.TIER_DEV


async def test_preferred_dev_account_does_not_pull_lease_down_a_tier(pools):
    picked = await ar.pick_account(preferred_account_id="engelmann", has_token=_all_tokens)
    assert picked.tier == ar.TIER_PROD


async def test_preferred_prod_account_is_honoured(pools):
    picked = await ar.pick_account(
        preferred_account_id="kurt", lease_counts={"kurt": 9}, has_token=_all_tokens,
    )
    assert picked.account_id == "kurt"


async def test_measured_dev_beats_unmeasured_prod(pools):
    pools["prod"] = {n: _row(95.0, known=False) for n in ("coach", "erk")}
    picked = await ar.pick_account(has_token=_all_tokens)
    assert picked.tier == ar.TIER_DEV


async def test_unmeasured_prod_beats_unmeasured_dev_when_nothing_is_measured(pools):
    pools["prod"] = {"coach": _row(50.0, known=False)}
    pools["dev"] = {"engelmann": _row(90.0, known=False)}
    picked = await ar.pick_account(has_token=_all_tokens)
    assert (picked.tier, picked.account_id) == (ar.TIER_PROD, "coach")


async def test_shared_account_name_across_tiers_fails_loud(pools):
    pools["prod"] = {"office": _row(0.0, available=False)}
    with pytest.raises(RuntimeError, match="BOTH"):
        await ar.pick_account(has_token=_all_tokens)


async def test_tier_disabled_without_url_is_dev_only_and_says_so(pools, monkeypatch, caplog):
    monkeypatch.setattr(ar, "_PROD_POOL_STATE_URL", "")
    with caplog.at_level(logging.WARNING, logger=ar.logger.name):
        picked = await ar.pick_account(has_token=_all_tokens)
    assert picked.tier == ar.TIER_DEV
    assert pools["calls"] == ["dev"]
    assert "prod-worker tier DISABLED" in caplog.text


async def test_tier_disabled_without_token_check(pools):
    picked = await ar.pick_account()
    assert picked.tier == ar.TIER_DEV


async def test_stale_prod_snapshot_is_used_within_limit_per_source(pools, monkeypatch):
    await ar.pick_account(has_token=_all_tokens)  # primes the prod snapshot
    pools["prod"] = RuntimeError("blip")
    picked = await ar.pick_account(has_token=_all_tokens)
    assert picked.tier == ar.TIER_PROD
    # The dev snapshot was never primed — a dev outage must still raise.
    pools["prod"] = {"coach": _row(0.0, available=False)}
    pools["dev"] = RuntimeError("dev down")
    monkeypatch.setattr(ar, "_last_good_state", {})
    with pytest.raises(RuntimeError, match="dev down"):
        await ar.pick_account(has_token=_all_tokens)


def test_has_oauth_token_never_needs_the_value(tmp_path, monkeypatch):
    from src.sandbox import lease_service as ls

    monkeypatch.setattr(ls, "_SECRETS_DIR", tmp_path)
    (tmp_path / "claude_token_coach.txt").write_text("x" * 20)
    (tmp_path / "claude_token_erk.txt").write_text("  \n")
    assert ls.has_oauth_token("coach") is True
    assert ls.has_oauth_token("erk") is False
    assert ls.has_oauth_token("kurt") is False


def test_metrics_reader_exposes_penalties_of_foreign_accounts(tmp_path, monkeypatch):
    """The pool-state overlay only covers this bridge's own accounts; prod
    accounts the daemon reported must still be readable for the lease router."""
    import time

    from src.metrics_reader import main as mr

    monkeypatch.setattr(mr, "_PENALTY_FILE", str(tmp_path / "penalties.json"))
    mr.post_observed_rate_limit({"account_id": "coach", "retry_after_s": 120})
    mr._write_penalties({**mr._read_penalties(), "erk": time.time() - 5})  # expired
    out = mr.get_observed_rate_limits()
    assert set(out["penalties"]) == {"coach"}
    assert 100 < out["penalties"]["coach"] <= 120


async def test_malformed_json_is_a_runtime_error_not_a_500(monkeypatch):
    """A 200 with HTML (or any non-JSON) must surface as RuntimeError — the
    route maps that to 503, and the prod tier then falls to dev."""
    import httpx

    class _Resp:
        status_code = 200

        def json(self):
            raise ValueError("Expecting value: line 1 column 1")

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url): return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    with pytest.raises(RuntimeError, match="malformed"):
        await ar._fetch_pool_state(PROD_URL)
    with pytest.raises(RuntimeError, match="malformed"):
        await ar._fetch_observed_penalties()


def test_has_oauth_token_rejects_path_like_ids(tmp_path, monkeypatch):
    from src.sandbox import lease_service as ls

    monkeypatch.setattr(ls, "_SECRETS_DIR", tmp_path / "s")
    (tmp_path / "claude_token_x.txt").write_text("secret")
    assert ls.has_oauth_token("../claude_token_x") is False
    assert ls.has_oauth_token("") is False
