"""Authority view combines both existing metrics readers; it never calls itself."""
import asyncio
import os
import time
import httpx
from .model import ROSTER, overlay

async def runtime_snapshot(policy):
    async def read(house):
        prefix = "BRIDGE_ACCOUNT_POLICY_" + house.upper() + "_POOL"
        url = os.environ.get(prefix + "_URL", "")
        token = os.environ.get(prefix + "_TOKEN", "")
        if not url:
            return house, None, "pool_not_configured"
        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                r = await client.get(url, headers={"X-Bridge-Service-Token": token} if token else {})
            if r.status_code != 200:
                return house, None, "pool_http_" + str(r.status_code)
            data = r.json()
            if not isinstance(data.get("accounts"), dict):
                return house, None, "pool_invalid"
            return house, data["accounts"], None
        except (httpx.HTTPError, ValueError, AttributeError):
            return house, None, "pool_unavailable"
    results = await asyncio.gather(read("partner"), read("dev"))
    if all(rows is None for _, rows, _ in results):
        raise RuntimeError("both account pools unavailable")
    accounts, errors = {}, []
    for house, rows, error in results:
        if error:
            errors.append({"house": house, "reason": error})
        for account, expected_house in ROSTER.items():
            if expected_house != house:
                continue
            info = (rows or {}).get(account)
            if not isinstance(info, dict):
                info = {"available": False, "usage_known": False, "capacity_lock_reason": error or "account_missing"}
            accounts[account] = dict(info, house=house)
    return {"source": "bridge-account-policy", "revision": policy["revision"],
            "ts": int(time.time()), "accounts": overlay(policy, accounts), "errors": errors}
