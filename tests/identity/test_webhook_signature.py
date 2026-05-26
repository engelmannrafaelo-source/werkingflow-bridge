"""
Tests for src.identity.webhook_signature — HMAC sign/verify helper shared by
the Bridge dispatcher and (eventually) App-side receivers.

The signing input format `<timestamp>.<nonce>.<body>` is the contract apps
in other languages must reimplement, so the round-trip + edge cases here
double as the spec.
"""
from __future__ import annotations

import time

import pytest

from src.identity.webhook_signature import (
    MAX_SIGNATURE_AGE_SECONDS,
    sign,
    verify,
)


class TestSign:
    def test_sign_returns_three_parts(self):
        sig, ts, nonce = sign("s3cret", b'{"a":1}')
        assert isinstance(sig, str) and len(sig) == 64
        int(sig, 16)  # hex
        assert isinstance(ts, int) and ts > 1_700_000_000
        assert isinstance(nonce, str) and len(nonce) == 32
        int(nonce, 16)  # hex

    def test_sign_is_deterministic_when_inputs_pinned(self):
        sig1, _, _ = sign("k", b"body", timestamp=1234, nonce="nn")
        sig2, _, _ = sign("k", b"body", timestamp=1234, nonce="nn")
        assert sig1 == sig2

    def test_sign_changes_with_each_input(self):
        base, _, _ = sign("k", b"body", timestamp=1, nonce="n")
        # different secret
        a, _, _ = sign("k2", b"body", timestamp=1, nonce="n")
        # different body
        b, _, _ = sign("k", b"body2", timestamp=1, nonce="n")
        # different ts
        c, _, _ = sign("k", b"body", timestamp=2, nonce="n")
        # different nonce
        d, _, _ = sign("k", b"body", timestamp=1, nonce="m")
        assert {base, a, b, c, d} == {base, a, b, c, d}  # all distinct
        assert len({base, a, b, c, d}) == 5

    def test_sign_default_nonce_is_unique(self):
        nonces = {sign("k", b"x")[2] for _ in range(20)}
        assert len(nonces) == 20  # 128-bit entropy makes collision unreachable


class TestVerify:
    def test_round_trip(self):
        body = b'{"hello":"world"}'
        sig, ts, nonce = sign("topsecret", body)
        assert verify("topsecret", body, timestamp=ts, nonce=nonce, signature_hex=sig)

    def test_rejects_tampered_body(self):
        sig, ts, nonce = sign("topsecret", b'{"a":1}')
        assert not verify(
            "topsecret", b'{"a":2}', timestamp=ts, nonce=nonce, signature_hex=sig,
        )

    def test_rejects_wrong_secret(self):
        sig, ts, nonce = sign("right", b"x")
        assert not verify("wrong", b"x", timestamp=ts, nonce=nonce, signature_hex=sig)

    def test_rejects_wrong_nonce(self):
        sig, ts, _ = sign("k", b"x", timestamp=1234, nonce="real-nonce")
        assert not verify(
            "k", b"x", timestamp=ts, nonce="fake-nonce", signature_hex=sig,
        )

    def test_rejects_wrong_timestamp(self):
        sig, _, nonce = sign("k", b"x", timestamp=1234, nonce="n")
        # Same content, different ts — both replay protection AND signature mismatch
        assert not verify(
            "k", b"x", timestamp=9999, nonce=nonce, signature_hex=sig,
        )

    def test_rejects_stale_timestamp(self):
        """Timestamp older than MAX_SIGNATURE_AGE_SECONDS must fail BEFORE
        we even bother computing the HMAC — that's the replay defence."""
        now = int(time.time())
        stale_ts = now - (MAX_SIGNATURE_AGE_SECONDS + 10)
        sig, _, nonce = sign("k", b"x", timestamp=stale_ts, nonce="n")
        assert not verify(
            "k", b"x", timestamp=stale_ts, nonce=nonce,
            signature_hex=sig, now=now,
        )

    def test_rejects_future_timestamp(self):
        """Clock-skew in either direction defeats verification."""
        now = int(time.time())
        future_ts = now + (MAX_SIGNATURE_AGE_SECONDS + 10)
        sig, _, nonce = sign("k", b"x", timestamp=future_ts, nonce="n")
        assert not verify(
            "k", b"x", timestamp=future_ts, nonce=nonce,
            signature_hex=sig, now=now,
        )

    def test_accepts_within_skew(self):
        now = int(time.time())
        ts = now - (MAX_SIGNATURE_AGE_SECONDS - 5)  # just inside the window
        sig, _, nonce = sign("k", b"x", timestamp=ts, nonce="n")
        assert verify("k", b"x", timestamp=ts, nonce=nonce, signature_hex=sig, now=now)

    def test_max_age_seconds_override(self):
        """Tight max-age (e.g. 60s) should reject something the default 5min would accept."""
        now = int(time.time())
        ts = now - 120  # 2min stale
        sig, _, nonce = sign("k", b"x", timestamp=ts, nonce="n")
        # default 5min: OK
        assert verify("k", b"x", timestamp=ts, nonce=nonce, signature_hex=sig, now=now)
        # tight 60s: rejected
        assert not verify(
            "k", b"x", timestamp=ts, nonce=nonce, signature_hex=sig,
            now=now, max_age_seconds=60,
        )
