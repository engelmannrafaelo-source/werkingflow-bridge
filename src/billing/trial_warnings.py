"""
Trial-expiry warning emails.

Sends two warnings per trial subscription:
  - 3 days before trial_ends_at  (column: trial_warning_3d_sent_at)
  - 1 day  before trial_ends_at  (column: trial_warning_1d_sent_at)

Idempotency: each send stamps its column to NOW(). The cron sweep selects
rows where the column IS NULL and the corresponding window is reached.

Defensive contract:
  - RESEND_API_KEY missing AND there are mails to send → loud error, no
    silent skip. (If there is nothing to send, the missing key is fine —
    we never need to talk to Resend.)
  - One user's email failure does NOT abort the sweep — other users are
    still processed. The stamp is only written on Resend 2xx.
  - The DB UPDATE that stamps the column is the LAST step per user. A
    crash after Resend-ack but before UPDATE causes a duplicate send on
    the next sweep; we accept that risk over the inverse (mark-then-send
    leading to silent skips).

Scheduling:
  - `start_trial_warning_loop()` schedules a sweep every day at 08:00 UTC.
  - On startup we wait until the next 08:00 UTC — we do NOT run a sweep
    on boot, since restart storms would otherwise re-trigger every send.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

import httpx

from src.db.client import get_pool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App-aware display names + portal URLs
# ---------------------------------------------------------------------------

_APP_DISPLAY_NAME: Dict[str, str] = {
    "werking-report": "WerkING Report",
    "werking-energy": "WerkING Energy",
    "werking-safety": "WerkING Safety",
    "werking-noise":  "WerkING Noise",
    "engelmann":      "Engelmann",
}

# Convention: each app exposes its portal at /settings/portal. Operators
# can override per-app via env var APP_PORTAL_URL_<APP_SLUG_UPPER>.
_APP_PORTAL_DEFAULT: Dict[str, str] = {
    "werking-report": "https://werking-report.werkingflow.com/settings/portal",
    "werking-energy": "https://werking-energy.werkingflow.com/settings/portal",
    "werking-safety": "https://werking-safety.werkingflow.com/settings/portal",
    "werking-noise":  "https://werking-noise.werkingflow.com/settings/portal",
    "engelmann":      "https://engelmann.werkingflow.com/settings/portal",
}


def _portal_url(app_id: str) -> str:
    env_key = "APP_PORTAL_URL_" + app_id.upper().replace("-", "_")
    override = os.environ.get(env_key)
    if override:
        return override
    default = _APP_PORTAL_DEFAULT.get(app_id)
    if not default:
        raise RuntimeError(
            f"trial_warnings: no portal URL configured for app_id={app_id!r}. "
            f"Set {env_key} env var or extend _APP_PORTAL_DEFAULT."
        )
    return default


def _app_display_name(app_id: str) -> str:
    name = _APP_DISPLAY_NAME.get(app_id)
    if not name:
        raise RuntimeError(
            f"trial_warnings: no display name for app_id={app_id!r}. "
            f"Extend _APP_DISPLAY_NAME."
        )
    return name


# ---------------------------------------------------------------------------
# Email rendering
# ---------------------------------------------------------------------------

def _render_warning_email(
    *,
    user_name: str,
    days_left: int,
    app_display: str,
    portal_url: str,
    trial_ends_at: datetime,
) -> tuple[str, str]:
    """Return (subject, html_body) for a trial-expiry warning."""
    if days_left <= 0:
        when_phrase = "heute"
        subject = f"Ihre Testphase für {app_display} endet heute"
    elif days_left == 1:
        when_phrase = "morgen"
        subject = f"Ihre Testphase für {app_display} endet morgen"
    else:
        when_phrase = f"in {days_left} Tagen"
        subject = f"Ihre Testphase für {app_display} endet in {days_left} Tagen"

    end_local = trial_ends_at.astimezone(timezone.utc).strftime("%d.%m.%Y")

    greeting = f"Hallo {user_name}," if user_name else "Hallo,"

    html = f"""<!doctype html>
<html lang="de">
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 560px; margin: 0 auto; padding: 24px; color: #1f2937;">
  <h2 style="margin: 0 0 16px;">Ihre Testphase endet {when_phrase}</h2>
  <p>{greeting}</p>
  <p>Ihre kostenlose Testphase für <strong>{app_display}</strong> läuft am
     <strong>{end_local}</strong> aus. Wenn Sie {app_display} weiter nutzen möchten,
     wählen Sie bitte rechtzeitig einen Plan in Ihrem Kundenportal.</p>
  <p style="margin: 28px 0;">
    <a href="{portal_url}"
       style="background: #2563eb; color: #ffffff; padding: 12px 20px;
              text-decoration: none; border-radius: 6px; display: inline-block;">
       Plan auswählen
    </a>
  </p>
  <p style="font-size: 13px; color: #6b7280;">
    Nach Ablauf der Testphase werden KI-gestützte Funktionen pausiert,
    bis ein Plan aktiviert ist. Ihre Daten bleiben erhalten.
  </p>
  <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 24px 0;">
  <p style="font-size: 12px; color: #9ca3af;">
    Sie erhalten diese E-Mail, weil Ihr Account bei {app_display} aktiv ist.
    Bei Fragen antworten Sie einfach auf diese Nachricht.
  </p>
