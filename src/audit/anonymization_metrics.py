"""Anonymization accountability counters from audit_log (ADR-0009 Schritt 2d).

The public surface is the worker's GET /v1/metrics/anonymization (main.py) —
it feeds the failure-alarm cron. The data lives in audit_log, i.e. with
platform-api, so the query runs here and the worker resolves it via
GET /v1/internal/audit/anonymization-metrics (three-stage like every other
read leaf). Before this split the endpoint answered a hardcoded all-zeros
body on a DB-less worker — which after the worker-host move would have
silently blinded the anonymization failure alarm on EVERY request.
"""
from __future__ import annotations

from typing import Any, Dict

from src.db.client import get_pool, is_db_enabled


async def query_anonymization_metrics_from_db(hours: int) -> Dict[str, Any]:
    """Aggregate pii.pseudonymized / pii.anonymization_failed counters for the
    last ``hours``. Raises on infra error (no DB, query failure) — the caller
    decides how to present that; this function never fabricates zeros."""
    if not is_db_enabled():
        raise RuntimeError("db disabled — cannot read anonymization metrics")

    hours = max(1, hours)
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT action, actor_label, COUNT(*) AS n
              FROM audit_log
             WHERE action IN ('pii.pseudonymized', 'pii.anonymization_failed')
               AND timestamp >= now() - make_interval(hours => $1)
             GROUP BY action, actor_label
            """,
            hours,
        )
        last_failure = await conn.fetchval(
            "SELECT max(timestamp) FROM audit_log WHERE action = 'pii.anonymization_failed'"
        )

    failed_total = 0
    pseudonymized_total = 0
    failed_by_app: Dict[str, int] = {}
    for r in rows:
        n = int(r["n"])
        if r["action"] == "pii.anonymization_failed":
            failed_total += n
            label = r["actor_label"] or "unknown"
            failed_by_app[label] = failed_by_app.get(label, 0) + n
        else:
            pseudonymized_total += n

    return {
        "window_hours": hours,
        "failed_total": failed_total,
        "pseudonymized_total": pseudonymized_total,
        "failed_by_app": failed_by_app,
        "last_failure_ts": last_failure.isoformat() if last_failure else None,
    }
