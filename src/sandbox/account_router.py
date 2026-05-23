"""
Sandbox account-pool router.

Picks the best account for a new sandbox lease from the aggregated
account-pool state served by the metrics-reader.

Filter semantics (architectural contract):
  - `available` from account-pool-state is the single source of truth for
    "this account can serve a new lease". It already encapsulates ALL hard
    locks: capacity_lock, session_pct < 95, headroom > 0, no rate-limit
    tracker penalty. Per design (main.py:5398) `adaptive_cooldown_s` is
    explicitly EXCLUDED from `available` because SHRINK is a capacity
    pacing signal, not a lock — a SHRINK'd account is still serving calls.
  - Additional filter: `headroom_percent > SANDBOX_HEADROOM_THRESHOLD`
    (the account must have enough budget reserve to be worth a lease).
  - `cooldown_remaining_s` is NEVER a filter here — only a tiebreaker score.

Selection (fair round-robin, S7):
  - preferred_account_id wins if eligible.
  - Otherwise: least-recently-used by lease count (lease_counts arg, typically
    leases-issued-in-last-24h from sandbox_leases). Without this argument,
    sort degenerates to headroom-only — backward-compatible.
  - Ties broken by (highest headroom, shortest cooldown).

Fail-fast:
  - Metrics-reader unreachable / non-200 / empty accounts → RuntimeError.
  - Account-pool-state row missing any required field → RuntimeError
    (would mask broken state otherwise).
  - No eligible account → NoCapacityError carrying per-account exclusion
    reasons, so the caller logs/returns actionable diagnostics.
"""
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_METRICS_READER_URL = os.getenv("BRIDGE_METRICS_READER_URL", "http://metrics-reader:8000")
_HEADROOM_THRESHOLD = float(os.getenv("SANDBOX_HEADROOM_THRESHOLD", "10"))


class NoCapacityError(Exception):
    def __init__(self, retry_after_s: int, reasons: Optional[dict[str, str]] = None):
        self.retry_after_s = retry_after_s
        self.reasons = reasons or {}
        reasons_str = "; ".join(f"{k}: {v}" for k, v in self.reasons.items()) or "(no accounts)"
        super().__init__(
            f"No account eligible for sandbox lease (retry_after_s={retry_after_s}). "
            f"Per-account exclusion reasons: {reasons_str}"
        )


@dataclass
class PickedAccount:
    account_id: str
    headroom_percent: float


def _require(info: dict[str, Any], key: str, acct_name: str) -> Any:
    """Fail-fast accessor: missing key means the state shape is broken."""
    if key not in info:
        raise RuntimeError(
            f"account-pool-state row for {acct_name!r} is missing required field {key!r}; "
            f"got keys: {sorted(info.keys())}"
        )
    return info[key]


def _evaluate(acct_name: str, info: dict[str, Any]) -> tuple[bool, str, float, int]:
    """
    Return (eligible, reason_if_excluded, headroom_percent, cooldown_remaining_s).
    `reason_if_excluded` is "" when eligible.
    """
    available = bool(_require(info, "available", acct_name))
    headroom_raw = _require(info, "headroom_percent", acct_name)
    cooldown_raw = _require(info, "cooldown_remaining_s", acct_name)

    headroom = float(headroom_raw) if headroom_raw is not None else 0.0
    cooldown = int(cooldown_raw) if cooldown_raw is not None else 0

    if not available:
        # Build a diagnostic reason from the underlying lock signals
        cap_lock = int(info.get("capacity_lock_remaining_s") or 0)
        soft_pen = int(info.get("soft_penalty_remaining_s") or 0)
        session_pct = float(info.get("session_percent") or 0.0)
        is_hard = bool(info.get("is_hard_limited"))
        parts = []
        if cap_lock > 0:
            parts.append(f"capacity_lock={cap_lock}s")
        if soft_pen > 0:
            parts.append(f"soft_penalty={soft_pen}s")
        if is_hard:
            parts.append("hard_limited")
        if session_pct >= 95.0:
            parts.append(f"session={session_pct}%")
        if headroom <= 0:
            parts.append("headroom_zero")
        reason = "not available (" + (", ".join(parts) if parts else "unknown") + ")"
        return False, reason, headroom, cooldown

    if headroom <= _HEADROOM_THRESHOLD:
        return False, f"headroom {headroom:.1f}% <= threshold {_HEADROOM_THRESHOLD}%", headroom, cooldown

    return True, "", headroom, cooldown


