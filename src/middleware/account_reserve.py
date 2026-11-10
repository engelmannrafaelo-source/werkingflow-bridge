"""Weekly subscription reserve. Independent of adaptive concurrency throttling.

Policy (Rafael, 10.11.2026 12:37Z): Claude accounts owned by Rafael may be used by
automation up to 97 % weekly; partner-customer accounts and accounts with unknown
owner stop at 90 %. The owner is never derived from host or name — only from the
documented source below (mirrors the `owner` field of the CUI account registry on
Haus Dev). This Bridge only runs Claude workers; Codex has no reserve here.
"""
import math
from typing import Optional

AUTOMATION_WEEKLY_LIMIT = 90.0  # default: partner customer or unknown owner
OWNER_WEEKLY_LIMIT = {"rafael": 97.0}
# Source: CUI registry /home/claude-user/.claude/accounts/registry.json (Haus Dev), field `owner`.
# Accounts missing here (e.g. the Partner workers) are unknown -> 90 %.
ACCOUNT_OWNER = {"engelmann": "rafael", "office": "rafael", "gmail": "rafael", "werking": "rafael"}

def valid_percent(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0

def automation_weekly_limit(account: Optional[str] = None) -> float:
    """Limit for `account`, or for THIS worker's account when omitted."""
    if account is None:
        from src.middleware.adaptive_limiter import WORKER_ACCOUNT  # lazy: limiter imports this module
        account = WORKER_ACCOUNT
    return OWNER_WEEKLY_LIMIT.get(ACCOUNT_OWNER.get(account or ""), AUTOMATION_WEEKLY_LIMIT)

def reserve_reason(usage: dict, account: Optional[str] = None) -> Optional[str]:
    weekly = usage.get("weekly_pct")
    if not usage.get("known") or not valid_percent(weekly):
        return "Nutzerreserve: Wochenverbrauch nicht frisch gemessen — Automatik wartet."
    limit = automation_weekly_limit(account)
    if weekly >= limit:
        return f"{100 - limit:g} % Nutzerreserve — Automatik ab {limit:g} % Wochenverbrauch gesperrt (aktuell {weekly:g} %)."
    return None

def require_account_reserve() -> None:
    # Lazy import: the limiter also uses the pure decision above.
    from src.middleware.adaptive_limiter import get_adaptive_limiter
    from src.middleware.bridge_error import BridgeError, bridge_error, SOURCE_BRIDGE_ACCOUNT, TYPE_ACCOUNT_EXHAUSTED
    limiter = get_adaptive_limiter()
    limiter._refresh_account_usage()
    reason = reserve_reason(limiter._account_usage)
    if reason:
        raise BridgeError(bridge_error(
            source=SOURCE_BRIDGE_ACCOUNT, error_type=TYPE_ACCOUNT_EXHAUSTED,
            message=reason, status_code=503, retry_after_s=120,
            extra={"limit_window": "weekly_reserve", "automation_weekly_limit": automation_weekly_limit(),
                   "weekly_percent": limiter._account_usage.get("weekly_pct") if valid_percent(limiter._account_usage.get("weekly_pct")) else None}))
