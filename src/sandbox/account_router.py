"""
Sandbox account-pool router.

Fetches the aggregated account-pool state from the metrics reader and picks
the best available account for a new sandbox lease.

Selection logic (mirrors spec §3 step 3):
  - Filter: available=True AND cooldown_remaining_s=0 AND headroom_percent > 10
  - If preferredAccountId passes the filter → use it
  - Otherwise → pick account with highest headroom_percent
  - No passing account → raise NoCapacityError with min_cooldown from candidates
"""
import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_METRICS_READER_URL = os.getenv("BRIDGE_METRICS_READER_URL", "http://metrics-reader:8000")
_HEADROOM_THRESHOLD = float(os.getenv("SANDBOX_HEADROOM_THRESHOLD", "10"))


class NoCapacityError(Exception):
    def __init__(self, retry_after_s: int):
        self.retry_after_s = retry_after_s
        super().__init__(f"No account with sufficient headroom (retry_after_s={retry_after_s})")


@dataclass
class PickedAccount:
    account_id: str
    headroom_percent: float


async def pick_account(preferred_account_id: Optional[str] = None) -> PickedAccount:
    """
    Query the metrics reader and return the best account for a new lease.

    Raises:
        RuntimeError: if the metrics reader is unreachable (fail-loud)
        NoCapacityError: if all accounts are in cooldown or low headroom
    """
    url = f"{_METRICS_READER_URL}/v1/metrics/account-pool-state"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
    except httpx.RequestError as exc:
        raise RuntimeError(
            f"account-pool-state unreachable ({url}): {exc}"
        ) from exc

    if resp.status_code != 200:
        raise RuntimeError(
            f"account-pool-state returned HTTP {resp.status_code} from {url}"
        )

    data = resp.json()
    accounts: dict = data.get("accounts", {})

    if not accounts:
        raise RuntimeError(
            f"account-pool-state returned empty accounts dict from {url}"
        )

    # Filter to eligible accounts
    eligible: list[tuple[str, float]] = []
    min_cooldown = 0

    for acct_name, info in accounts.items():
        available = info.get("available", False)
        cooldown = info.get("cooldown_remaining_s", 0) or 0
        headroom = float(info.get("headroom_percent", 0.0) or 0.0)

        if available and cooldown == 0 and headroom > _HEADROOM_THRESHOLD:
            eligible.append((acct_name, headroom))
        elif cooldown > 0:
            if min_cooldown == 0 or cooldown < min_cooldown:
                min_cooldown = cooldown

    if not eligible:
        raise NoCapacityError(retry_after_s=max(min_cooldown, 30))

    # Prefer the requested account if eligible
    if preferred_account_id:
        for acct_name, headroom in eligible:
            if acct_name == preferred_account_id:
                logger.info(f"Sandbox account pick: preferred={acct_name} headroom={headroom:.1f}%")
                return PickedAccount(account_id=acct_name, headroom_percent=headroom)

    # Otherwise pick highest headroom
    best = max(eligible, key=lambda x: x[1])
    logger.info(f"Sandbox account pick: best={best[0]} headroom={best[1]:.1f}%")
    return PickedAccount(account_id=best[0], headroom_percent=best[1])
