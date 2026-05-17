"""
Per-User / Per-Tenant / Per-App / Per-Model Usage-Metriken.

Aggregates the activities log into cost-attributed reports for the
Platform-Admin Usage panel. Cost calculation is done on read (cheap-ish on
modest data volumes; switch to pre-aggregated rollups if activities grows
past ~10M rows).

GET /v1/metrics/usage?groupBy=user|tenant|app|model&since=…&until=…&appId=…

Returns:
  rows[]   one row per group key with token counts, call count, EUR cost
  totals   sum across rows
  pricing  model pricing table used for the calculation
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from src.api_auth import require_admin, AuthClaims
from src.db.client import get_pool

router = APIRouter(prefix="/v1/metrics", tags=["metrics"])

_ALLOWED_GROUP_BY = {"user", "tenant", "app", "model"}
_ALLOWED_APP_IDS = {
    "werking-report", "werking-energy", "werking-safety",
    "werking-noise", "engelmann",
}

# Per-1M-token pricing in USD, defaults match Anthropic public list-prices.
# Override via env: MODEL_PRICING_JSON='{"claude-opus-4":{"in":15,"out":75}}'
_DEFAULT_PRICING = {
    "claude-sonnet-4-5":            {"in": 3.00,  "out": 15.00},
    "claude-sonnet-4-5-20250929":   {"in": 3.00,  "out": 15.00},
    "claude-sonnet-4-6":            {"in": 3.00,  "out": 15.00},
    "claude-opus-4":                {"in": 15.00, "out": 75.00},
    "claude-opus-4-7":              {"in": 15.00, "out": 75.00},
    "claude-haiku-4-5":             {"in": 1.00,  "out": 5.00},
    "claude-haiku-4-5-20251001":    {"in": 1.00,  "out": 5.00},
    "gpt-5":                        {"in": 5.00,  "out": 15.00},
    "gpt-5-mini":                   {"in": 0.30,  "out": 1.20},
}


def _load_pricing() -> Dict[str, Dict[str, float]]:
    """Merge defaults with env override. Env keys are model IDs."""
    pricing = dict(_DEFAULT_PRICING)
    override = os.environ.get("MODEL_PRICING_JSON", "")
    if override:
        import json as _json
        try:
            pricing.update(_json.loads(override))
        except Exception:
            pass
    return pricing


def _usd_to_eur_rate() -> float:
    """Static USD→EUR for invoice predictability. Override via env, default 0.92."""
    try:
        return float(os.environ.get("USD_TO_EUR_RATE", "0.92"))
    except Exception:
        return 0.92


def _model_cost_eur(model: Optional[str], prompt_tokens: int, completion_tokens: int,
                    pricing: Dict[str, Dict[str, float]], rate: float) -> float:
    """Return EUR cost for one call. Unknown models → 0 (logged on aggregate level)."""
    if not model or model not in pricing:
        return 0.0
    p = pricing[model]
    usd = (prompt_tokens / 1_000_000.0) * p["in"] + (completion_tokens / 1_000_000.0) * p["out"]
    return round(usd * rate, 4)


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid ISO timestamp: {s}")


@router.get("/usage")
async def usage_metrics(
    groupBy: str = Query("user", description="user|tenant|app|model"),
    since: Optional[str] = Query(None, description="ISO timestamp — default: 30 days ago"),
    until: Optional[str] = Query(None, description="ISO timestamp — default: now"),
    appId: Optional[str] = Query(None),
    tenantId: Optional[str] = Query(None),
    userId: Optional[str] = Query(None),
    mode: Optional[str] = Query(None, description="prod|staging|local — filter by app_env (X-App-Env)"),
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Aggregate AI-call activities with EUR cost. Admin only — exposes
    cross-tenant data and pricing.
    """
    if mode and mode not in ("prod", "staging", "local"):
        raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}")
    if groupBy not in _ALLOWED_GROUP_BY:
        raise HTTPException(status_code=400, detail=f"groupBy must be one of {sorted(_ALLOWED_GROUP_BY)}")
    if appId and appId not in _ALLOWED_APP_IDS:
        raise HTTPException(status_code=400, detail=f"Unknown appId: {appId}")

    until_dt = _parse_iso(until) or datetime.now(timezone.utc)
    since_dt = _parse_iso(since) or (until_dt - timedelta(days=30))

    # Activity columns stay qualified as `a.*` for readability.
    where = ["a.category = 'workflow'", "a.event_type LIKE 'ai-call:%' OR a.event_type LIKE 'ai-call-error:%'",
             "a.timestamp >= $1", "a.timestamp <= $2"]
    args: List[Any] = [since_dt, until_dt]

    def _add(cond: str, val: Any) -> None:
        args.append(val)
        where.append(cond.replace("$$", f"${len(args)}"))

    if appId:
        _add("a.app_id = $$", appId)
    if tenantId:
        _add("a.tenant_id = $$", tenantId)
    if userId:
        import uuid as _uuid
        try:
            uid = _uuid.UUID(userId)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid userId UUID: {userId}")
        _add("a.actor_user_id = $$", uid)

    # "mode" filters by the environment the call came from
    # (X-App-Env → activities.app_env), not the customer's tenant.category.
    # NULL app_env (pre-migration / no header) is excluded when filtered.
    if mode:
        args.append(mode)
        where.append(f"a.app_env = ${len(args)}::app_env")

    where_sql = " AND ".join(f"({cx})" if " OR " in cx else cx for cx in where)
    sql = f"""
        SELECT
          a.actor_user_id, a.tenant_id, a.app_id, a.event_type,
          a.payload->>'model'                       AS model,
          (a.payload->>'promptTokens')::bigint      AS prompt_tokens,
          (a.payload->>'completionTokens')::bigint  AS completion_tokens,
          (a.payload->>'totalTokens')::bigint       AS total_tokens
        FROM activities a
        WHERE {where_sql}
    """

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)

    pricing = _load_pricing()
    eur_rate = _usd_to_eur_rate()

    # Aggregate in Python — keeps SQL simple and lets us compute cost with the
    # in-process pricing table.
    buckets: Dict[Optional[str], Dict[str, Any]] = {}
    totals = {"calls": 0, "errors": 0, "promptTokens": 0, "completionTokens": 0,
              "totalTokens": 0, "estimatedCostEur": 0.0}
    unknown_models: Dict[str, int] = {}

    for r in rows:
        if groupBy == "user":
            key = str(r["actor_user_id"]) if r["actor_user_id"] else None
        elif groupBy == "tenant":
            key = r["tenant_id"]
        elif groupBy == "app":
            key = r["app_id"]
        else:  # model
            key = r["model"]

        b = buckets.setdefault(key, {
            "key": key, "calls": 0, "errors": 0,
            "promptTokens": 0, "completionTokens": 0, "totalTokens": 0,
            "estimatedCostEur": 0.0,
            "byApp": {}, "byModel": {},
        })

        pt = int(r["prompt_tokens"] or 0)
        ct = int(r["completion_tokens"] or 0)
        tt = int(r["total_tokens"] or (pt + ct))
        is_err = (r["event_type"] or "").startswith("ai-call-error:")
        model = r["model"]
        cost = _model_cost_eur(model, pt, ct, pricing, eur_rate) if not is_err else 0.0

        b["calls"] += 1
        if is_err:
            b["errors"] += 1
        b["promptTokens"] += pt
        b["completionTokens"] += ct
        b["totalTokens"] += tt
        b["estimatedCostEur"] = round(b["estimatedCostEur"] + cost, 4)

        if r["app_id"]:
            b["byApp"][r["app_id"]] = b["byApp"].get(r["app_id"], 0) + 1
        if model:
            mb = b["byModel"].setdefault(model, {"calls": 0, "totalTokens": 0, "estimatedCostEur": 0.0})
            mb["calls"] += 1
            mb["totalTokens"] += tt
            mb["estimatedCostEur"] = round(mb["estimatedCostEur"] + cost, 4)
            if model not in pricing:
                unknown_models[model] = unknown_models.get(model, 0) + 1

        totals["calls"] += 1
        if is_err:
            totals["errors"] += 1
        totals["promptTokens"] += pt
        totals["completionTokens"] += ct
        totals["totalTokens"] += tt
        totals["estimatedCostEur"] = round(totals["estimatedCostEur"] + cost, 4)

    # Resolve labels (email for user, plain key for others).
    if groupBy == "user":
        user_ids = [k for k in buckets.keys() if k]
        if user_ids:
            import uuid as _uuid
            async with pool.acquire() as conn:
                user_rows = await conn.fetch(
                    "SELECT id, email, name FROM users WHERE id = ANY($1::uuid[])",
                    [_uuid.UUID(u) for u in user_ids],
                )
            user_map = {str(u["id"]): u for u in user_rows}
            for k, b in buckets.items():
                if k and k in user_map:
                    b["label"] = user_map[k]["email"] or user_map[k]["name"] or k
                else:
                    b["label"] = "(unknown user)" if k else "(unattributed)"
        else:
            for b in buckets.values():
                b["label"] = "(unattributed)"
    else:
        for k, b in buckets.items():
            b["label"] = k or "(unattributed)"

    # Sort by EUR cost descending — most expensive groups first.
    out_rows = sorted(buckets.values(), key=lambda x: x["estimatedCostEur"], reverse=True)

    return {
        "groupBy": groupBy,
        "since": since_dt.isoformat(),
        "until": until_dt.isoformat(),
        "rows": out_rows,
        "totals": totals,
        "pricing": {
            "modelsKnown": list(pricing.keys()),
            "modelsUnknown": unknown_models,
            "usdToEurRate": eur_rate,
            "rates": pricing,
        },
    }


