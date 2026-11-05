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
  - Metrics-reader unreachable / non-200 / empty accounts → RuntimeError,
    AUSSER es existiert ein Last-known-good-Snapshot juenger als
    SANDBOX_POOL_STATE_STALE_MAX_S (Default 60s). Der wird dann — laut
    geloggt, mit Alter — verwendet, damit ein kurzer Reader-Aussetzer
    (Child-Restart nach OOM, Deploy) laufende Sandbox-Starts nicht killt.
    Bewusste Design-Entscheidung, kein Silent Fallback: Deckelung hart,
    Risiko ist ein bis zu 60s veralteter Sperr-Status eines Accounts.
  - Account-pool-state row missing any required field → RuntimeError
    (would mask broken state otherwise).
  - No eligible account → NoCapacityError carrying per-account exclusion
    reasons, so the caller logs/returns actionable diagnostics.
"""
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_METRICS_READER_URL = os.getenv("BRIDGE_METRICS_READER_URL", "http://metrics-reader:8000")
_HEADROOM_THRESHOLD = float(os.getenv("SANDBOX_HEADROOM_THRESHOLD", "10"))
_STALE_MAX_S = float(os.getenv("SANDBOX_POOL_STATE_STALE_MAX_S", "60"))

# Stufe 1 (Rafael 02.10.2026, e-tester-agent-bridge-20261002: "Kontovergabe
# nimmt zuerst die vier Worker, dann Dev"): Basis-URL der Bridge, deren
# account-pool-state die Prod-Worker-Konten (coach/erk/kurt/sahori) meldet.
# Leer = Stufe 1 aus, die Vergabe bleibt Dev-only — das wird bei JEDER Vergabe
# als Warnung geloggt, nicht still hingenommen. Die Login-Token dieser Konten
# liegen nach derselben Konvention wie die Dev-Token im Secrets-Verzeichnis
# dieses Hosts (lease_service.read_oauth_token); ein Prod-Konto ohne Token
# wird mit Grund ausgeschlossen und laut geloggt.
_PROD_POOL_STATE_URL = os.getenv("SANDBOX_PROD_POOL_STATE_URL", "").rstrip("/")
# Compose baut die URL aus BRIDGE_PROD_HOST; fehlt der, steht hier "http://:8000".
if _PROD_POOL_STATE_URL and not urlparse(_PROD_POOL_STATE_URL).hostname:
    logger.error(
        "SANDBOX_PROD_POOL_STATE_URL=%r has no host (BRIDGE_PROD_HOST unset?) — "
        "prod-worker tier DISABLED", _PROD_POOL_STATE_URL,
    )
    _PROD_POOL_STATE_URL = ""

TIER_PROD = "prod"
TIER_DEV = "dev"

# Last-known-good Pool-State je Quelle: url -> (monotonic-Zeitstempel, accounts-Dict).
_last_good_state: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}


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
    tier: str = TIER_DEV
    policy_revision: Optional[int] = None
    selection_reason: str = "automatic"


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
        # Der Pool-State liefert kein soft_penalty_remaining_s; eine
        # Tracker-Strafe steht nur in cooldown_remaining_s. Ohne diese Zeile
        # hiess sie im NO_CAPACITY-Log "unknown".
        if not parts and cooldown > 0:
            parts.append(f"rate_limit_penalty={cooldown}s")
        reason = "not available (" + (", ".join(parts) if parts else "unknown") + ")"
        return False, reason, headroom, cooldown

    if headroom <= _HEADROOM_THRESHOLD:
        return False, f"headroom {headroom:.1f}% <= threshold {_HEADROOM_THRESHOLD}%", headroom, cooldown

    return True, "", headroom, cooldown


async def _fetch_pool_state(base_url: str) -> dict[str, dict[str, Any]]:
    """Frischen Pool-State von base_url holen. RuntimeError bei
    unreachable / non-200 / leerem accounts-Dict — Bewertung passiert im Caller."""
    url = f"{base_url}/v1/metrics/account-pool-state"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
    except httpx.RequestError as exc:
        raise RuntimeError(f"account-pool-state unreachable ({url}): {exc}") from exc

    if resp.status_code != 200:
        raise RuntimeError(
            f"account-pool-state returned HTTP {resp.status_code} from {url}"
        )

    try:
        accounts = resp.json().get("accounts", {})
        if not isinstance(accounts, dict) or not all(isinstance(v, dict) for v in accounts.values()):
            raise ValueError("'accounts' is not a dict of account rows")
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError(f"account-pool-state from {url} is malformed: {exc}") from exc

    if not accounts:
        raise RuntimeError(f"account-pool-state returned empty accounts dict from {url}")
    return accounts


async def _load_state(base_url: str) -> dict[str, dict[str, Any]]:
    """Pool-State einer Quelle, mit hart gedeckeltem Last-known-good-Rueckgriff.

    Kurzer Reader-Aussetzer (uvicorn-Child-Restart nach OOM, Deploy) soll
    einen Sandbox-Start nicht sofort killen. Rueckgriff auf den letzten guten
    Snapshot ist hart gedeckelt und wird LAUT mit Alter geloggt — kein Silent
    Fallback. Restrisiko: eine in der Zwischenzeit gesetzte Account-Sperre ist
    bis zu _STALE_MAX_S lang nicht sichtbar.
    """
    try:
        accounts = await _fetch_pool_state(base_url)
        _last_good_state[base_url] = (time.monotonic(), accounts)
        return accounts
    except RuntimeError as exc:
        snap = _last_good_state.get(base_url)
        if snap is None:
            raise
        age = time.monotonic() - snap[0]
        if age > _STALE_MAX_S:
            raise RuntimeError(
                f"pool state {base_url} unavailable and last-known-good snapshot is "
                f"{age:.0f}s old (max {_STALE_MAX_S:.0f}s): {exc}"
            ) from exc
        logger.warning(
            f"pick_account: pool state {base_url} unavailable ({exc}) — using "
            f"last-known-good snapshot, age={age:.1f}s (max {_STALE_MAX_S:.0f}s)"
        )
        return snap[1]


async def _fetch_observed_penalties() -> dict[str, int]:
    """Sandbox-observed 429 penalties ({account: remaining_s}) from THIS
    bridge's metrics-reader. The daemon reports every sandbox 429 there, also
    for prod-worker accounts; the prod bridge never sees them. RuntimeError on
    any failure — the caller must not lease prod accounts blind."""
    url = f"{_METRICS_READER_URL}/v1/metrics/sandbox-observed-rate-limits"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
    except httpx.RequestError as exc:
        raise RuntimeError(f"sandbox penalties unreachable ({url}): {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(f"sandbox penalties returned HTTP {resp.status_code} from {url}")
    try:
        penalties = resp.json().get("penalties")
        if not isinstance(penalties, dict):
            raise ValueError("no 'penalties' dict")
        return {str(k): int(v) for k, v in penalties.items()}
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError(f"sandbox penalties response from {url} is malformed: {exc}") from exc


def _overlay_penalties(
    accounts: dict[str, dict[str, Any]], penalties: dict[str, int]
) -> dict[str, dict[str, Any]]:
    """Copy of accounts with sandbox-observed penalties applied — same effect
    as the metrics-reader overlay for its own accounts."""
    out: dict[str, dict[str, Any]] = {}
    for acct_name, info in accounts.items():
        remaining = penalties.get(acct_name, 0)
        if remaining > 0:
            info = dict(info)
            info["available"] = False
            info["soft_penalty_remaining_s"] = max(int(info.get("soft_penalty_remaining_s") or 0), remaining)
            info["cooldown_remaining_s"] = max(int(info.get("cooldown_remaining_s") or 0), remaining)
        out[acct_name] = info
    return out


# (acct_name, headroom, cooldown, lease_count)
_Row = tuple[str, float, int, int]


def _split(
    accounts: dict[str, dict[str, Any]],
    lease_counts: dict[str, int],
    exclusion_reasons: dict[str, str],
    all_cooldowns: list[int],
    reason_prefix: str,
    has_token: Optional[Callable[[str], bool]] = None,
) -> tuple[list[_Row], list[_Row]]:
    """Bewertet eine Quelle; liefert (measured, unmeasured) der zulaessigen Konten.
    Ausschluesse landen mit Quellen-Praefix in exclusion_reasons."""
    measured: list[_Row] = []
    unmeasured: list[_Row] = []
    for acct_name, info in accounts.items():
        ok, reason, headroom, cooldown = _evaluate(acct_name, info)
        all_cooldowns.append(cooldown)
        if ok and has_token is not None and not has_token(acct_name):
            ok = False
            reason = "no oauth token file on this host"
            logger.error(
                "pick_account: %s account %r is eligible but has NO token file on "
                "this host — excluded. Provision claude_token_%s.txt.",
                reason_prefix, acct_name, acct_name,
            )
        if not ok:
            exclusion_reasons[f"{reason_prefix}:{acct_name}"] = reason
            continue
        row = (acct_name, headroom, cooldown, lease_counts.get(acct_name, 0))
        # Strictly `is True`: a missing field (worker on a pre-tri-state image)
        # must count as unmeasured, never as measured.
        if info.get("usage_known") is True:
            measured.append(row)
        else:
            unmeasured.append(row)
    return measured, unmeasured


def _choose(
    eligible: list[_Row],
    preferred_account_id: Optional[str],
    tier: str,
    exclusion_reasons: dict[str, str],
) -> PickedAccount:
    # Honour preferred if eligible in the tier being served — a resume hint
    # never pulls a lease down a tier.
    if preferred_account_id:
        for acct_name, headroom, cooldown, lc in eligible:
            if acct_name == preferred_account_id:
                logger.info(
                    f"pick_account: tier={tier} preferred={acct_name} headroom={headroom:.1f}% "
                    f"cooldown_rem={cooldown}s lease_count={lc} (of {len(eligible)} eligible)"
                )
                return PickedAccount(account_id=acct_name, headroom_percent=headroom, tier=tier)

    # Fair round-robin: rank by (lease_count ASC, -headroom ASC, cooldown ASC).
    # Least-used wins; ties broken by most-budget; final tiebreak shortest cooldown.
    ranked = sorted(eligible, key=lambda x: (x[3], -x[1], x[2]))
    picked_name, picked_headroom, picked_cooldown, picked_lc = ranked[0]
    logger.info(
        f"pick_account: tier={tier} picked={picked_name} headroom={picked_headroom:.1f}% "
        f"cooldown_rem={picked_cooldown}s lease_count={picked_lc} "
        f"(of {len(eligible)} eligible, excluded={list(exclusion_reasons.keys())})"
    )
    return PickedAccount(account_id=picked_name, headroom_percent=picked_headroom, tier=tier)


async def pick_account(
    preferred_account_id: Optional[str] = None,
    lease_counts: Optional[dict[str, int]] = None,
    has_token: Optional[Callable[[str], bool]] = None,
) -> PickedAccount:
    """Fresh user policy first; technical/token guards still apply to every candidate."""
    from src.account_policy.client import fetch_policy
    from src.account_policy.model import choose, rank
    policy = await fetch_policy()  # never reuse a cached authorization
    lease_counts = lease_counts or {}
    reasons: dict[str, str] = {}
    cooldowns: list[int] = []
    measured, unmeasured = [], []
    seen = set()
    tiers = {}

    async def collect(url, tier, token_check=None):
        accounts = await _load_state(url)
        if tier == TIER_PROD:
            accounts = _overlay_penalties(accounts, await _fetch_observed_penalties())
        clash = seen & set(accounts)
        if clash:
            raise RuntimeError("duplicate account in BOTH pool sources: " + ",".join(sorted(clash)))
        seen.update(accounts)
        allowed = {}
        for account, info in accounts.items():
            if rank(policy, account) is None:
                reasons[tier + ":" + account] = "bridge_disabled"
            else:
                allowed[account] = info
                tiers[account] = tier
        m, u = _split(allowed, lease_counts, reasons, cooldowns, tier, token_check)
        measured.extend(m); unmeasured.extend(u)

    if _PROD_POOL_STATE_URL and has_token is not None:
        try:
            await collect(_PROD_POOL_STATE_URL, TIER_PROD, has_token)
        except RuntimeError as exc:
            if "duplicate account" in str(exc):
                raise
            reasons["prod"] = "pool state unavailable"
            logger.error("policy admission: prod-worker pool state unavailable")
    else:
        reasons["prod"] = "tier disabled (URL or token check unavailable)"
        logger.warning("prod-worker tier DISABLED: %s", reasons["prod"])
    if not measured:
        logger.warning("no measured prod-worker account eligible: %s", reasons)
    try:
        await collect(_METRICS_READER_URL, TIER_DEV)
    except RuntimeError as exc:
        if "duplicate account" in str(exc):
            raise
        if not measured and not unmeasured:
            raise
        reasons["dev"] = "pool state unavailable"
    eligible = measured or unmeasured
    selected = choose(policy, [r[0] for r in eligible], preferred_account_id)
    if selected is None:
        if preferred_account_id:
            reasons["preferred"] = "preferred account disabled or technically unavailable; no eligible fallback"
        raise NoCapacityError(min([c for c in cooldowns if c > 0] or [30]), reasons)
    row = next(r for r in eligible if r[0] == selected)
    logger.info("pick_account policy_revision=%s account=%s priority=%s tier=%s",
                policy["revision"], selected, rank(policy, selected), tiers[selected])
    return PickedAccount(account_id=selected, headroom_percent=row[1], tier=tiers[selected],
                         policy_revision=policy["revision"],
                         selection_reason=("preferred" if selected == preferred_account_id else
                                           "preferred_unavailable_fallback" if preferred_account_id else "priority"))
