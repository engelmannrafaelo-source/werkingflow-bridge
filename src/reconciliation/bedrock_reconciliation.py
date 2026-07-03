"""Bedrock 1:1 billing reconciliation — our ledger vs. AWS's ledger.

Business requirement (Rafael): Bedrock traffic is paid per token against the
company AWS account, so every token billed to a user must be PROVABLY the
token paid to AWS. This module makes drift a queryable fact instead of an
end-of-month invoice surprise:

  bridge side  = SUM(usage_events) WHERE provider='bedrock'  per day/model/region
  aws side     = CloudWatch AWS/Bedrock InputTokenCount/OutputTokenCount
                 (daily Sum, per ModelId dimension, queried in-region)

Both directions are covered:
  * models WE tracked        → compared against AWS's counts (billing drift)
  * models only AWS reports  → bridge_calls=0 rows flagged as drift — tokens
    AWS will invoice that never reached OUR ledger (worst case: untracked
    usage, e.g. a code path that bypasses persist_ai_call_activity)

Results are upserted into bedrock_reconciliation (migration 033). Drift beyond
BEDROCK_RECONCILIATION_TOLERANCE_PCT (default 0.5%) logs an ERROR — loud in
the operator feed, per defensive-programming policy.

The loop only runs where it can do something: Bedrock credentials configured
AND the bridge DB present. CloudWatch failures are recorded as
'aws_unavailable' rows — visibly unverified, never silently ok.
"""

from __future__ import annotations

import asyncio
import os
import logging
from datetime import datetime, date, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Relative drift tolerance in percent. CloudWatch sums and our per-call usage
# events should match exactly for invoke_model; a small tolerance absorbs
# day-boundary skew (a call spanning midnight lands in different buckets).
_DEFAULT_TOLERANCE_PCT = 0.5

# How often the loop re-checks (it always reconciles the closed previous UTC
# day; the upsert makes re-runs idempotent).
_LOOP_INTERVAL_SECONDS = 6 * 3600


def _tolerance_pct() -> float:
    try:
        return float(os.environ.get("BEDROCK_RECONCILIATION_TOLERANCE_PCT", str(_DEFAULT_TOLERANCE_PCT)))
    except (TypeError, ValueError):
        return _DEFAULT_TOLERANCE_PCT


