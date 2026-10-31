"""Platform-only shared credit ledger. One configured primary for both bridges.

Secrets come from a private manifest, never from the DB. Advisory transaction
locks serialize grant admission across platform processes and worker hosts.
"""
from __future__ import annotations

import hmac
import json
import os
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from src.db.client import get_pool
from src.pricing import price_entry

router = APIRouter(prefix="/v1/internal/api-credit-pool", tags=["internal"])


def authenticate(x_credit_pool_token: str | None = Header(default=None)) -> None:
    expected = os.getenv("ANTHROPIC_CREDIT_POOL_TOKEN", "")
    if not expected or not x_credit_pool_token or not hmac.compare_digest(expected, x_credit_pool_token):
        raise HTTPException(403, "Credit pool authentication required")


def _manifest_data() -> dict:
    path = os.getenv("ANTHROPIC_CREDIT_POOL_ACCOUNTS_FILE", "/app/secrets/api-credit-pool.json")
    try:
        data = json.loads(Path(path).read_text())
        if not isinstance(data.get("accounts"), list) or not data["accounts"]:
            raise ValueError("Missing account inventory")
        return data
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        raise HTTPException(503, "Credit pool manifest unavailable or invalid") from exc


def manifest(data: dict | None = None) -> list[dict]:
    try:
        data = _manifest_data() if data is None else data
        now = datetime.now(timezone.utc)
        accounts = []
        seen_grants = set()
        for a in data["accounts"]:
            # A failed individual refresh cannot be hidden by another account.
            observed = datetime.fromisoformat(a["observed_at"].replace("Z", "+00:00"))
            if now - observed > timedelta(hours=1) or observed > now + timedelta(minutes=1):
                continue
            if not a.get("api_key") or a.get("disabled") or a.get("billing_guard") != "free_only_verified":
                continue
            for g in a["grants"]:
                if g["id"] in seen_grants:
                    raise ValueError("Duplicate credit grant")
                seen_grants.add(g["id"])
                for field in ("granted_usd", "remaining_usd"):
                    value = Decimal(str(g[field]))
                    if not value.is_finite() or value < 0:
                        raise ValueError("Invalid credit balance")
                expires = datetime.fromisoformat(g["expires_at"].replace("Z", "+00:00"))
                if expires <= now + timedelta(hours=1):
                    continue
                # Keep $2 headroom against upstream accounting delays.
                budget = Decimal(str(g["granted_usd"])) - Decimal("2")
                if budget <= 0 or Decimal(str(g["remaining_usd"])) <= 2:
                    continue
                accounts.append({**a, "grant_id": g["id"], "expires_at": g["expires_at"],
                                 "budget": budget, "remaining": Decimal(str(g["remaining_usd"])) - 2,
                                 "observed": observed})
        return sorted(accounts, key=lambda a: a["expires_at"])
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        raise HTTPException(503, "Credit pool manifest unavailable or invalid") from exc


def reservation_cost(model: str, inputs: int, outputs: int) -> Decimal:
    p = price_entry(model)
    if not p or not model.startswith("claude-"):
        raise HTTPException(400, "Unpriced credit-pool model")
    # Upper tier, 1h cache write (2x), plus 10% headroom. count_tokens is
    # authoritative even for images; max_tokens includes thinking output.
    cost = (Decimal(inputs) * Decimal(str(p["in"])) * 2
            + Decimal(outputs) * Decimal(str(p["out"]))) / 1_000_000
    return max(Decimal("0.000001"), (cost * Decimal("1.1")).quantize(Decimal("0.000000001"), rounding=ROUND_CEILING))


