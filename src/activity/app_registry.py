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
#
# BOUNDED on purpose: the key is an inbound header value, i.e. attacker- and
# bug-controlled. An unbounded dict here would be a slow memory leak driven by
# whatever strings the outside world sends (one dead app looping a random
# client-id is enough). Past the cap we stop tracking NEW labels and count them
# in bulk instead — the diagnostic value is in the first few anyway.
_REJECTED_TRACKING_CAP = 256
_rejected_counts: dict = {}
_rejected_overflow = 0

# Set once the degraded "no registry loaded" path has been reported, so the
# no-DB case says so exactly once instead of per call.
_unloaded_reported = False


async def load_known_app_ids() -> Optional[FrozenSet[str]]:
    """Read the app_id enum members from Postgres and cache them.

    Called once at worker startup, AFTER init_pool(). Returns None on an
    instance with no database (prod-bridge runs without one) — validation then
    stays off, which is correct rather than degraded: with no DB there is no
    INSERT to protect.

    FAIL FAST when a DB *is* configured and the enum cannot be read. Same
    invariant as validate_billing_integrity() and the plan catalog: a worker
    that cannot tell a real app from a call-site label must not serve traffic
    whose ledger rows can silently vanish — that is exactly the failure this
    module exists to end, and swallowing it here would reinstate it while
    looking healthy in the logs.

    (Written after doing precisely that: the first cut of this call sat before
    init_pool(), get_pool() raised, the except logged and moved on, and
    validation was permanently off in a build that reported success.)
    """
    global _known_app_ids

    from src.db.client import is_db_enabled

    if not is_db_enabled():
        logger.info(
            "app registry: no Bridge DB on this instance — app_id validation off"
        )
        _known_app_ids = None
        return None

    # No try/except: a failure here must reach the caller and stop the boot.
    from src.db.client import get_pool

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

    loaded = frozenset(r["enumlabel"] for r in rows)
    if not loaded:
        raise RuntimeError(
            f"APP REGISTRY VIOLATION — enum {APP_ID_ENUM_TYPE!r} resolved to ZERO "
            "members. Either the type is missing (migration not applied) or the "
            "query is wrong. Booting on would validate every app_id as unknown "
            "and book the whole fleet as app=NULL — kein Silent-Fail erlaubt."
        )

    _known_app_ids = loaded
    logger.info(
        "app registry: %d app_id enum members loaded (%s)",
        len(loaded), ", ".join(sorted(loaded)),
    )
    return loaded


def known_app_ids() -> Optional[FrozenSet[str]]:
    """The cached enum members, or None if not loaded (see load_known_app_ids)."""
    return _known_app_ids


def reset_registry_for_tests(known: Optional[FrozenSet[str]] = None) -> None:
    """Set/clear the cached set. Tests only — production fills it at startup."""
    global _known_app_ids, _rejected_overflow, _unloaded_reported
    _known_app_ids = known
    _rejected_counts.clear()
    _rejected_overflow = 0
    _unloaded_reported = False


def normalize_app_id(
    raw: Optional[str], *, known: Optional[FrozenSet[str]] = None
) -> Tuple[Optional[str], Optional[str]]:
    """Split an inbound app_id into (value safe for the enum column, rejected raw).

    Returns ``(app_id, None)`` when the value is a real app or already absent,
    and ``(None, raw)`` when it is something else — a client-id segment, a user
    name, a typo. The caller writes the first into the enum column and carries
    the second into metadata, so nothing is lost and nothing is invented.

    Pure when ``known`` is passed explicitly, which is how the tests drive it.

    An unloaded registry means "no DB on this instance" — startup fails loudly
    otherwise (see load_known_app_ids), so this branch cannot be a silently
    degraded worker that still writes rows. With no DB there is no INSERT, so
    passing the value through is honest rather than a fallback.
    """
    global _rejected_overflow, _unloaded_reported

    if raw is None or raw == "":
        return None, None

    valid = known if known is not None else _known_app_ids
    if valid is None:
        if not _unloaded_reported:
            _unloaded_reported = True
            logger.info(
                "app registry: app_id=%r not validated — no enum loaded (instance "
                "without a Bridge DB, so nothing is written either)",
                raw,
            )
        return raw, None

    if raw in valid:
        return raw, None

    known_label = raw in _rejected_counts
    if known_label or len(_rejected_counts) < _REJECTED_TRACKING_CAP:
        seen = _rejected_counts.get(raw, 0) + 1
        _rejected_counts[raw] = seen
    else:
        # Cap reached — count in bulk instead of growing on inbound strings.
        _rejected_overflow += 1
        if _rejected_overflow % 1000 == 1:
            logger.error(
                "app registry: >%d distinct invalid app_id labels seen; %d further "
                "rejects not tracked individually (latest %r). Something is "
                "generating app labels — check X-Client-ID senders.",
                _REJECTED_TRACKING_CAP, _rejected_overflow, raw,
            )
        return None, raw

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
