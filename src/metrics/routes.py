"""
Per-User / Per-Tenant / Per-App / Per-Model Usage-Metriken.

Reads from the usage_events ledger — the unified source for workflow +
sandbox + chat usage. The activities table remains the audit trail for
/v1/activity/query; this module does not touch it.

GET /v1/metrics/usage
    groupBy     user|tenant|app|model
    since/until ISO timestamps  (default: last 30 days)
    appId       filter by app
    tenantId    filter by tenant
    userId      filter by user (UUID)
    mode        prod|staging|local  — filter by app_env (X-App-Env)
    source      workflow|sandbox|chat — filter by source type (default: all)

Returns per-bucket rows with:
  - calls / errors
  - promptTokens / completionTokens / totalTokens  (backward-compat names)
  - cacheReadTokens / cacheCreationTokens           (new, cache-specific)
  - realCostEur                                     (0 for flat_rate_estimated)
  - hypotheticalCostEur                             (pay-per-token rate for everyone)
  - estimatedCostEur                                (alias for hypotheticalCostEur)
  - byApp / byModel / bySource                      (drill-down counters)

GET /v1/metrics/timeseries
    bucket      day|hour
    since/until ISO timestamps
    appId       filter by app
    mode        prod|staging|local
    source      workflow|sandbox|chat
"""
from __future__ import annotations

import uuid as _uuid_module
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api_auth import require_admin, AuthClaims
from src.db.client import get_pool
from src.pricing import load_pricing as _load_pricing, usd_to_eur_rate as _usd_to_eur_rate

router = APIRouter(prefix="/v1/metrics", tags=["metrics"])

