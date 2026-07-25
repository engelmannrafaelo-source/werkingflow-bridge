"""Worker-pool saturation signal for the research-cloud overflow decision.

DESIGN.md asks for "Autobahn-Signale: Account-Cooldowns/Pool-Sättigung" as the
trigger for automatic overflow. There is no existing cluster-wide "is the
whole pool exhausted" boolean in this codebase (verified — grep for
should_reject_new_request / adaptive_limiter call sites found only per-worker,
per-request checks). Building a real cross-worker aggregate would mean
querying every worker's account-pool-state via the metrics-reader, which is
more than this decision needs: the research job is going to be dispatched to
THIS worker process regardless (nginx already routed the request to it), so
what actually matters is whether THIS worker's own capacity is exhausted
right now.

Chosen signal (documented choice, not the only possible one): OR of
  1. RateLimitTracker.should_reject_new_request(worker) — this worker's
     account is hard/soft rate-limited by Anthropic.
  2. AdaptiveLoadLimiter — in-flight tokens have reached the tuned effective
     cap (or the cap has collapsed to ~0 under weekly-budget throttling).

Both signals already exist and are read-only checks (no side effects), so
combining them here adds no new failure surface. See DESIGN.md for the
"open point" this leaves: a cluster-wide signal (all N workers saturated, not
just this one) would be a better trigger if research traffic gets distributed
unevenly across workers — not built here, flagged for Rafael.
"""
from __future__ import annotations

import os


def is_worker_pool_saturated() -> bool:
    """True iff this worker's own subscription-pool capacity looks exhausted."""
    from src.claude_cli import rate_limit_tracker
    from src.middleware.adaptive_limiter import get_adaptive_limiter

    worker_id = os.getenv("INSTANCE_NAME", "unknown")
    if rate_limit_tracker.should_reject_new_request(worker_id):
        return True

    snap = get_adaptive_limiter().snapshot()
    effective_cap = snap.get("effective_cap_tokens") or 0
    inflight = snap.get("inflight_tokens") or 0
    if effective_cap <= 0:
        # Cap collapsed (e.g. weekly-budget multiplier hit 0) — treat as saturated.
        return True
    return inflight >= effective_cap
