"""Worker-side client for the money path's database leaves (ADR-0009 Schritt 2c).

The counterpart of src/activity/ledger_db.py (which runs on platform-api) and
the same relationship src/audit/recorder.py has to src/audit/db_writer.py: the
worker states facts over HTTP, platform-api owns the connection.

## No direct-DB fallback here — on purpose, and unlike the read paths

principals.py, user_resolver.py and prepaid_cap.py all fall back to their own
Postgres query when platform-api cannot answer (Schritt 2a/2b). This module
deliberately does not, for two reasons that only apply to the money path:

  * **There is already a better fallback.** Every call handled here is on disk,
    fsync'd, BEFORE the first of these requests is made (Schritt 1's write-ahead
    spool). "platform-api did not answer" therefore does not mean the row is
    lost — it means the row is still OWED and the flusher will replay it, off
    the hot path, with a visible backlog on /health. A second, synchronous
    database path would add a failure mode without adding a guarantee.
  * **A fallback would defeat the point.** Schritt 3 moves a worker to its own
    host precisely so it stops needing BRIDGE_DB_URL. A DB fallback on the
    hottest write path is the one dependency that would have to survive the
    move — i.e. a raw Postgres port over the network, which is what the move
    exists to avoid.

## Retry polarity per call

The three functions differ, and the difference is load-bearing:

  * The two READS opt into one retry — replaying a pure read cannot write
    anything, and the precedent (user_resolver, principals) is exactly this.
  * The WRITE does NOT retry, although its idempotency key would make a retry
    *safe*. Safe is not the same as worthwhile: the spool is already a retry
    mechanism for this exact record, and a better one (asynchronous, bounded by
    MAX_ATTEMPTS/MAX_AGE, observable). Retrying inline would only move that work
    onto the caller's latency, so the conservative default stands.

Nothing in this module swallows an error into a success. Everything that is not
a definitive answer raises, and the caller turns that into "still owed".
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.platform_client import PlatformResponse, call_platform

logger = logging.getLogger(__name__)

# The ledger write does two INSERTs and is the one call whose ANSWER we cannot
# reconstruct afterwards: hearing "written" is what authorises the (non-
# repeatable) budget deduction, so a premature timeout costs a deduction even
# though the row itself is safe. Hence more headroom than the 2s the read leaves
# and the budget gate use — but still bounded, because this runs in the request
# path.
LEDGER_WRITE_TIMEOUT_S = 5.0

# Set once the anonymous identity row is confirmed present — a migration that
# has run cannot un-run, so this is cached for the process lifetime. Only a
# POSITIVE answer is cached: a negative one must keep being asked, or running
# migration 032 would not take effect until the worker restarts.
# Keyed by federation cache scope (ADR-0011): "" = own platform-api, else the
# peer's — the two databases run their migrations independently, so "present
# here" says nothing about "present there".
_anonymous_identity_verified: set = set()


class LedgerWriteRejected(Exception):
    """platform-api answered, and the answer was not a usable outcome.

    Distinct from PlatformUnavailable ("could not answer at all"). Both leave
    the call owed in the spool — this one because a definitive-looking 4xx on
    the money path is a caller/contract bug, and dropping a real billing row on
    a bug is never the right trade. The spool bounds the repetition: after
    MAX_ATTEMPTS the record is buried with a log, exactly as a permanently
    unwritable row behaved before this seam existed.
    """


async def anonymous_identity_present() -> bool:
    """Is the migration-032 anonymous identity there? Cached once true.

    Raises PlatformUnavailable / LedgerWriteRejected when the question could not
    be answered — the caller must not read that as "absent", because absent is a
    definitive skip and unanswerable is not.
    """
    from src.federation import cache_scope

    scope = cache_scope()
    if scope in _anonymous_identity_verified:
        return True

    resp = await call_platform(
        "GET", "/v1/internal/identity/anonymous", retries=1, domain="user"
    )
    present = _require_field(resp, "present", "/v1/internal/identity/anonymous")
    if present:
        _anonymous_identity_verified.add(scope)
    return bool(present)


async def load_billing_context(user_id: str) -> Optional[Dict[str, Any]]:
    """{tenantId, billingMode} for this user, or None when there is no tenant.

    None is a definitive answer the caller turns into "skipped:no_tenant". That
    is why an unexpected status or body raises instead of returning None: a
    platform-api that has not been deployed answers 404, and reading that as
    "this customer has no tenant" would release a real billing row from the
    spool and file the call as correctly-not-metered.
    """
    resp = await call_platform(
        "GET", f"/v1/internal/users/{user_id}/billing-context", retries=1,
        domain="user",
    )
    return _require_field(
        resp, "context", f"/v1/internal/users/{user_id}/billing-context"
    )


async def write_ai_call(payload: Dict[str, Any]) -> str:
    """Write the authoritative billing row (+ its audit row). Returns the
    ledger_spool outcome: "written" (this attempt created it) or "duplicate"
    (a replay caught up with a row that had already landed).

    "written" is the only answer that authorises the budget deduction, and it
    can be given at most once per idempotency key — that is what makes a replay
    incapable of charging twice.

    Raises PlatformUnavailable (unreachable / timeout / 5xx) or
    LedgerWriteRejected (any other non-answer). Both mean: not written, still
    owed. Never returns a fabricated outcome.
    """
    resp = await call_platform(
        "POST",
        "/v1/internal/usage/ai-call",
        json=payload,
        timeout_s=LEDGER_WRITE_TIMEOUT_S,
        retries=0,  # the spool is this call's retry — see module docstring
        domain="user",  # ADR-0011: the usage row belongs to the user's HOME ledger
    )
    outcome = _require_field(resp, "outcome", "/v1/internal/usage/ai-call")
    if outcome not in ("written", "duplicate"):
        raise LedgerWriteRejected(
            f"/v1/internal/usage/ai-call returned unknown outcome {outcome!r} — "
            f"treating the billing row as not written"
        )
    if resp.json and resp.json.get("auditWritten") is False:
        # The money row landed; only the audit trail has a gap. Reported here
        # because the worker's logs are where this path is usually read from.
        logger.error(
            "ledger: usage_events row for call %s is written, but its audit row "
            "was rejected — the call stays metered, the audit trail does not "
            "cover it", payload.get("idempotency_key"),
        )
    return outcome


def _require_field(resp: PlatformResponse, field: str, path: str) -> Any:
    """200 + the expected key, or raise. There is no lenient reading of a money
    -path answer: a missing key means we do not know what happened, and not
    knowing must never be rendered as a definitive outcome."""
    if resp.status_code == 200 and isinstance(resp.json, dict) and field in resp.json:
        return resp.json[field]
    raise LedgerWriteRejected(
        f"{path} answered status={resp.status_code} body={resp.json!r} — "
        f"expected 200 with a {field!r} field"
    )
