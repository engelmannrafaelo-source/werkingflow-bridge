"""
Request Limiter - Prevent Concurrent Overload

Limits concurrent requests per worker to prevent Claude Code SDK overload.
When limit is reached, returns 503 immediately so the client can retry.

Usage (FastAPI Dependency — avoids Python 3.13 + Starlette BaseHTTPMiddleware bug):
    @app.post("/v1/chat/completions")
    async def chat_completions(..., _=Depends(concurrency_limit)):
        ...
"""

import asyncio
import os
import psutil
from contextlib import asynccontextmanager
from typing import Optional
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from fastapi import HTTPException

from config.logging_config import get_logger

logger = get_logger(__name__)

# Worker name for log context (set via WORKER_NAME env var in docker-compose)
WORKER_NAME = os.getenv("WORKER_NAME", "worker")


class RequestLimiter:
    """
    Tracks active requests and enforces concurrency limits.
    Monitors system memory to prevent overload.
    """

    def __init__(self, max_concurrent: int = 3, memory_threshold_percent: float = 90.0):
        """
        Args:
            max_concurrent: Maximum concurrent requests allowed (default: 3)
            memory_threshold_percent: Reject requests if memory usage exceeds this % (default: 90%)
        """
        self.max_concurrent = max_concurrent
        self.memory_threshold = memory_threshold_percent
        self.active_requests = 0
        self.total_requests = 0
        self.rejected_requests = 0
        self.lock = asyncio.Lock()

        logger.info("ℹ️  Request Limiter initialized:")
        logger.info(f"   Max concurrent: {max_concurrent}")
        logger.info(f"   Memory threshold: {memory_threshold_percent}%")

    async def can_accept_request(self) -> tuple[bool, Optional[str]]:
        """
        Check if new request can be accepted.

        Returns:
            (can_accept, reason_if_rejected)
        """
        async with self.lock:
            # Check concurrent limit
            if self.active_requests >= self.max_concurrent:
                reason = f"Max concurrent requests reached ({self.active_requests}/{self.max_concurrent})"
                self.rejected_requests += 1
                logger.error(
                    f"🚫 [{WORKER_NAME}] OVERLOAD — {reason} "
                    f"(total_rejected={self.rejected_requests}, total_requests={self.total_requests})"
                )
                return False, reason

            # Check memory usage
            memory = psutil.virtual_memory()
            if memory.percent >= self.memory_threshold:
                reason = f"Memory threshold exceeded ({memory.percent:.1f}% >= {self.memory_threshold}%)"
                self.rejected_requests += 1
                logger.error(
                    f"🚫 [{WORKER_NAME}] MEMORY OVERLOAD — {reason} "
                    f"used={memory.used / 1024**3:.1f}GB / {memory.total / 1024**3:.1f}GB "
                    f"(total_rejected={self.rejected_requests})"
                )
                return False, reason

            return True, None

    async def acquire(self):
        """Mark request as active"""
        async with self.lock:
            self.active_requests += 1
            self.total_requests += 1

            memory = psutil.virtual_memory()
            logger.info(
                f"▶️  [{WORKER_NAME}] Request accepted "
                f"(active: {self.active_requests}/{self.max_concurrent}, mem: {memory.percent:.1f}%)"
            )

    async def release(self):
        """Mark request as completed"""
        async with self.lock:
            self.active_requests = max(0, self.active_requests - 1)

            memory = psutil.virtual_memory()
            logger.info(
                f"✅ [{WORKER_NAME}] Request completed "
                f"(active: {self.active_requests}/{self.max_concurrent}, mem: {memory.percent:.1f}%)"
            )

    @asynccontextmanager
    async def throttled(self):
        """
        Async context manager: acquire on enter, release on exit.
        Raises HTTPException(503) if limit reached.

        Usage:
            async with request_limiter.throttled():
                ... do work ...
        """
        can_accept, reason = await self.can_accept_request()
        if not can_accept:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "Bridge overloaded — too many concurrent requests",
                    "reason": reason,
                    "retry_after_seconds": 15,
                    "worker": WORKER_NAME,
                    "stats": self.get_stats(),
                }
            )
        await self.acquire()
        try:
            yield
        finally:
            await self.release()

    def get_stats(self) -> dict:
        """Get current limiter statistics"""
        memory = psutil.virtual_memory()
        return {
            'active_requests': self.active_requests,
            'max_concurrent': self.max_concurrent,
            'total_requests': self.total_requests,
            'rejected_requests': self.rejected_requests,
            'memory_usage_percent': memory.percent,
            'memory_used_gb': memory.used / 1024**3,
            'memory_total_gb': memory.total / 1024**3,
            'memory_threshold': self.memory_threshold
        }


