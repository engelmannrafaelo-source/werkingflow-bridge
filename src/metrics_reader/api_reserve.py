"""Credential-free view of the single shared API reserve for capacity clients."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
import os
import threading
import time
import urllib.request

_lock = threading.Lock()
_cached: tuple[float, dict] | None = None
_FIELDS = {
    "pool_id", "currency", "shared_across_bridges", "state", "granted_usd",
    "remaining_usd", "available_usd", "reserved_usd", "account_count",
    "eligible_accounts", "stale_accounts", "blocked_accounts", "measured_at",
    "checked_at", "next_expiry_at", "automatic_paid_fallback",
    "supported_operations", "supports_tools", "supports_claude_code",
    "requires_stateless_request",
}


def api_reserve_snapshot() -> dict:
    global _cached
    with _lock:
        if _cached and time.monotonic() - _cached[0] < 10:
            return dict(_cached[1])
        url = os.getenv("ANTHROPIC_CREDIT_POOL_URL", "").rstrip("/")
        token = os.getenv("ANTHROPIC_CREDIT_POOL_TOKEN", "")
        result = {"pool_id": None, "currency": "USD", "available_usd": None,
                  "automatic_paid_fallback": False, "shared_across_bridges": True,
                  "active_on_this_bridge": bool(url and token),
                  "state": "unavailable" if url and token else "not_configured"}
        if url and token:
            try:
                req = urllib.request.Request(url + "/v1/internal/api-credit-pool/summary",
                                             headers={"X-Credit-Pool-Token": token})
                with urllib.request.urlopen(req, timeout=4) as response:
                    data = json.load(response)
                if (not isinstance(data, dict) or data.get("currency") != "USD"
                        or data.get("automatic_paid_fallback") is not False
                        or data.get("state") not in ("available", "exhausted", "unavailable")):
                    raise ValueError("Invalid reserve status")
                for field in ("granted_usd", "remaining_usd", "available_usd", "reserved_usd",
                              "account_count", "eligible_accounts", "stale_accounts", "blocked_accounts"):
                    value = data.get(field)
                    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                        raise ValueError("Invalid reserve amount")
                if (data.get("shared_across_bridges") is not True
                        or data.get("supports_tools") is not False
                        or data.get("supports_claude_code") is not False
                        or data.get("requires_stateless_request") is not True
                        or data.get("supported_operations") != ["text", "vision"]
                        or data.get("pool_id") != "anthropic-subscription-api-reserve"):
                    raise ValueError("Invalid reserve capabilities")
                now = datetime.now(timezone.utc)
                checked = datetime.fromisoformat(data["checked_at"].replace("Z", "+00:00"))
                if not -60 <= (now - checked).total_seconds() <= 60:
                    raise ValueError("Stale reserve response")
                if data["state"] == "available":
                    observed = datetime.fromisoformat(data["measured_at"].replace("Z", "+00:00"))
                    if not -60 <= (now - observed).total_seconds() <= 3600:
                        raise ValueError("Stale reserve balance")
                result.update({k: data[k] for k in _FIELDS if k in data})
            except Exception:
                # Never serve yesterday's money as available; never return a
                # coordinator response body (it could contain credentials).
                result["reason"] = "credit_coordinator_unavailable"
        _cached = (time.monotonic(), result)
        return dict(result)