def usage_cost(model: str, usage: dict) -> Decimal:
    names = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    if any(type(usage.get(n)) is not int or usage[n] < 0 for n in names):
        raise HTTPException(422, "Incomplete usage; reservation retained")
    # Deliberately upper tier and 1h write rate. Conservative monetary ledger;
    # app billing continues to use its existing exact SSoT independently.
    p = price_entry(model)
    if not p:
        raise HTTPException(422, "Missing price; reservation retained")
    value = (Decimal(usage["input_tokens"]) * Decimal(str(p["in"]))
             + Decimal(usage["output_tokens"]) * Decimal(str(p["out"]))
             + Decimal(usage["cache_read_input_tokens"]) * Decimal(str(p["in"])) * Decimal(str(p.get("cache_read_mult", .1)))
             + Decimal(usage["cache_creation_input_tokens"]) * Decimal(str(p["in"])) * 2) / 1_000_000
    server = usage.get("server_tool_use", {})
    if not isinstance(server, dict):
        raise HTTPException(422, "Unknown server tool usage; reservation retained")
    if any(k not in ("web_search_requests", "web_fetch_requests") for k in server):
        raise HTTPException(422, "Unpriced server usage; reservation retained")
    if any(type(v) is not int or v < 0 for v in server.values()):
        raise HTTPException(422, "Invalid server tool usage; reservation retained")
    value += Decimal(server.get("web_search_requests", 0)) * Decimal("0.01")
    return (value * Decimal("1.1")).quantize(Decimal("0.000000001"), rounding=ROUND_CEILING)


async def available(conn, a: dict) -> Decimal:
    row = await conn.fetchrow("""SELECT COALESCE(SUM(charged_usd),0) AS total,
        COALESCE(SUM(charged_usd) FILTER (WHERE created_at >= $2 OR state='reserved'),0) AS recent
        FROM anthropic_credit_reservations WHERE grant_id=$1""", a["grant_id"], a["observed"] - timedelta(minutes=5))
    # Recent/uncertain costs may not yet be reflected in the console balance.
    return min(a["budget"] - row["total"], a["remaining"] - row["recent"])


@router.get("/accounts", dependencies=[Depends(authenticate)])
async def accounts():
    result = []
    async with get_pool().acquire() as conn:
        for a in manifest():
            if await available(conn, a) > 0:
                result.append({k: a[k] for k in ("id", "org_id", "grant_id", "api_key")})
    return {"accounts": result}


class Reserve(BaseModel):
    id: UUID
    account_id: str
    grant_id: str
    model: str
    input_tokens: int = Field(ge=0, le=32_000_000, strict=True)
    max_tokens: int = Field(gt=0, le=8_000_000, strict=True)


@router.post("/reserve", dependencies=[Depends(authenticate)])
async def reserve(body: Reserve):
    a = next((a for a in manifest() if a["id"] == body.account_id and a["grant_id"] == body.grant_id), None)
    if not a:
        raise HTTPException(409, "Grant expired or unavailable")
    amount = reservation_cost(body.model, body.input_tokens, body.max_tokens)
    async with get_pool().acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", body.grant_id)
        old = await conn.fetchrow("SELECT * FROM anthropic_credit_reservations WHERE id=$1", body.id)
        if old:
            if any(old[k] != getattr(body, k) for k in ("account_id", "grant_id", "model", "input_tokens", "max_tokens")):
                raise HTTPException(409, "Reservation identity conflict")
            return {"id": str(body.id), "state": old["state"]}
        if amount > await available(conn, a):
            raise HTTPException(429, "Insufficient promotional credit budget")
        await conn.execute("""INSERT INTO anthropic_credit_reservations
            (id,account_id,grant_id,model,input_tokens,max_tokens,reserved_usd,charged_usd)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$7)""", body.id, body.account_id, body.grant_id,
                           body.model, body.input_tokens, body.max_tokens, amount)
    return {"id": str(body.id), "state": "reserved"}


class Settle(BaseModel):
    id: UUID
    usage: dict | None = None
    rejected: bool = False