class RequestLimiterMiddleware(BaseHTTPMiddleware):
    """
    FastAPI/Starlette middleware for request limiting.
    """

    def __init__(self, app, limiter: RequestLimiter):
        super().__init__(app)
        self.limiter = limiter

    async def dispatch(self, request, call_next):
        # Skip health checks and metrics
        if request.url.path in ['/health', '/metrics', '/stats']:
            return await call_next(request)

        # Check if request can be accepted
        can_accept, reason = await self.limiter.can_accept_request()

        if not can_accept:
            self.limiter.rejected_requests += 1
            logger.error(f"❌ Request rejected: {reason}")

            return JSONResponse(
                status_code=503,  # Service Unavailable
                content={
                    'error': 'Service Temporarily Unavailable',
                    'reason': reason,
                    'retry_after_seconds': 30,
                    'stats': self.limiter.get_stats()
                }
            )

        # Accept request
        await self.limiter.acquire()

        try:
            response = await call_next(request)
            return response
        finally:
            await self.limiter.release()


class PureASGIRequestLimiter:
    """
    Pure ASGI middleware for concurrent request limiting.
    Compatible with Python 3.13 + Starlette 0.46 (no BaseHTTPMiddleware issues).

    Usage:
        app.add_middleware(PureASGIRequestLimiter, max_concurrent=5, memory_threshold=90.0)
    """

    def __init__(self, app, max_concurrent: int = 5, memory_threshold: float = 90.0):
        self.app = app
        self.limiter = get_limiter(max_concurrent=max_concurrent, memory_threshold=memory_threshold)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in ["/health", "/metrics", "/stats", "/lb-status"]:
            await self.app(scope, receive, send)
            return

        can_accept, reason = await self.limiter.can_accept_request()
        if not can_accept:
            import json as _json
            body = _json.dumps({
                "error": "Bridge overloaded — too many concurrent requests",
                "reason": reason,
                "retry_after_seconds": 15,
                "worker": WORKER_NAME,
                "stats": self.limiter.get_stats(),
            }).encode()
            await send({
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"retry-after", b"15"],
                    [b"content-length", str(len(body)).encode()],
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        await self.limiter.acquire()
        try:
            await self.app(scope, receive, send)
        finally:
            await self.limiter.release()


# Global limiter instance
limiter: Optional[RequestLimiter] = None


def get_limiter(max_concurrent: int = 5, memory_threshold: float = 90.0) -> RequestLimiter:
    """Get or create global limiter instance"""
    global limiter
    if limiter is None:
        limiter = RequestLimiter(max_concurrent, memory_threshold)
    return limiter


async def concurrency_limit():
    """
    FastAPI Dependency — enforces concurrent request limit.

    Add to any endpoint that runs Claude Code SDK:
        @app.post("/v1/chat/completions")
        async def chat_completions(..., _=Depends(concurrency_limit)):

    Raises HTTPException(503) with retry_after when limit reached.
    Avoids Python 3.13 + Starlette 0.46 BaseHTTPMiddleware bug.
    """
    if limiter is None:
        return  # Limiter not initialized yet — let request through
    async with limiter.throttled():
        yield