async def _bridge_daily_sums(day: date) -> Dict[Tuple[str, str], Dict[str, int]]:
    """Our ledger: (bedrock_model_id, region) → calls/input/output sums for `day` (UTC)."""
    from src.db.client import get_pool

    start = datetime.combine(day, dtime.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                COALESCE(provider_metadata->>'bedrock_model_id', model) AS bedrock_model_id,
                COALESCE(region, provider_metadata->>'region', 'unknown') AS region,
                COUNT(*)                                   AS calls,
                COALESCE(SUM(input_tokens), 0)::BIGINT     AS input_tokens,
                COALESCE(SUM(output_tokens), 0)::BIGINT    AS output_tokens
            FROM usage_events
            WHERE provider = 'bedrock'
              AND recorded_at >= $1
              AND recorded_at < $2
            GROUP BY 1, 2
            """,
            start, end,
        )

    return {
        (r["bedrock_model_id"], r["region"]): {
            "calls": r["calls"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
        }
        for r in rows
    }


def _cloudwatch_client(region: str):
    """boto3 CloudWatch client with the same credentials as the Bedrock calls."""
    import boto3
    from src.auth import bedrock_credential_manager as bcm

    return boto3.client(
        "cloudwatch",
        region_name=region,
        aws_access_key_id=bcm.aws_access_key,
        aws_secret_access_key=bcm.aws_secret_key,
    )


def _aws_token_sum(cw_client, metric_name: str, model_id: str, start: datetime, end: datetime) -> int:
    """Daily Sum of one AWS/Bedrock token metric for one ModelId. 0 = no datapoints."""
    resp = cw_client.get_metric_statistics(
        Namespace="AWS/Bedrock",
        MetricName=metric_name,
        Dimensions=[{"Name": "ModelId", "Value": model_id}],
        StartTime=start,
        EndTime=end,
        Period=86400,
        Statistics=["Sum"],
    )
    return int(sum(dp.get("Sum", 0) for dp in resp.get("Datapoints", [])))


def _aws_active_model_ids(cw_client) -> List[str]:
    """ModelIds CloudWatch has recently seen ANY invocations for (~2 week lookback).

    Catches the dangerous direction: AWS-side usage our ledger never saw.
    """
    model_ids: List[str] = []
    paginator = cw_client.get_paginator("list_metrics")
    for page in paginator.paginate(Namespace="AWS/Bedrock", MetricName="Invocations"):
        for metric in page.get("Metrics", []):
            for dim in metric.get("Dimensions", []):
                if dim.get("Name") == "ModelId":
                    model_ids.append(dim["Value"])
    return sorted(set(model_ids))


async def reconcile_bedrock_day(day: Optional[date] = None) -> List[Dict[str, Any]]:
    """Reconcile one UTC day (default: yesterday). Returns the result rows.

    Raises RuntimeError when preconditions are missing (no DB) — callers that
    poll (the loop) check preconditions first; the manual endpoint surfaces it.
    """
    from src.db.client import is_db_enabled, get_pool
    from src.auth import bedrock_credential_manager as bcm

    if not is_db_enabled():
        raise RuntimeError("bedrock reconciliation needs the bridge DB (BRIDGE_DB_URL)")

    if day is None:
        day = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    start = datetime.combine(day, dtime.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    tolerance = _tolerance_pct()

    bridge = await _bridge_daily_sums(day)

    # Regions to query = regions we saw traffic in + the default region.
    regions = {r for (_m, r) in bridge.keys() if r and r != "unknown"}
    if bcm.is_configured() and bcm.default_region:
        regions.add(bcm.default_region)

    # (model, region) → aws sums; None = CloudWatch unavailable for that region
    aws: Dict[Tuple[str, str], Optional[Dict[str, int]]] = {}
    aws_configured = bcm.is_configured()

    def _gather_aws_for_region(region: str, tracked_here: set) -> Dict[str, Dict[str, int]]:
        """Sync boto3 work for one region — run via to_thread off the event loop."""
        cw = _cloudwatch_client(region)
        # Union: models we tracked in this region + models AWS saw.
        aws_seen = set(_aws_active_model_ids(cw))
        return {
            model_id: {
                "input_tokens": _aws_token_sum(cw, "InputTokenCount", model_id, start, end),
                "output_tokens": _aws_token_sum(cw, "OutputTokenCount", model_id, start, end),
            }
            for model_id in sorted(tracked_here | aws_seen)
        }

    if aws_configured:
        for region in sorted(regions):
            tracked_here = {m for (m, r) in bridge.keys() if r == region}
            try:
                region_sums = await asyncio.to_thread(_gather_aws_for_region, region, tracked_here)
                for model_id, sums in region_sums.items():
                    aws[(model_id, region)] = sums
            except Exception as e:  # noqa: BLE001 — recorded as aws_unavailable, never silent
                logger.error("bedrock reconciliation: CloudWatch query failed (region=%s): %s", region, e)
                for (m, r) in bridge.keys():
                    if r == region:
                        aws[(m, r)] = None
    else:
        logger.warning(
            "bedrock reconciliation: AWS credentials not configured — bridge "
            "sums stored as aws_unavailable (unverified, not ok)."
        )

    # Merge keys from both sides.
    all_keys = set(bridge.keys()) | {k for k, v in aws.items() if v and (v["input_tokens"] or v["output_tokens"])}

    results: List[Dict[str, Any]] = []
    pool = get_pool()

    for (model_id, region) in sorted(all_keys):
        b = bridge.get((model_id, region), {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        a = aws.get((model_id, region)) if aws_configured else None

        input_diff_pct = output_diff_pct = None
        detail = None

        if a is None:
            status = "aws_unavailable"
            detail = "CloudWatch not queryable (missing credentials or API error)"
        else:
            def _diff_pct(aws_v: int, bridge_v: int) -> Optional[float]:
                if aws_v == 0 and bridge_v == 0:
                    return 0.0
                base = max(aws_v, bridge_v)
                return round(abs(aws_v - bridge_v) / base * 100.0, 4)

            input_diff_pct = _diff_pct(a["input_tokens"], b["input_tokens"])
            output_diff_pct = _diff_pct(a["output_tokens"], b["output_tokens"])
            drift = max(input_diff_pct or 0.0, output_diff_pct or 0.0) > tolerance
            status = "drift" if drift else "ok"
            if drift and b["calls"] == 0:
                detail = "AWS reports tokens with ZERO bridge-side rows — untracked usage!"
            elif drift:
                detail = f"token drift beyond {tolerance}% tolerance"

        if status != "ok":
            logger.error(
                "🚨 bedrock reconciliation %s: day=%s model=%s region=%s "
                "bridge(in=%d out=%d calls=%d) aws(in=%s out=%s) diff(in=%s%% out=%s%%) %s",
                status, day, model_id, region,
                b["input_tokens"], b["output_tokens"], b["calls"],
                a["input_tokens"] if a else "?", a["output_tokens"] if a else "?",
                input_diff_pct, output_diff_pct, detail or "",
            )

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO bedrock_reconciliation (
                    day, bedrock_model_id, region,
                    bridge_calls, bridge_input_tokens, bridge_output_tokens,
                    aws_input_tokens, aws_output_tokens,
                    input_diff_pct, output_diff_pct,
                    status, detail, checked_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,NOW())
                ON CONFLICT (day, bedrock_model_id, region) DO UPDATE SET
                    bridge_calls = EXCLUDED.bridge_calls,
                    bridge_input_tokens = EXCLUDED.bridge_input_tokens,
                    bridge_output_tokens = EXCLUDED.bridge_output_tokens,
                    aws_input_tokens = EXCLUDED.aws_input_tokens,
                    aws_output_tokens = EXCLUDED.aws_output_tokens,
                    input_diff_pct = EXCLUDED.input_diff_pct,
                    output_diff_pct = EXCLUDED.output_diff_pct,
                    status = EXCLUDED.status,
                    detail = EXCLUDED.detail,
                    checked_at = NOW()
                """,
                day, model_id, region,
                b["calls"], b["input_tokens"], b["output_tokens"],
                a["input_tokens"] if a else None,
                a["output_tokens"] if a else None,
                input_diff_pct, output_diff_pct,
                status, detail,
            )

        results.append({
            "day": day.isoformat(),
            "bedrockModelId": model_id,
            "region": region,
            "bridgeCalls": b["calls"],
            "bridgeInputTokens": b["input_tokens"],
            "bridgeOutputTokens": b["output_tokens"],
            "awsInputTokens": a["input_tokens"] if a else None,
            "awsOutputTokens": a["output_tokens"] if a else None,
            "inputDiffPct": input_diff_pct,
            "outputDiffPct": output_diff_pct,
            "status": status,
            "detail": detail,
        })

    if not results:
        logger.info("bedrock reconciliation: day=%s — no Bedrock traffic on either side", day)

    return results


async def bedrock_reconciliation_loop() -> None:
    """Startup task: reconcile the previous UTC day, then re-check periodically.

    Idempotent per day (upsert), so overlapping runs across workers are
    harmless — last writer wins with identical inputs.
    """
    from src.db.client import is_db_enabled
    from src.auth import bedrock_credential_manager as bcm

    if not is_db_enabled():
        logger.info("bedrock reconciliation loop: no bridge DB — disabled")
        return
    if not bcm.is_configured():
        logger.info(
            "bedrock reconciliation loop: AWS credentials not configured — "
            "disabled (enable by setting AWS_ACCESS_KEY_ID_BEDROCK / "
            "AWS_SECRET_ACCESS_KEY_BEDROCK)"
        )
        return

    logger.info("bedrock reconciliation loop: started (every %ds)", _LOOP_INTERVAL_SECONDS)
    # Let startup finish first — the DB pool is initialised after the task
    # spawn point; the first run must not race it.
    await asyncio.sleep(60)
    while True:
        try:
            await reconcile_bedrock_day()
        except Exception as e:  # noqa: BLE001 — loop must survive, error must be loud
            logger.error("bedrock reconciliation run failed: %s", e)
        await asyncio.sleep(_LOOP_INTERVAL_SECONDS)
