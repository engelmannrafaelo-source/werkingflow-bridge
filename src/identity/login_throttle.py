"""
Per-account failed-login throttle for POST /v1/auth/login.

Login had NO brute-force protection (security-audit-live-findings-20260818.md
L10c/B.4): the endpoint is fully PUBLIC (no service token, no app-level
auth — see the contract table at the top of identity/routes.py) and directly
reachable on the world-open Bridge port (L13: ":8000 ohne TLS, weltoffen").

IP-based rate limiting (the pattern already used for the worker's own
"auth"-bucket endpoints, src/rate_limiter.py) is the WRONG tool here: every
app calls /v1/auth/login server-side from its own Next.js API route running
on Vercel (never browser-direct — see bridge-auth.ts / bridge-adapter.ts),
so the Bridge only ever sees each app's shared Vercel egress IP for EVERY
customer's login attempt. Limiting by source IP would either be a no-op
(many egress IPs) or, worse, let one attacker's failed attempts lock out
every other customer sharing that IP — a cross-tenant denial of service.

The dimension that IS safe regardless of caller topology is the TARGET
account (email): lock an email out after too many consecutive failures,
independent of which IP/app sent them. A correct password immediately clears
the lockout (record_success) — a real owner mistyping their password is not
punished once they get it right.

Caveat (documented, not silently hidden): platform-api runs 2 uvicorn
workers (docker/Dockerfile.platform) and this state is per-process — the
effective attempt budget is up to ~2x MAX_ATTEMPTS across the pair, not a
hard global cap. A DB-backed counter would close that gap; deferred (needs a
migration, out of scope for this hardening pass).

Fail-open: any bug in this module must never lock out a legitimate user or
break login. Errors are logged loud and treated as "not locked" /
"not recorded".
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def _max_attempts() -> int:
    return int(os.getenv("BRIDGE_LOGIN_MAX_ATTEMPTS", "5"))


def lockout_window_s() -> float:
    return float(os.getenv("BRIDGE_LOGIN_LOCKOUT_WINDOW_S", "300"))


def _max_tracked_emails() -> int:
    """Bound memory: an attacker spraying millions of distinct emails must
    not grow this dict unbounded."""
    return int(os.getenv("BRIDGE_LOGIN_THROTTLE_MAX_TRACKED", "50000"))


@dataclass
class _Window:
    count: int
    first_attempt_ts: float


_attempts: Dict[str, _Window] = {}


def _normalize(email: str) -> str:
    return (email or "").strip().lower()


def _prune_expired(now: float, window_s: float) -> None:
    expired = [k for k, w in _attempts.items() if (now - w.first_attempt_ts) > window_s]
    for k in expired:
        _attempts.pop(k, None)


def is_locked_out(email: Optional[str]) -> bool:
    """True if this email has >= MAX_ATTEMPTS failed logins within the window.

    Deliberately gives the identical answer for an unknown email as for a
    known one with the same failure count — locking out by email alone adds
    no account-enumeration signal beyond what already exists.
    """
    try:
        key = _normalize(email or "")
        if not key:
            return False
        now = time.monotonic()
        w = _attempts.get(key)
        if w is None:
            return False
        if (now - w.first_attempt_ts) > lockout_window_s():
            _attempts.pop(key, None)
            return False
        return w.count >= _max_attempts()
    except Exception as e:  # noqa: BLE001 — fail-open, never break login
        logger.warning("login_throttle.is_locked_out failed (fail-open): %s", e)
        return False


def record_failure(email: Optional[str]) -> None:
    """Record one failed login attempt for this email."""
    try:
        key = _normalize(email or "")
        if not key:
            return
        now = time.monotonic()
        window_s = lockout_window_s()
        w = _attempts.get(key)
        if w is None or (now - w.first_attempt_ts) > window_s:
            if len(_attempts) >= _max_tracked_emails():
                _prune_expired(now, window_s)
            _attempts[key] = _Window(count=1, first_attempt_ts=now)
        else:
            w.count += 1
    except Exception as e:  # noqa: BLE001 — fail-open, never break login
        logger.warning("login_throttle.record_failure failed (fail-open): %s", e)


def record_success(email: Optional[str]) -> None:
    """Clear any failure state for this email. A correct password proves
    account ownership; locking the real owner out would itself be a DoS."""
    try:
        key = _normalize(email or "")
        if key:
            _attempts.pop(key, None)
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.warning("login_throttle.record_success failed (fail-open): %s", e)


def _reset_for_tests() -> None:
    """Test-only: clear all tracked state. Not used by production code paths."""
    _attempts.clear()
