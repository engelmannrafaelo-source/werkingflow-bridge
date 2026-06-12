"""
Unit tests for the circuit-breaker semantics in providers/fallback.py.

Regression guard for the stuck-"down" deadlock: a transient 429 / throttle was
recorded as a breaker failure, pinning a provider "down" in /health forever (no
success to reset it, no time-based recovery — visible as
`claude-premium: down, dep_bridge_error: throttle, consecutive_failures: 5`
while the accounts had plenty of quota).

Invariants:
  1. Rate-limits / throttles / token-exhaustion do NOT trip the breaker.
  2. Genuine brokenness (5xx / connection / timeout) DOES.
  3. The breaker self-heals after BREAKER_RECOVERY_SECONDS.
  4. 429 is still *retryable* (fallback still fires) even though it is not a
     breaker failure — the two concepts are independent.
"""

import time
import httpx
import pytest

from src.providers.fallback import (
    ProviderHealth,
    is_breaker_failure,
    is_retryable_error,
    record_failure,
    record_success,
    get_provider_health,
    BREAKER_DOWN_THRESHOLD,
    BREAKER_RECOVERY_SECONDS,
)
from src.auth import AllTokensExhausted
from src.providers.openai_compatible import ProviderError


class TestRateLimitDoesNotTripBreaker:
    def test_429_is_not_a_breaker_failure(self):
        assert is_breaker_failure(ProviderError(429, "rate limited")) is False

    def test_throttle_runtimeerror_is_not_a_breaker_failure(self):
        assert is_breaker_failure(RuntimeError("dep_bridge_error: throttle")) is False

    def test_token_exhaustion_is_not_a_breaker_failure(self):
        assert is_breaker_failure(AllTokensExhausted()) is False

    def test_429_is_still_retryable(self):
        # Independent of the breaker — a 429 must still trigger a fallback attempt.
        assert is_retryable_error(ProviderError(429, "rate limited")) is True


class TestGenuineBrokennessTripsBreaker:
    @pytest.mark.parametrize("code", [500, 502, 503, 504])
    def test_5xx_is_a_breaker_failure(self, code):
        assert is_breaker_failure(ProviderError(code, "boom")) is True

    def test_connection_timeout_is_a_breaker_failure(self):
        assert is_breaker_failure(httpx.ConnectTimeout("x")) is True

    def test_runtimeerror_5xx_is_a_breaker_failure(self):
        assert is_breaker_failure(RuntimeError("upstream 503 boom")) is True


class TestBreakerSelfHeals:
    def test_status_down_then_recovers_after_cooldown(self):
        h = ProviderHealth()
        h.consecutive_failures = BREAKER_DOWN_THRESHOLD + 2
        h.last_error = time.time()
        assert h.status == "down"

        # Recovery window elapsed with no fresh failure → self-healed.
        h.last_error = time.time() - (BREAKER_RECOVERY_SECONDS + 5)
        assert h.status == "up"

    def test_record_failure_decays_stale_streak(self):
        tier = "test-tier-decay"
        h = get_provider_health(tier)
        h.consecutive_failures = 5
        h.last_error = time.time() - (BREAKER_RECOVERY_SECONDS + 5)

        record_failure(tier, "upstream 500")
        # Stale streak decayed to 0, then this fresh failure counted as 1.
        assert h.consecutive_failures == 1
        assert h.status == "degraded"

    def test_record_success_resets(self):
        tier = "test-tier-success"
        h = get_provider_health(tier)
        h.consecutive_failures = 4
        record_success(tier)
        assert h.consecutive_failures == 0
        assert h.status == "up"