async def pick_account(
    preferred_account_id: Optional[str] = None,
    lease_counts: Optional[dict[str, int]] = None,
) -> PickedAccount:
    """
    Query the metrics reader and return the best account for a new lease.

    Args:
        preferred_account_id: caller hint; wins if eligible (e.g. resume).
        lease_counts: optional {account_id: recent_lease_count} for fairness
            ranking (typically leases-issued-in-last-24h from sandbox_leases).
            Missing accounts default to 0. Without this arg, ranking degrades
            to headroom-only (backward-compatible).

    Raises:
        RuntimeError: metrics reader unreachable / malformed / empty state
        NoCapacityError: no account passes the available+headroom filter;
            carries per-account exclusion reasons for diagnostics.
    """
    url = f"{_METRICS_READER_URL}/v1/metrics/account-pool-state"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
    except httpx.RequestError as exc:
        raise RuntimeError(f"account-pool-state unreachable ({url}): {exc}") from exc

    if resp.status_code != 200:
        raise RuntimeError(
            f"account-pool-state returned HTTP {resp.status_code} from {url}"
        )

    data = resp.json()
    accounts: dict[str, dict[str, Any]] = data.get("accounts", {})

    if not accounts:
        raise RuntimeError(f"account-pool-state returned empty accounts dict from {url}")

    lease_counts = lease_counts or {}
    eligible: list[tuple[str, float, int, int]] = []  # (acct_name, headroom, cooldown, lease_count)
    exclusion_reasons: dict[str, str] = {}
    all_cooldowns: list[int] = []

    for acct_name, info in accounts.items():
        ok, reason, headroom, cooldown = _evaluate(acct_name, info)
        all_cooldowns.append(cooldown)
        if ok:
            eligible.append((acct_name, headroom, cooldown, lease_counts.get(acct_name, 0)))
        else:
            exclusion_reasons[acct_name] = reason

    if not eligible:
        # retry_after_s: shortest non-zero cooldown across pool, or 30s
        # baseline if no cooldowns set (e.g. capacity_lock-only scenarios).
        non_zero = [c for c in all_cooldowns if c > 0]
        retry_after = min(non_zero) if non_zero else 30
        logger.warning(
            f"pick_account NO_CAPACITY: retry_after={retry_after}s reasons={exclusion_reasons}"
        )
        raise NoCapacityError(retry_after_s=retry_after, reasons=exclusion_reasons)

    # Honour preferred if eligible
    if preferred_account_id:
        for acct_name, headroom, cooldown, lc in eligible:
            if acct_name == preferred_account_id:
                logger.info(
                    f"pick_account: preferred={acct_name} headroom={headroom:.1f}% "
                    f"cooldown_rem={cooldown}s lease_count={lc} (of {len(eligible)} eligible)"
                )
                return PickedAccount(account_id=acct_name, headroom_percent=headroom)

    # Fair round-robin: rank by (lease_count ASC, -headroom ASC, cooldown ASC).
    # Least-used wins; ties broken by most-budget; final tiebreak shortest cooldown.
    eligible.sort(key=lambda x: (x[3], -x[1], x[2]))
    picked_name, picked_headroom, picked_cooldown, picked_lc = eligible[0]
    logger.info(
        f"pick_account: picked={picked_name} headroom={picked_headroom:.1f}% "
        f"cooldown_rem={picked_cooldown}s lease_count={picked_lc} "
        f"(of {len(eligible)} eligible, excluded={list(exclusion_reasons.keys())})"
    )
    return PickedAccount(account_id=picked_name, headroom_percent=picked_headroom)
