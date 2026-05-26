"""
Webhook dispatcher — background loop that delivers auth-token webhooks.

Each iteration:
  1. SELECT up to `BATCH_SIZE` pending deliveries whose retry-time has come.
  2. For each row: build payload, sign with HMAC, POST to the app's URL.
  3. Translate the HTTP outcome into a status transition:
       2xx → delivered
       4xx → failed (Bridge does NOT retry — the App rejected our payload,
             which is an App-side bug, not a transient outage).
       5xx / network / timeout → keep `pending`, increment attempts, schedule
             `next_retry_at` via exponential backoff.
  4. After 5 attempts → abandoned + alert.
  5. Sleep `POLL_INTERVAL_SECONDS`, repeat.

Design choices (ADR cross-app/0002 Phase M1):

  * One worker per Bridge platform-api instance. SELECT FOR UPDATE SKIP LOCKED
    keeps multiple replicas from racing — the row is locked for the duration
    of the HTTP call. asyncpg's `transaction()` provides FOR UPDATE; SKIP
    LOCKED is appended to the SELECT.

  * Payload shape: {token, kind, email, expiresAt, userId}. `email` is joined
    from `users` at dispatch time (not stored on the delivery row) so an
    edit of the user's email between issue and delivery uses the current
    address — the user who would actually receive the reset link.

  * Signing input is `<timestamp>.<nonce>.<body>` (see webhook_signature.py).
    Nonce is a fresh 16-byte hex per attempt; replays of the same token can
    therefore differ on the wire, but the app dedups via token_id in the
    payload (Phase M2).

  * Fail-loud: if a webhook config is missing for a row's app_id, log + leave
    the row pending. The boot-time check in webhook_config.init_webhook_configs
    should have prevented this; if it slips through, do NOT silently abandon.

  * No background thread / process — pure asyncio. Started by
    platform_main.lifespan; stopped on shutdown via task.cancel().
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

import httpx

from src.identity.webhook_config import get_webhook_config
from src.identity.webhook_signature import sign

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — env-overridable so ops can tighten / loosen at runtime
# ---------------------------------------------------------------------------

# Max pending rows fetched per loop iteration. Capped to keep one worker
# from monopolising the connection pool when a flood of tokens lands.
BATCH_SIZE: int = int(os.getenv("WEBHOOK_DISPATCHER_BATCH_SIZE", "50"))

# Sleep between loop iterations. 30s matches the ADR. Too short = wasted
# DB scans; too long = noticeable mail-delivery latency for users.
POLL_INTERVAL_SECONDS: float = float(os.getenv("WEBHOOK_DISPATCHER_POLL_INTERVAL_SECONDS", "30"))

# Per-request timeout. Slow apps shouldn't pin the worker for minutes —
# 10s is generous for a "store token + queue mail" handler.
REQUEST_TIMEOUT_SECONDS: float = float(os.getenv("WEBHOOK_DISPATCHER_REQUEST_TIMEOUT_SECONDS", "10"))

# Max attempts before status flips to `abandoned`. The backoff schedule below
# must have at least this many entries (we look up by `attempts` post-increment).
MAX_ATTEMPTS: int = 5

# Backoff schedule (seconds) — index = `attempts` value after increment.
# So a row that just failed its 1st attempt gets next_retry_at = now + 60s,
# 2nd → +300s, etc. Schedule mirrors the ADR.
_BACKOFF_SECONDS: Tuple[int, ...] = (
    60,        # after 1st failure → retry in  1 min
    5 * 60,    # after 2nd failure → retry in  5 min
    30 * 60,   # after 3rd failure → retry in 30 min
    2 * 60 * 60,    # after 4th failure → retry in  2 h
    12 * 60 * 60,   # after 5th failure → would be retry in 12h — but at
                    # attempts >= MAX_ATTEMPTS we instead `abandon`. Kept for
                    # symmetry / future-tuning.
)


def _backoff_seconds(attempts: int) -> int:
    """
    Map post-increment `attempts` to the next-retry delay in seconds.

    `attempts == 1` after the first failure → schedule[0]. Bounds-checked
    so an unexpected `attempts` value never IndexError-crashes the loop.
    """
    idx = max(0, min(attempts - 1, len(_BACKOFF_SECONDS) - 1))
    return _BACKOFF_SECONDS[idx]


# ---------------------------------------------------------------------------
# Alerting hook — abandoned deliveries
# ---------------------------------------------------------------------------

def _alert_email() -> Optional[str]:
    """
    Operator email for abandoned-delivery alerts. Read each call so a runtime
    Infisical update propagates without restart. Returns None if unset — in
    that case we log a WARNING with full delivery details so an ops alert
    can be wired up later via the log pipeline.
    """
    val = (os.getenv("BRIDGE_OPS_EMAIL") or "").strip()
    return val or None


async def _emit_abandoned_alert(
    *, delivery_id: str, token_id: str, app_id: str, kind: str,
    response_status: Optional[int], response_body: Optional[str],
) -> None:
    """
    Surface an abandoned delivery. Today: a structured WARNING log line.
    Future: ship via an actual mail / pager path — the call-site already
    has everything needed.
    """
    target = _alert_email() or "<unset BRIDGE_OPS_EMAIL>"
    logger.warning(
        "webhook_dispatcher.abandoned: delivery_id=%s token_id=%s app_id=%s "
        "kind=%s last_status=%s last_body=%s alert_to=%s",
        delivery_id, token_id, app_id, kind,
        response_status,
        (response_body or "")[:500],
        target,
    )


# ---------------------------------------------------------------------------
# Core delivery — single row
# ---------------------------------------------------------------------------

async def _claim_and_load_one(conn: Any) -> Optional[Dict[str, Any]]:
    """
    Claim ONE pending delivery via SELECT ... FOR UPDATE SKIP LOCKED.

    Returns the row joined with auth_tokens + users, or None when there are
    no pending rows ready to retry. The lock is held until the surrounding
    transaction completes (caller's responsibility — we expect the caller
    to be inside `async with conn.transaction()`).

    SKIP LOCKED makes the dispatcher safe to scale horizontally: if a second
    worker is added in the future, neither will block on a row the other is
    handling.
    """
    row = await conn.fetchrow(
        """
        SELECT d.id              AS delivery_id,
               d.token_id        AS token_id,
               d.app_id::text    AS app_id,
               d.kind            AS kind,
               d.attempts        AS attempts,
               d.token_cleartext AS token_cleartext,
               t.expires_at      AS expires_at,
               t.user_id         AS user_id,
               u.email           AS email,
               u.anonymized_at   AS anonymized_at
          FROM auth_token_webhook_deliveries d
          JOIN auth_tokens t ON t.id = d.token_id
          JOIN users       u ON u.id = t.user_id
         WHERE d.status = 'pending'
           AND (d.next_retry_at IS NULL OR d.next_retry_at <= NOW())
         ORDER BY d.created_at
         LIMIT 1
         FOR UPDATE OF d SKIP LOCKED
        """,
    )
    return dict(row) if row else None


def _build_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build the JSON payload sent to the app, per ADR cross-app/0002:

        { token, kind, email, expiresAt, userId }

    `token` is the cleartext loaded from auth_token_webhook_deliveries
    (see migration 021 for the cleartext-lifecycle invariant). The row is
    expected to carry a non-NULL `token_cleartext` since the dispatcher
    only claims pending rows and the CHECK constraint guarantees pending
    ⇒ cleartext present.

    `email` is joined from `users` at dispatch time (not stored on the
    delivery row) so an email update between issue and delivery uses the
    address that would actually receive the reset link.
    """
    cleartext = row.get("token_cleartext")
    if not cleartext:
        # Defensive: should be unreachable thanks to chk_cleartext_lifecycle,
        # but we never want to silently send a malformed payload.
        raise RuntimeError(
            f"webhook_dispatcher: pending row {row['delivery_id']} has no "
            f"cleartext — chk_cleartext_lifecycle constraint violated."
        )
    expires_at = row["expires_at"]
    return {
        "token": cleartext,
        "kind": row["kind"],
        "email": row["email"],
        "expiresAt": expires_at.isoformat() if expires_at else None,
        "userId": str(row["user_id"]),
    }


