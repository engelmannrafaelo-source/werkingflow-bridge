"""Which ``app_id`` values the database actually accepts.

Why this exists (2026-08-01):
  ``activities.app_id`` is a Postgres ENUM listing the real apps. The rest of
  the stack treats app_id as a free-form string: it is read off an HTTP header
  (X-App-ID), or derived from the first segment of X-Client-ID, and then handed
  straight to the INSERT. Nothing in between asks "is this actually an app?".

  That gap cost us the ledger. ``/v1/jobs`` dispatched without X-App-ID labels
  its self-call ``bridge-jobs/selfcall`` (src/jobs/executors.py), which becomes
  app_id="bridge-jobs" — not an enum member. Postgres rejected the audit INSERT,
  the shared try-block took the usage_events INSERT down with it, and the whole
  thing was swallowed as a warning. Every research job dispatched that way
  booked nothing at all; research-cloud spend (real money on the 1P key) was
  invisible from 2026-07-27 onward.

  The bitter part: the self-call label was introduced precisely to "name the
  true call-site WITHOUT masking the leak". The enum inverted that intent —
  instead of a visible bridge-jobs row there was no row.

Design:
  The DB enum is the single source of truth for "which apps exist". Mirroring
  it as a Python literal would drift the moment an app is added — someone edits
  the enum, forgets the constant, and the new app silently books as NULL. So we
  read the enum once at startup and validate against that.

  Validation NEVER invents an app and never drops the evidence: a value that is
  not an app is replaced by NULL (which the column accepts and which books
  fine — that is the honest "no app" case) while the raw value travels on into
  the row's metadata, so the call-site stays queryable. Loud once per distinct
  value, so a new app that nobody added to the enum is findable instead of
  quietly unattributed.
"""
from __future__ import annotations

import logging
from typing import FrozenSet, Optional, Tuple

logger = logging.getLogger(__name__)

# Enum type backing activities.app_id. Named here (not spread over call sites)
# so a rename is a one-line change.
APP_ID_ENUM_TYPE = "app_id"

# Populated once by load_known_app_ids() at startup. None = "not loaded yet",
# which is deliberately distinct from an empty set ("loaded, enum is empty").
_known_app_ids: Optional[FrozenSet[str]] = None

# Distinct rejected values already reported — keeps a busy call-site from
# flooding the log while still counting how often it happened.
_rejected_counts: dict = {}


async def load_known_app_ids() -> Optional[FrozenSet[str]]:
    """Read the app_id enum members from Postgres and cache them.

    Called once during startup. Returns None when there is no database on this
    instance (prod-bridge runs without one) — validation then stays off, which
    is correct: with no DB there is no INSERT to protect.

    A failure here is logged loudly rather than raised: the Bridge must still
    boot without its ledger, and normalize_app_id() degrades to pass-through.
    """
    global _known_app_ids

    from src.db.client import get_pool, is_db_enabled

    if not is_db_enabled():
        logger.info(
            "app registry: no Bridge DB on this instance — app_id validation off"
        )
        return None

    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT e.enumlabel
                FROM pg_enum e
                JOIN pg_type t ON t.oid = e.enumtypid
                WHERE t.typname = $1
                """,
                APP_ID_ENUM_TYPE,
            )
        _known_app_ids = frozenset(r["enumlabel"] for r in rows)
        if not _known_app_ids:
            logger.error(
                "app registry: enum %r resolved to ZERO members — every app_id "
                "would now be rejected as unknown. Validation stays OFF until "
                "this is fixed.",
                APP_ID_ENUM_TYPE,
            )
            _known_app_ids = None
            return None
        logger.info(
            "app registry: %d app_id enum members loaded (%s)",
            len(_known_app_ids),
            ", ".join(sorted(_known_app_ids)),
        )
        return _known_app_ids
    except Exception as e:  # noqa: BLE001 — boot must survive a ledger outage
        logger.error(
            "app registry: could not load the %r enum (%s) — app_id validation "
            "is OFF, an invalid app_id will again cost the whole ledger row",
            APP_ID_ENUM_TYPE, e,
        )
        _known_app_ids = None
        return None


def known_app_ids() -> Optional[FrozenSet[str]]:
    """The cached enum members, or None if not loaded (see load_known_app_ids)."""
    return _known_app_ids


def reset_registry_for_tests(known: Optional[FrozenSet[str]] = None) -> None:
    """Set/clear the cached set. Tests only — production fills it at startup."""
    global _known_app_ids
    _known_app_ids = known
    _rejected_counts.clear()


def normalize_app_id(
    raw: Optional[str], *, known: Optional[FrozenSet[str]] = None
) -> Tuple[Optional[str], Optional[str]]:
    """Split an inbound app_id into (value safe for the enum column, rejected raw).

    Returns ``(app_id, None)`` when the value is a real app or already absent,
    and ``(None, raw)`` when it is something else — a client-id segment, a user
    name, a typo. The caller writes the first into the enum column and carries
    the second into metadata, so nothing is lost and nothing is invented.

    Pure when ``known`` is passed explicitly, which is how the tests drive it.
    With no registry loaded it degrades to pass-through (cannot validate what we
    cannot enumerate) — logged once so the degraded mode is visible.
    """
    if raw is None or raw == "":
        return None, None

    valid = known if known is not None else _known_app_ids
    if valid is None:
        if not _rejected_counts.get("__registry_unloaded__"):
            logger.warning(
                "app registry: app_id=%r passed through unvalidated — enum not "
                "loaded on this instance",
                raw,
            )
        _rejected_counts["__registry_unloaded__"] = (
            _rejected_counts.get("__registry_unloaded__", 0) + 1
        )
        return raw, None

    if raw in valid:
        return raw, None

    seen = _rejected_counts.get(raw, 0) + 1
    _rejected_counts[raw] = seen
    if seen == 1:
        logger.error(
            "app registry: app_id=%r is not one of the %d known apps (%s) — "
            "booking this call with app=NULL and keeping %r in the row metadata. "
            "If this IS a real app, add it to the %r enum; if it is a call-site "
            "label, it does not belong in app_id.",
            raw, len(valid), ", ".join(sorted(valid)), raw, APP_ID_ENUM_TYPE,
        )
    elif seen % 100 == 0:
        logger.error(
            "app registry: app_id=%r rejected %dx (still booking as app=NULL)",
            raw, seen,
        )
    return None, raw
