"""Weekly subscription reserve. Independent of adaptive concurrency throttling."""
import math
from typing import Optional

AUTOMATION_WEEKLY_LIMIT = 90.0

def valid_percent(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0

def reserve_reason(usage: dict) -> Optional[str]:
    weekly = usage.get("weekly_pct")
    if not usage.get("known") or not valid_percent(weekly):
        return "Nutzerreserve: Wochenverbrauch nicht frisch gemessen — Automatik wartet."
    if weekly >= AUTOMATION_WEEKLY_LIMIT:
        return f"10 % Nutzerreserve — Automatik ab 90 % Wochenverbrauch gesperrt (aktuell {weekly:g} %)."
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
            extra={"limit_window": "weekly_reserve", "automation_weekly_limit": AUTOMATION_WEEKLY_LIMIT,
                   "weekly_percent": limiter._account_usage.get("weekly_pct") if valid_percent(limiter._account_usage.get("weekly_pct")) else None}))