async def _http_post_with_signature(
    *, url: str, secret: str, payload: Dict[str, Any], client: httpx.AsyncClient,
) -> Tuple[Optional[int], Optional[str], Optional[Exception]]:
    """
    POST `payload` to `url` with HMAC headers. Returns
    (status_code, response_body_truncated, exception) — exactly one of
    `status_code` and `exception` is set on each return path.

    Body is `json.dumps(payload).encode('utf-8')` so the signature input
    matches what hits the wire (no library-internal re-serialisation).

    Response body is capped at 1024 chars before storing — the column is
    TEXT but we don't want a misbehaving app dumping a megabyte of HTML
    into our DB on every retry.
    """
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature_hex, timestamp, nonce = sign(secret, body)
    headers = {
        "Content-Type": "application/json",
        "X-Bridge-Signature": signature_hex,
        "X-Bridge-Timestamp": str(timestamp),
        "X-Bridge-Nonce": nonce,
    }
    try:
        resp = await client.post(url, content=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — we want EVERY transport error here
        return None, None, exc
    return resp.status_code, (resp.text or "")[:1024], None


def _classify(status_code: Optional[int]) -> str:
    """
    Map the HTTP outcome (or None == transport error) to a transition class:
        'delivered'  — 2xx
        'failed'     — 4xx (App rejected; do NOT retry)
        'retry'      — 5xx / network / timeout
    """
    if status_code is None:
        return "retry"
    if 200 <= status_code < 300:
        return "delivered"
    if 400 <= status_code < 500:
        return "failed"
    return "retry"


async def _apply_outcome(
    conn: Any,
    *,
    delivery_id: str,
    token_id: str,
    app_id: str,
    kind: str,
    attempts_before: int,
    status_code: Optional[int],
    response_body: Optional[str],
    transport_error: Optional[Exception],
) -> str:
    """
    Persist the outcome onto the delivery row. Returns the new `status`.

    Same connection / transaction context as `_claim_and_load_one` — keeps
    the FOR UPDATE lock until we commit, which guarantees no double-delivery.
    """
    klass = _classify(status_code)
    body_to_store = (
        response_body
        if response_body is not None
        else (f"transport_error: {transport_error!r}" if transport_error else None)
    )

    if klass == "delivered":
        # Scrub cleartext on terminal state (chk_cleartext_lifecycle).
        await conn.execute(
            """
            UPDATE auth_token_webhook_deliveries
               SET status          = 'delivered',
                   attempts        = $1,
                   last_attempt_at = NOW(),
                   next_retry_at   = NULL,
                   response_status = $2,
                   response_body   = $3,
                   token_cleartext = NULL
             WHERE id = $4
            """,
            attempts_before + 1, status_code, body_to_store, delivery_id,
        )
        logger.info(
            "webhook_dispatcher.delivered: delivery_id=%s token_id=%s app_id=%s kind=%s status=%s",
            delivery_id, token_id, app_id, kind, status_code,
        )
        return "delivered"

    if klass == "failed":
        # Scrub cleartext on terminal state (chk_cleartext_lifecycle).
        await conn.execute(
            """
            UPDATE auth_token_webhook_deliveries
               SET status          = 'failed',
                   attempts        = $1,
                   last_attempt_at = NOW(),
                   next_retry_at   = NULL,
                   response_status = $2,
                   response_body   = $3,
                   token_cleartext = NULL
             WHERE id = $4
            """,
            attempts_before + 1, status_code, body_to_store, delivery_id,
        )
        logger.warning(
            "webhook_dispatcher.failed_4xx: delivery_id=%s token_id=%s app_id=%s kind=%s status=%s body=%s",
            delivery_id, token_id, app_id, kind, status_code,
            (body_to_store or "")[:200],
        )
        return "failed"

    # klass == 'retry' — either schedule next attempt or abandon
    attempts_after = attempts_before + 1
    if attempts_after >= MAX_ATTEMPTS:
        # Scrub cleartext on terminal state (chk_cleartext_lifecycle).
        await conn.execute(
            """
            UPDATE auth_token_webhook_deliveries
               SET status          = 'abandoned',
                   attempts        = $1,
                   last_attempt_at = NOW(),
                   next_retry_at   = NULL,
                   response_status = $2,
                   response_body   = $3,
                   token_cleartext = NULL
             WHERE id = $4
            """,
            attempts_after, status_code, body_to_store, delivery_id,
        )
        await _emit_abandoned_alert(
            delivery_id=delivery_id, token_id=token_id,
            app_id=app_id, kind=kind,
            response_status=status_code, response_body=body_to_store,
        )
        return "abandoned"

    delay = _backoff_seconds(attempts_after)
    await conn.execute(
        """
        UPDATE auth_token_webhook_deliveries
           SET status          = 'pending',
               attempts        = $1,
               last_attempt_at = NOW(),
               next_retry_at   = NOW() + ($2 || ' seconds')::interval,
               response_status = $3,
               response_body   = $4
         WHERE id = $5
        """,
        attempts_after, str(delay), status_code, body_to_store, delivery_id,
    )
    logger.info(
        "webhook_dispatcher.retry_scheduled: delivery_id=%s token_id=%s app_id=%s "
        "kind=%s attempts=%s next_in=%ss last_status=%s",
        delivery_id, token_id, app_id, kind,
        attempts_after, delay, status_code,
    )
    return "pending"


async def process_one_pending(
    pool: Any, client: httpx.AsyncClient,
) -> Optional[str]:
    """
    Claim + deliver + persist one pending row, end-to-end.

    Returns the new status of the row, or None when no row was claimable.
    This is the smallest unit that's worth testing on its own.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _claim_and_load_one(conn)
            if row is None:
                return None

            delivery_id = str(row["delivery_id"])
            token_id = str(row["token_id"])
            app_id = row["app_id"]
            kind = row["kind"]
            attempts_before = int(row["attempts"])

            # Resolve webhook config; if missing — log + leave pending. The
            # boot-time check should make this impossible; if it does happen,
            # we'd rather see the row stuck pending (and an operator-visible
            # log line) than burn an attempt against a wrong URL.
            try:
                cfg = get_webhook_config(app_id)
            except (LookupError, RuntimeError) as exc:
                logger.error(
                    "webhook_dispatcher.config_missing: delivery_id=%s app_id=%s err=%s",
                    delivery_id, app_id, exc,
                )
                return "pending"

            payload = _build_payload(row)

            status_code, response_body, transport_error = await _http_post_with_signature(
                url=cfg.url, secret=cfg.secret, payload=payload, client=client,
            )

            return await _apply_outcome(
                conn,
                delivery_id=delivery_id,
                token_id=token_id,
                app_id=app_id,
                kind=kind,
                attempts_before=attempts_before,
                status_code=status_code,
                response_body=response_body,
                transport_error=transport_error,
            )


# ---------------------------------------------------------------------------
# Background loop — started from platform_main.lifespan
# ---------------------------------------------------------------------------

class WebhookDispatcher:
    """
    Lifecycle wrapper around the background loop. One instance per process;
    `start()` is idempotent (re-starting after a crash is OK), `stop()`
    cancels the loop cleanly.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._task: Optional[asyncio.Task] = None
        self._stopped: bool = False
        # httpx client — pooled per dispatcher, not per request. Keeps
        # TCP/TLS reuse across attempts. Initialised on `start()`.
        self._client: Optional[httpx.AsyncClient] = None

    async def _loop(self) -> None:
        logger.info(
            "webhook_dispatcher.loop_started: poll=%ss batch=%s",
            POLL_INTERVAL_SECONDS, BATCH_SIZE,
        )
        assert self._client is not None
        while not self._stopped:
            try:
                # Drain up to BATCH_SIZE rows in this iteration. SKIP LOCKED
                # makes each `process_one_pending` independent — we just loop
                # until there's nothing more to claim or we hit the cap.
                drained = 0
                while drained < BATCH_SIZE:
                    new_status = await process_one_pending(self._pool, self._client)
                    if new_status is None:
                        break
                    drained += 1

                if drained > 0:
                    logger.debug(
                        "webhook_dispatcher.iteration: processed=%s", drained,
                    )

                # Sleep interruptibly so stop() cancels us promptly.
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                # NEVER let an unexpected error kill the loop — back off and try again.
                logger.exception("webhook_dispatcher.loop_iteration_failed: %s", exc)
                await asyncio.sleep(POLL_INTERVAL_SECONDS)

        logger.info("webhook_dispatcher.loop_stopped")

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._client = httpx.AsyncClient()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client is not None:
            await self._client.aclose()
            self._client = None
