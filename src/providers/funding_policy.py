"""Operator funding policy, independent of caller-supplied routing wishes.

Energy uses verified promotional credits only (Rafael, 2026-10-30).
No subscription, foreign provider or legacy key is a fallback for this app.
"""
from fastapi import HTTPException

def promotional_only(request):
    return request is not None and request.headers.get("x-app-id", "").strip().lower() == "werking-energy"

def require_pool():
    from src.providers.credit_pool import enabled
    if not enabled():
        raise HTTPException(503, "promotional_only: credit coordinator missing; no paid fallback")