_ALLOWED_GROUP_BY = {"user", "tenant", "app", "model"}
_ALLOWED_APP_IDS = {
    "werking-report", "werking-energy", "werking-safety",
    "werking-noise", "engelmann",
}
_ALLOWED_SOURCES = {"workflow", "sandbox", "chat"}
# usage_events.provider values the API accepts as filter. Matches what the
# request paths actually book (ai_call_writer default + the Bedrock branch).
_ALLOWED_PROVIDERS = {"anthropic", "bedrock"}


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
    mode: Optional[str] = Query(None, description="prod|staging|local — filter by app_env"),
    source: Optional[str] = Query(None, description="workflow|sandbox|chat (default: all)"),
    provider: Optional[str] = Query(None, description="anthropic|bedrock — filter by serving backend (default: all)"),
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Aggregate usage_events ledger. Admin only.
    Covers workflow + sandbox + chat in one view with real vs. hypothetical costs.
    """
    if mode and mode not in ("prod", "staging", "local", "all"):
        raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}")
    if groupBy not in _ALLOWED_GROUP_BY:
        raise HTTPException(status_code=400, detail=f"groupBy must be one of {sorted(_ALLOWED_GROUP_BY)}")
    if appId and appId not in _ALLOWED_APP_IDS:
        raise HTTPException(status_code=400, detail=f"Unknown appId: {appId}")
    if source and source not in _ALLOWED_SOURCES:
        raise HTTPException(status_code=400, detail=f"source must be one of {sorted(_ALLOWED_SOURCES)}")
    if provider and provider not in _ALLOWED_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"provider must be one of {sorted(_ALLOWED_PROVIDERS)}")

    until_dt = _parse_iso(until) or datetime.now(timezone.utc)
    since_dt = _parse_iso(since) or (until_dt - timedelta(days=30))

    where: List[str] = ["u.recorded_at >= $1", "u.recorded_at <= $2"]
    args: List[Any] = [since_dt, until_dt]

    def _add(cond: str, val: Any) -> None:
        args.append(val)
        where.append(cond.replace("$$", f"${len(args)}"))

    if appId:
        _add("u.app = $$", appId)
    if tenantId:
        _add("u.tenant_id = $$", tenantId)
    if userId:
        try:
            uid = _uuid_module.UUID(userId)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid userId UUID: {userId}")
        _add("u.user_id = $$", uid)
    if mode and mode != "all":
        args.append(mode)
        where.append(f"u.app_env = ${len(args)}::app_env")
    if source:
        args.append(source)
        where.append(f"u.source = ${len(args)}::usage_source")
    if provider:
        _add("u.provider = $$", provider)

    where_sql = " AND ".join(where)
    sql = f"""
        SELECT
            u.user_id, u.tenant_id, u.app, u.model,
            u.source::text                                      AS source,
            u.provider,
            u.input_tokens, u.output_tokens,
            u.cache_read_tokens, u.cache_creation_tokens,
            u.real_cost_eur, u.hypothetical_cost_eur,
            COALESCE((u.provider_metadata->>'status') = 'error', false) AS is_error
        FROM usage_events u
        WHERE {where_sql}
    """

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)

    pricing = _load_pricing()
    eur_rate = _usd_to_eur_rate()

    buckets: Dict[Optional[str], Dict[str, Any]] = {}
    totals: Dict[str, Any] = {
        "calls": 0, "errors": 0,
        # backward-compat names kept alongside new names
        "promptTokens": 0, "completionTokens": 0, "totalTokens": 0,
        "cacheReadTokens": 0, "cacheCreationTokens": 0,
        "realCostEur": 0.0, "hypotheticalCostEur": 0.0, "estimatedCostEur": 0.0,
    }
    seen_models: set[str] = set()
    totals_by_provider: Dict[str, Dict[str, Any]] = {}

    for r in rows:
        if groupBy == "user":
            key = str(r["user_id"]) if r["user_id"] else None
        elif groupBy == "tenant":
            key = r["tenant_id"]
        elif groupBy == "app":
            key = r["app"]
        else:  # model
            key = r["model"]

        b = buckets.setdefault(key, {
            "key": key, "calls": 0, "errors": 0,
            "promptTokens": 0, "completionTokens": 0, "totalTokens": 0,
            "cacheReadTokens": 0, "cacheCreationTokens": 0,
            "realCostEur": 0.0, "hypotheticalCostEur": 0.0, "estimatedCostEur": 0.0,
            "byApp": {}, "byModel": {}, "bySource": {}, "byProvider": {},
        })

        it = int(r["input_tokens"] or 0)
        ot = int(r["output_tokens"] or 0)
        crt = int(r["cache_read_tokens"] or 0)
        cct = int(r["cache_creation_tokens"] or 0)
        tt = it + ot
        real = float(r["real_cost_eur"] or 0)
        hyp = float(r["hypothetical_cost_eur"] or 0)
        is_err = bool(r["is_error"])
        src = r["source"] or "workflow"
        model = r["model"]

        if model:
            seen_models.add(model)

        b["calls"] += 1
        if is_err:
            b["errors"] += 1
        b["promptTokens"] += it
        b["completionTokens"] += ot
        b["totalTokens"] += tt
        b["cacheReadTokens"] += crt
        b["cacheCreationTokens"] += cct
        b["realCostEur"] = round(b["realCostEur"] + real, 6)
        b["hypotheticalCostEur"] = round(b["hypotheticalCostEur"] + hyp, 6)
        b["estimatedCostEur"] = b["hypotheticalCostEur"]

        if r["app"]:
            b["byApp"][r["app"]] = b["byApp"].get(r["app"], 0) + 1
        if model:
            mb = b["byModel"].setdefault(model, {
                "calls": 0, "totalTokens": 0,
                "realCostEur": 0.0, "hypotheticalCostEur": 0.0, "estimatedCostEur": 0.0,
            })
            mb["calls"] += 1
            mb["totalTokens"] += tt
            mb["realCostEur"] = round(mb["realCostEur"] + real, 6)
            mb["hypotheticalCostEur"] = round(mb["hypotheticalCostEur"] + hyp, 6)
            mb["estimatedCostEur"] = mb["hypotheticalCostEur"]
        sb = b["bySource"].setdefault(src, {
            "calls": 0, "tokens": 0, "realCostEur": 0.0, "hypotheticalCostEur": 0.0,
        })
        sb["calls"] += 1
        sb["tokens"] += tt
        sb["realCostEur"] = round(sb["realCostEur"] + real, 6)
        sb["hypotheticalCostEur"] = round(sb["hypotheticalCostEur"] + hyp, 6)

        prov = r["provider"] or "anthropic"
        pb = b["byProvider"].setdefault(prov, {
            "calls": 0, "tokens": 0, "realCostEur": 0.0, "hypotheticalCostEur": 0.0,
        })
        pb["calls"] += 1
        pb["tokens"] += tt
        pb["realCostEur"] = round(pb["realCostEur"] + real, 6)
        pb["hypotheticalCostEur"] = round(pb["hypotheticalCostEur"] + hyp, 6)

        tp = totals_by_provider.setdefault(prov, {
            "calls": 0, "tokens": 0, "realCostEur": 0.0, "hypotheticalCostEur": 0.0,
        })
        tp["calls"] += 1
        tp["tokens"] += tt
        tp["realCostEur"] = round(tp["realCostEur"] + real, 6)
        tp["hypotheticalCostEur"] = round(tp["hypotheticalCostEur"] + hyp, 6)

        totals["calls"] += 1
        if is_err:
            totals["errors"] += 1
        totals["promptTokens"] += it
        totals["completionTokens"] += ot
        totals["totalTokens"] += tt
        totals["cacheReadTokens"] += crt
        totals["cacheCreationTokens"] += cct
        totals["realCostEur"] = round(totals["realCostEur"] + real, 6)
        totals["hypotheticalCostEur"] = round(totals["hypotheticalCostEur"] + hyp, 6)
        totals["estimatedCostEur"] = totals["hypotheticalCostEur"]

    # Resolve user labels.
    if groupBy == "user":
        user_ids = [k for k in buckets.keys() if k]
        if user_ids:
            async with pool.acquire() as conn:
                user_rows = await conn.fetch(
                    "SELECT id, email, name FROM users WHERE id = ANY($1::uuid[])",
                    [_uuid_module.UUID(u) for u in user_ids],
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

    out_rows = sorted(buckets.values(), key=lambda x: x["hypotheticalCostEur"], reverse=True)

    # pricing block kept for backward compat — UI reads usdToEurRate + modelsUnknown
    unknown_models = {m: 1 for m in seen_models if m not in pricing}

    return {
        "groupBy": groupBy,
        "since": since_dt.isoformat(),
        "until": until_dt.isoformat(),
        "rows": out_rows,
        "totals": totals,
        "totalsByProvider": totals_by_provider,
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
    source: Optional[str] = Query(None, description="workflow|sandbox|chat (default: all)"),
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Time-bucketed usage for charts. Buckets calls + EUR cost per day or hour.
    Reads from usage_events — covers workflow + sandbox + chat.
    """
    if mode and mode not in ("prod", "staging", "local", "all"):
        raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}")
    if bucket not in ("day", "hour"):
        raise HTTPException(status_code=400, detail="bucket must be 'day' or 'hour'")
    if appId and appId not in _ALLOWED_APP_IDS:
        raise HTTPException(status_code=400, detail=f"Unknown appId: {appId}")
    if source and source not in _ALLOWED_SOURCES:
        raise HTTPException(status_code=400, detail=f"source must be one of {sorted(_ALLOWED_SOURCES)}")

    until_dt = _parse_iso(until) or datetime.now(timezone.utc)
    default_lookback = timedelta(days=30 if bucket == "day" else 2)
    since_dt = _parse_iso(since) or (until_dt - default_lookback)

    where: List[str] = ["u.recorded_at >= $1", "u.recorded_at <= $2"]
    args: List[Any] = [since_dt, until_dt]
    if appId:
        args.append(appId)
        where.append(f"u.app = ${len(args)}")
    if mode and mode != "all":
        args.append(mode)
        where.append(f"u.app_env = ${len(args)}::app_env")
    if source:
        args.append(source)
        where.append(f"u.source = ${len(args)}::usage_source")

    where_sql = " AND ".join(where)
    sql = f"""
        SELECT
            date_trunc('{bucket}', u.recorded_at)               AS bucket_ts,
            u.app, u.model,
            u.input_tokens, u.output_tokens,
            u.real_cost_eur, u.hypothetical_cost_eur,
            COALESCE((u.provider_metadata->>'status') = 'error', false) AS is_error
        FROM usage_events u
        WHERE {where_sql}
        ORDER BY bucket_ts ASC
    """

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)

    eur_rate = _usd_to_eur_rate()

    series: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        b_key = r["bucket_ts"].isoformat()
        b = series.setdefault(b_key, {
            "bucket": b_key, "calls": 0, "errors": 0,
            "totalTokens": 0,
            "realCostEur": 0.0, "hypotheticalCostEur": 0.0, "estimatedCostEur": 0.0,
        })
        it = int(r["input_tokens"] or 0)
        ot = int(r["output_tokens"] or 0)
        is_err = bool(r["is_error"])
        b["calls"] += 1
        if is_err:
            b["errors"] += 1
        b["totalTokens"] += it + ot
        b["realCostEur"] = round(b["realCostEur"] + float(r["real_cost_eur"] or 0), 6)
        b["hypotheticalCostEur"] = round(b["hypotheticalCostEur"] + float(r["hypothetical_cost_eur"] or 0), 6)
        b["estimatedCostEur"] = b["hypotheticalCostEur"]

    return {
        "bucket": bucket,
        "since": since_dt.isoformat(),
        "until": until_dt.isoformat(),
        "series": sorted(series.values(), key=lambda x: x["bucket"]),
        "usdToEurRate": eur_rate,
    }