</body>
</html>
"""
    return subject, html


# ---------------------------------------------------------------------------
# Resend sender
# ---------------------------------------------------------------------------

async def _send_via_resend(
    *,
    recipient: str,
    subject: str,
    html_body: str,
    resend_key: str,
    sender: str,
) -> None:
    """POST to Resend. Raises RuntimeError on non-2xx."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": sender,
                "to": [recipient],
                "subject": subject,
                "html": html_body,
            },
        )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Resend error {resp.status_code}: {resp.text[:200]}"
        )


# ---------------------------------------------------------------------------
# Sweep — selects due rows, sends, stamps
# ---------------------------------------------------------------------------

# Why the app_licenses EXISTS gate:
#
# Bridge identity is cross-app — a user row exists once across all apps,
# and the seed-legacy-trials backfill can create trial subscriptions for
# apps the user has never touched (an artefact of running the seed
# without a per-app activity filter). Sending "your trial ends" mail to
# users who never used the app is spam.
#
# app_licenses is the right signal because it is created exclusively
# during the per-app registration flow (identity/routes.py:498): one
# license per app the user has actually signed up for. Trial subscriptions
# without a matching app_license were seeded blindly and should NOT receive
# warning emails.
#
# end_date check: NULL = open-ended (default for trial licenses),
# CURRENT_DATE = still valid. Expired licenses (past end_date) are
# excluded.

_LICENSE_GATE = """
  AND  EXISTS (
        SELECT 1 FROM app_licenses al
        WHERE  al.user_id = s.user_id
          AND  al.app_id  = s.app_id
          AND  (al.end_date IS NULL OR al.end_date >= CURRENT_DATE)
       )
"""

_SELECT_3D_DUE_SQL = f"""
SELECT s.id            AS subscription_id,
       s.user_id       AS user_id,
       s.app_id        AS app_id,
       s.trial_ends_at AS trial_ends_at,
       u.email         AS email,
       u.name          AS name
FROM   subscriptions s
JOIN   users u ON u.id = s.user_id
WHERE  s.plan_id = 'trial'
  AND  s.status  = 'active'
  AND  s.trial_warning_3d_sent_at IS NULL
  AND  s.trial_ends_at IS NOT NULL
  AND  s.trial_ends_at >  NOW()
  AND  s.trial_ends_at <= NOW() + INTERVAL '3 days'
{_LICENSE_GATE}
"""

_SELECT_1D_DUE_SQL = f"""
SELECT s.id            AS subscription_id,
       s.user_id       AS user_id,
       s.app_id        AS app_id,
       s.trial_ends_at AS trial_ends_at,
       u.email         AS email,
       u.name          AS name
FROM   subscriptions s
JOIN   users u ON u.id = s.user_id
WHERE  s.plan_id = 'trial'
  AND  s.status  = 'active'
  AND  s.trial_warning_1d_sent_at IS NULL
  AND  s.trial_ends_at IS NOT NULL
  AND  s.trial_ends_at >  NOW()
  AND  s.trial_ends_at <= NOW() + INTERVAL '1 day'
{_LICENSE_GATE}
"""

_STAMP_3D_SQL = (
    "UPDATE subscriptions SET trial_warning_3d_sent_at = NOW() WHERE id = $1"
)
_STAMP_1D_SQL = (
    "UPDATE subscriptions SET trial_warning_1d_sent_at = NOW() WHERE id = $1"
)