@router.get("/timeseries")
async def usage_timeseries(
    bucket: str = Query("day", description="day|hour"),
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    appId: Optional[str] = Query(None),
    mode: Optional[str] = Query(None, description="prod|staging|local"),
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Time-bucketed usage for charts. Buckets calls + EUR cost per day or hour.
    """
    if mode and mode not in ("prod", "staging", "local"):
        raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}")
    if bucket not in ("day", "hour"):
        raise HTTPException(status_code=400, detail="bucket must be 'day' or 'hour'")
    if appId and appId not in _ALLOWED_APP_IDS:
        raise HTTPException(status_code=400, detail=f"Unknown appId: {appId}")

    until_dt = _parse_iso(until) or datetime.now(timezone.utc)
    default_lookback = timedelta(days=30 if bucket == "day" else 2)
    since_dt = _parse_iso(since) or (until_dt - default_lookback)

    # Activity columns stay qualified as `a.*` for readability.
    where = ["a.category = 'workflow'",
             "(a.event_type LIKE 'ai-call:%' OR a.event_type LIKE 'ai-call-error:%')",
             "a.timestamp >= $1", "a.timestamp <= $2"]
    args: List[Any] = [since_dt, until_dt]
    if appId:
        args.append(appId)
        where.append(f"a.app_id = ${len(args)}")

    # "mode" filters by the environment the call came from
    # (X-App-Env → activities.app_env), not the customer's tenant.category.
    # NULL app_env (pre-migration / no header) is excluded when filtered.
    if mode:
        args.append(mode)
        where.append(f"a.app_env = ${len(args)}::app_env")

    where_sql = " AND ".join(where)
    sql = f"""
        SELECT
          date_trunc('{bucket}', a.timestamp) AS bucket_ts,
          a.app_id,
          a.payload->>'model'                       AS model,
          (a.payload->>'promptTokens')::bigint      AS prompt_tokens,
          (a.payload->>'completionTokens')::bigint  AS completion_tokens,
          a.event_type
        FROM activities a
        WHERE {where_sql}
        ORDER BY bucket_ts ASC
    """

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)

    pricing = _load_pricing()
    eur_rate = _usd_to_eur_rate()

    series: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        b_key = r["bucket_ts"].isoformat()
        b = series.setdefault(b_key, {
            "bucket": b_key, "calls": 0, "errors": 0,
            "totalTokens": 0, "estimatedCostEur": 0.0,
        })
        pt = int(r["prompt_tokens"] or 0)
        ct = int(r["completion_tokens"] or 0)
        is_err = (r["event_type"] or "").startswith("ai-call-error:")
        b["calls"] += 1
        if is_err:
            b["errors"] += 1
        b["totalTokens"] += pt + ct
        b["estimatedCostEur"] = round(
            b["estimatedCostEur"] + (_model_cost_eur(r["model"], pt, ct, pricing, eur_rate) if not is_err else 0.0),
            4,
        )

    return {
        "bucket": bucket,
        "since": since_dt.isoformat(),
        "until": until_dt.isoformat(),
        "series": sorted(series.values(), key=lambda x: x["bucket"]),
        "usdToEurRate": eur_rate,
    }

# mode_filter applied
