"""
Webhook HMAC signature — shared between Bridge dispatcher and App receivers.

Bridge POSTs each auth-token webhook with:

    X-Bridge-Timestamp: <unix-seconds, int>
    X-Bridge-Nonce:     <16-byte hex>
    X-Bridge-Signature: <hex SHA-256 HMAC>
    Content-Type:       application/json
    body:               JSON

The signature input is the literal string `<timestamp>.<nonce>.<body>` where
`body` is the exact UTF-8 bytes that hit the wire. Apps re-compute the same
string and `compare_digest()` against the header.

Body-only signing is INSUFFICIENT — without timestamp + nonce, an attacker
who captures one delivery can replay it forever. Timestamp gives apps a
freshness check (reject > 5min skew). Nonce gives apps idempotency-via-cache
so a within-skew replay is also rejected.

This module is intentionally framework-agnostic and pure-Python: apps in
TypeScript / other languages reimplement the same string-concatenation +
SHA-256 HMAC + constant-time compare. ADR cross-app/0002 Phase M2 references
this string format as the spec.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Tuple


# Max clock skew apps should accept between Bridge `X-Bridge-Timestamp` and
# their wall clock. 5 minutes matches Mollie / Stripe convention — generous
# enough for slow VMs, tight enough that a replayed delivery from yesterday
# is rejected.
MAX_SIGNATURE_AGE_SECONDS: int = 5 * 60


def _signing_input(timestamp: int, nonce: str, body: bytes) -> bytes:
    """Build the exact byte string fed to HMAC. Caller and verifier MUST
    use identical bytes — that's why `body` is passed in as the raw bytes
    that hit the wire, not the parsed JSON dict."""
    return f"{timestamp}.{nonce}.".encode("utf-8") + body


def sign(secret: str, body: bytes, *, timestamp: int | None = None, nonce: str | None = None) -> Tuple[str, int, str]:
    """
    Compute the signature header value for a request.

    Returns (signature_hex, timestamp, nonce) — caller fills the three
    X-Bridge-* headers. `timestamp` and `nonce` may be provided to make
    a request reproducible from tests; production callers omit them.

    `secret` must be the same string the receiver uses (Bridge side: read
    from BRIDGE_WEBHOOK_SECRET_<APP>; App side: from app-local Infisical).
    Hex encoding keeps the header ASCII-clean for nginx / observability
    pipelines that mangle binary.
    """
    if timestamp is None:
        timestamp = int(time.time())
    if nonce is None:
        # 16 bytes = 32 hex chars = 128 bits of entropy. The cache-window is
        # ~5min so even at 1k rps a collision is astronomically unlikely.
        nonce = secrets.token_hex(16)
    mac = hmac.new(
        secret.encode("utf-8"),
        _signing_input(timestamp, nonce, body),
        hashlib.sha256,
    )
    return mac.hexdigest(), timestamp, nonce


def verify(
    secret: str,
    body: bytes,
    *,
    timestamp: int,
    nonce: str,
    signature_hex: str,
    now: int | None = None,
    max_age_seconds: int = MAX_SIGNATURE_AGE_SECONDS,
) -> bool:
    """
    Verify a received webhook. Returns True iff:
        - signature matches HMAC-SHA256(secret, "<ts>.<nonce>.<body>")
        - |now - timestamp| <= max_age_seconds (replay protection)

    Nonce idempotency (rejecting a repeat-of-a-still-fresh delivery) is the
    receiver's responsibility — this function deliberately stays stateless.
    Apps should cache seen nonces for at least `max_age_seconds`.

    `hmac.compare_digest` is used to defeat timing oracles even though hex
    comparison would also be fine — same posture as
    src/api_auth/deps.py:_verify_service_token.
    """
    if now is None:
        now = int(time.time())
    if abs(now - timestamp) > max_age_seconds:
        return False
    expected, _, _ = sign(secret, body, timestamp=timestamp, nonce=nonce)
    return hmac.compare_digest(expected, signature_hex)