@router.post("/settle", dependencies=[Depends(authenticate)])
async def settle(body: Settle):
    if body.rejected == (body.usage is not None):
        raise HTTPException(422, "Provide usage OR a confirmed rejection")
    async with get_pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT * FROM anthropic_credit_reservations WHERE id=$1 FOR UPDATE", body.id)
        if not row:
            raise HTTPException(404, "Unknown reservation")
        if row["state"] != "reserved":
            return {"id": str(body.id), "state": row["state"]}
        cost = Decimal(0) if body.rejected else usage_cost(row["model"], body.usage)
        state = "released" if body.rejected else "settled"
        await conn.execute("""UPDATE anthropic_credit_reservations SET state=$2, charged_usd=$3,
            settled_at=now(), usage=$4::jsonb WHERE id=$1""", body.id, state, cost, json.dumps(body.usage))
    return {"id": str(body.id), "state": state}


@router.get("/status", dependencies=[Depends(authenticate)])
async def status():
    result = []
    async with get_pool().acquire() as conn:
        for a in manifest():
            row = await conn.fetchrow("""SELECT count(*) AS requests,
                count(*) FILTER (WHERE state='reserved') AS pending,
                COALESCE(sum(charged_usd),0) AS charged FROM anthropic_credit_reservations WHERE grant_id=$1""", a["grant_id"])
            result.append({"account": a["id"], "org_id": a["org_id"], "grant_id": a["grant_id"],
                           "expires_at": a["expires_at"], "available_usd": float(max(Decimal(0), await available(conn, a))),
                           "requests": row["requests"], "pending": row["pending"], "charged_usd": float(row["charged"])})
    return {"accounts": result, "paid_fallback": False}


@router.get("/summary", dependencies=[Depends(authenticate)])
async def summary():
    """One shared monetary reserve, never four additional subscription slots.

    No account identities, organization IDs, cookies or keys leave this route.
    Expired/stale observations do not contribute spendable money.
    """
    data = _manifest_data()
    eligible = manifest(data)
    now = datetime.now(timezone.utc)
    granted = balance = reserved = usable = Decimal(0)
    observed_at, expires_at = [], []
    stale = blocked = 0
    try:
        for account in data["accounts"]:
            observed = datetime.fromisoformat(account["observed_at"].replace("Z", "+00:00"))
            if now - observed > timedelta(hours=1) or observed > now + timedelta(minutes=1):
                stale += 1
                continue
            if account.get("disabled") or not account.get("api_key") or account.get("billing_guard") != "free_only_verified":
                blocked += 1
                continue
            observed_at.append(observed)
            for grant in account["grants"]:
                expiry = datetime.fromisoformat(grant["expires_at"].replace("Z", "+00:00"))
                if expiry <= now + timedelta(hours=1):
                    continue
                amounts = [Decimal(str(grant[k])) for k in ("granted_usd", "remaining_usd")]
                if any(not amount.is_finite() or amount < 0 for amount in amounts):
                    raise ValueError("Invalid credit balance")
                granted += amounts[0]
                balance += amounts[1]
                expires_at.append(expiry)
        async with get_pool().acquire() as conn:
            for account in eligible:
                usable += max(Decimal(0), await available(conn, account))
                reserved += await conn.fetchval(
                    "SELECT COALESCE(sum(charged_usd),0) FROM anthropic_credit_reservations WHERE grant_id=$1 AND state='reserved'",
                    account["grant_id"],
                )
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        raise HTTPException(503, "API reserve summary unavailable") from exc
    state = "available" if usable > 0 else "unavailable" if stale or blocked else "exhausted"
    return {
        "pool_id": "anthropic-subscription-api-reserve", "currency": "USD",
        "shared_across_bridges": True, "state": state,
        "granted_usd": float(granted), "remaining_usd": float(balance),
        "available_usd": float(usable), "reserved_usd": float(reserved),
        "account_count": len(data["accounts"]),
        "eligible_accounts": len({a["id"] for a in eligible}),
        "stale_accounts": stale, "blocked_accounts": blocked,
        "measured_at": min(observed_at).isoformat() if observed_at else None,
        "checked_at": now.isoformat(),
        "next_expiry_at": min(expires_at).isoformat() if expires_at else None,
        "automatic_paid_fallback": False,
        "supported_operations": ["text", "vision"],
        "supports_tools": False, "supports_claude_code": False,
        "requires_stateless_request": True,
    }