async def _process_batch(
    rows: List[Dict[str, Any]],
    stamp_sql: str,
    resend_key: str,
    sender: str,
) -> Dict[str, int]:
    """Send warning email per row, stamp on success.

    Per-row exceptions are caught + logged. Returns counts.
    """
    sent = 0
    failed = 0
    skipped = 0
    pool = get_pool()

    for row in rows:
        email = row["email"]
        if not email:
            logger.warning(
                "trial_warnings: skipping subscription=%s — user_id=%s has no email",
                row["subscription_id"], row["user_id"],
            )
            skipped += 1
            continue

        trial_ends_at = row["trial_ends_at"]
        days_left = (trial_ends_at - datetime.now(timezone.utc)).days
        try:
            subject, html_body = _render_warning_email(
                user_name=(row["name"] or "").strip(),
                days_left=days_left,
                app_display=_app_display_name(row["app_id"]),
                portal_url=_portal_url(row["app_id"]),
                trial_ends_at=trial_ends_at,
            )
            await _send_via_resend(
                recipient=email,
                subject=subject,
                html_body=html_body,
                resend_key=resend_key,
                sender=sender,
            )
        except Exception as exc:
            logger.error(
                "trial_warnings: send failed subscription=%s user_id=%s email=%s err=%s",
                row["subscription_id"], row["user_id"], email, exc,
            )
            failed += 1
            continue

        try:
            async with pool.acquire() as conn:
                await conn.execute(stamp_sql, row["subscription_id"])
        except Exception as exc:
            # Email already went out but stamp failed → next sweep will
            # re-send. Loud-log so ops can manually stamp if needed.
            logger.error(
                "trial_warnings: STAMP FAILED after successful send "
                "subscription=%s user_id=%s err=%s — next sweep will re-send",
                row["subscription_id"], row["user_id"], exc,
            )
            failed += 1
            continue

        logger.info(
            "trial_warnings: sent subscription=%s user_id=%s email=%s days_left=%s app=%s",
            row["subscription_id"], row["user_id"], email, days_left, row["app_id"],
        )
        sent += 1

    return {"sent": sent, "failed": failed, "skipped": skipped}


async def run_trial_warning_sweep() -> Dict[str, Any]:
    """One sweep pass. Idempotent — safe to call multiple times.

    Raises RuntimeError if RESEND_API_KEY is missing AND any rows are due.
    Returns {"3d": {sent, failed, skipped}, "1d": {sent, failed, skipped}}.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows_3d = await conn.fetch(_SELECT_3D_DUE_SQL)
        rows_1d = await conn.fetch(_SELECT_1D_DUE_SQL)
    rows_3d_list = [dict(r) for r in rows_3d]
    rows_1d_list = [dict(r) for r in rows_1d]

    total_due = len(rows_3d_list) + len(rows_1d_list)
    if total_due == 0:
        logger.info("trial_warnings: sweep complete — no warnings due")
        return {"3d": {"sent": 0, "failed": 0, "skipped": 0},
                "1d": {"sent": 0, "failed": 0, "skipped": 0}}

    resend_key = os.environ.get("RESEND_API_KEY")
    sender = os.environ.get("RESEND_TRIAL_WARNING_SENDER",
                            os.environ.get("RESEND_INVOICE_SENDER",
                                           "billing@werking.tools"))
    if not resend_key:
        raise RuntimeError(
            f"trial_warnings: RESEND_API_KEY not configured on bridge — "
            f"cannot send {total_due} due warnings (3d={len(rows_3d_list)}, "
            f"1d={len(rows_1d_list)})"
        )

    logger.info(
        "trial_warnings: sweep starting — 3d_due=%s 1d_due=%s",
        len(rows_3d_list), len(rows_1d_list),
    )

    result_3d = await _process_batch(rows_3d_list, _STAMP_3D_SQL, resend_key, sender)
    result_1d = await _process_batch(rows_1d_list, _STAMP_1D_SQL, resend_key, sender)

    logger.info(
        "trial_warnings: sweep complete — 3d=%s 1d=%s",
        result_3d, result_1d,
    )
    return {"3d": result_3d, "1d": result_1d}


# ---------------------------------------------------------------------------
# Daily scheduler — wait until 08:00 UTC, sweep, repeat
# ---------------------------------------------------------------------------

_WARNING_HOUR_UTC = int(os.environ.get("TRIAL_WARNING_HOUR_UTC", "8"))


async def _trial_warning_loop() -> None:
    """Sleep until next 08:00 UTC, run sweep, repeat."""
    while True:
        now = datetime.now(timezone.utc)
        next_run = now.replace(
            hour=_WARNING_HOUR_UTC, minute=0, second=0, microsecond=0,
        )
        if next_run <= now:
            next_run = next_run + timedelta(days=1)
        delay = (next_run - now).total_seconds()
        logger.info(
            "trial_warnings: next sweep at %s UTC (in %.0f minutes)",
            next_run.isoformat(), delay / 60,
        )
        await asyncio.sleep(delay)

        try:
            await run_trial_warning_sweep()
        except Exception as exc:
            # Sweep-level failure (e.g. missing RESEND key). Log and keep
            # the loop alive — operator must fix config; the next sweep
            # in 24h will pick up the same + new due rows idempotently.
            logger.error("trial_warnings: sweep failed: %s", exc)


def start_trial_warning_loop() -> asyncio.Task:
    """Schedule the daily sweep. Call once from lifespan startup."""
    return asyncio.create_task(_trial_warning_loop())