# =============================================================================
# Bedrock 1:1 reconciliation — our ledger vs. AWS CloudWatch token counts
# =============================================================================

@router.get("/bedrock-reconciliation")
async def get_bedrock_reconciliation(
    days: int = Query(default=30, ge=1, le=365),
    claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """Daily reconciliation rows (newest first). status != 'ok' needs eyes:
    'drift' = token counts diverge beyond tolerance, 'aws_unavailable' =
    bridge sums stored but unverified against AWS."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT day, bedrock_model_id, region,
                   bridge_calls, bridge_input_tokens, bridge_output_tokens,
                   aws_input_tokens, aws_output_tokens,
                   input_diff_pct, output_diff_pct,
                   status, detail, checked_at
            FROM bedrock_reconciliation
            WHERE day >= (CURRENT_DATE - $1::int)
            ORDER BY day DESC, bedrock_model_id, region
            """,
            days,
        )
    items = [
        {
            "day": r["day"].isoformat(),
            "bedrockModelId": r["bedrock_model_id"],
            "region": r["region"],
            "bridgeCalls": r["bridge_calls"],
            "bridgeInputTokens": r["bridge_input_tokens"],
            "bridgeOutputTokens": r["bridge_output_tokens"],
            "awsInputTokens": r["aws_input_tokens"],
            "awsOutputTokens": r["aws_output_tokens"],
            "inputDiffPct": r["input_diff_pct"],
            "outputDiffPct": r["output_diff_pct"],
            "status": r["status"],
            "detail": r["detail"],
            "checkedAt": r["checked_at"].isoformat(),
        }
        for r in rows
    ]
    not_ok = sum(1 for i in items if i["status"] != "ok")
    return {"days": days, "notOkCount": not_ok, "items": items}


@router.post("/bedrock-reconciliation/run")
async def run_bedrock_reconciliation(
    day: Optional[str] = Query(default=None, description="UTC day YYYY-MM-DD, default: yesterday"),
    claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """Manually (re-)reconcile one day — e.g. to verify a fresh AWS invoice."""
    from datetime import date as _date
    from src.reconciliation import reconcile_bedrock_day

    target = _date.fromisoformat(day) if day else None
    try:
        results = await reconcile_bedrock_day(target)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"day": day or "yesterday", "items": results}
