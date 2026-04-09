"""
Performance Monitoring Middleware for ECO OpenAI Wrapper.

Features:
- Request duration tracking
- Slow request warnings (>5s)
- Performance metrics logging
- Endpoint-specific performance monitoring
- Pure ASGI implementation (streaming-safe)

Usage:
    from middleware.performance_monitor import PerformanceMonitorMiddleware
    app.add_middleware(PerformanceMonitorMiddleware)
"""

import time
import json
import os
from typing import Callable
from starlette.types import ASGIApp, Scope, Receive, Send, Message
from config.logging_config import get_logger

logger = get_logger(__name__)


class PerformanceMonitorMiddleware:
    """
    Pure ASGI Middleware to monitor request performance and log slow requests.

    Streaming-safe implementation that does not block on responses.

    Tracks:
    - Request duration for all endpoints
    - Slow request warnings (configurable threshold)
    - Performance metrics per endpoint
    - Tool-aware thresholds (separate for tool-enabled requests)

    Configuration (via .env):
        SLOW_REQUEST_THRESHOLD=5.0         # Slow request warning for non-tool (default: 5.0s)
        VERY_SLOW_REQUEST_THRESHOLD=10.0   # Very slow request error for non-tool (default: 10.0s)
        SLOW_REQUEST_THRESHOLD_TOOLS=30.0  # Slow request warning for tool-enabled (default: 30.0s)
        VERY_SLOW_REQUEST_THRESHOLD_TOOLS=60.0  # Very slow for tool-enabled (default: 60.0s)

    Recommended: Analyze real durations first, then set thresholds to avg + 1σ and avg + 2σ
    """

    def __init__(self, app: ASGIApp):
        self.app = app

        # Load thresholds from environment or use defaults
        # Non-tool thresholds (fast requests)
        self.slow_threshold = float(os.getenv('SLOW_REQUEST_THRESHOLD', '5.0'))
        self.very_slow_threshold = float(os.getenv('VERY_SLOW_REQUEST_THRESHOLD', '10.0'))

        # Tool-enabled thresholds (slower requests expected)
        self.slow_threshold_tools = float(os.getenv('SLOW_REQUEST_THRESHOLD_TOOLS', '30.0'))
        self.very_slow_threshold_tools = float(os.getenv('VERY_SLOW_REQUEST_THRESHOLD_TOOLS', '60.0'))

        logger.info(
            f"Performance Monitor initialized (Pure ASGI):\n"
            f"  Non-tool requests: slow={self.slow_threshold}s, very_slow={self.very_slow_threshold}s\n"
            f"  Tool requests: slow={self.slow_threshold_tools}s, very_slow={self.very_slow_threshold_tools}s"
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        ASGI application interface.

        Args:
            scope: ASGI scope dict with request info
            receive: ASGI receive callable for incoming messages
            send: ASGI send callable for outgoing messages
        """
        # Only process HTTP requests
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Extract request info from scope
        method = scope["method"]
        path = scope["path"]
        client = scope.get("client", ("unknown", 0))
        client_host = client[0] if client else "unknown"

        # Start timer
        start_time = time.time()

        # Track tool detection and body
        tools_enabled = False
        body_chunks = []

        # Wrap receive to detect tools from body
        async def receive_with_tool_detection() -> Message:
            nonlocal tools_enabled

            message = await receive()

            # Capture body for tool detection (only for /v1/chat/completions POST)
            if message["type"] == "http.request" and method == "POST" and path == "/v1/chat/completions":
                body_chunk = message.get("body", b"")
                if body_chunk:
                    body_chunks.append(body_chunk)

                # If this is the last chunk, parse for tool detection
                if not message.get("more_body", False) and body_chunks:
                    try:
                        full_body = b"".join(body_chunks)
                        body_data = json.loads(full_body.decode())
                        tools_enabled = body_data.get('enable_tools', False)

                        # Log tool detection
                        if tools_enabled:
                            logger.debug(f"Tool usage detected for: {method} {path}")
                    except Exception as e:
                        # If parsing fails, assume non-tool
                        logger.debug(f"Could not parse body for tool detection: {e}")

            return message

        # Track response status and completion
        response_status = None
        response_started = False

        async def send_with_timing(message: Message) -> None:
            nonlocal response_status, response_started

            if message["type"] == "http.response.start":
                response_status = message["status"]
                response_started = True

            # Log when response is complete (last body chunk)
            elif message["type"] == "http.response.body":
                if not message.get("more_body", False):
                    # Calculate duration
                    duration = time.time() - start_time

                    # Select thresholds based on tool usage
                    if tools_enabled:
                        slow = self.slow_threshold_tools
                        very_slow = self.very_slow_threshold_tools
                        threshold_type = "tools"
                    else:
                        slow = self.slow_threshold
                        very_slow = self.very_slow_threshold
                        threshold_type = "non-tools"

                    # Log based on duration
                    if duration >= very_slow:
                        logger.error(
                            f"VERY SLOW REQUEST [{threshold_type}]: {method} {path} - {duration:.2f}s "
                            f"(threshold: {very_slow}s) status={response_status} client={client_host}"
                        )
                    elif duration >= slow:
                        logger.warning(
                            f"Slow request [{threshold_type}]: {method} {path} - {duration:.2f}s "
                            f"(threshold: {slow}s) status={response_status} client={client_host}"
                        )
                    else:
                        logger.info(
                            f"Request completed [{threshold_type}]: {method} {path} - {duration:.3f}s "
                            f"status={response_status} client={client_host}"
                        )

                    # Record metrics (in-memory)
                    metrics.record_request(
                        endpoint=path,
                        duration=duration,
                        slow_threshold=slow,
                        very_slow_threshold=very_slow
                    )

                    # Persist to JSONL (disk)
                    try:
                        from src.middleware.bridge_metrics_store import get_request_log
                        get_request_log().record(
                            method=method,
                            endpoint=path,
                            status_code=response_status or 0,
                            duration_s=duration,
                            tools_enabled=tools_enabled,
                            client_ip=client_host,
                        )
                    except Exception as _rl_err:
                        logger.debug(f"Request log write failed: {_rl_err}")

            # Send original message
            await send(message)

        # Process request
        try:
            await self.app(scope, receive_with_tool_detection, send_with_timing)
        except Exception as e:
            # Log exception with duration
            duration = time.time() - start_time
            logger.error(
                f"Request failed: {method} {path} - {type(e).__name__}: {str(e)} "
                f"(duration: {duration:.2f}s)"
            )
            raise


class RequestMetrics:
    """
    Track and report request performance metrics.

    Provides aggregated statistics for monitoring and optimization.
    """

    def __init__(self):
        self.request_count = 0
        self.total_duration = 0.0
        self.slow_requests = 0
        self.very_slow_requests = 0
        self.endpoint_metrics = {}

    def record_request(
        self,
        endpoint: str,
        duration: float,
        slow_threshold: float = 5.0,
        very_slow_threshold: float = 10.0
    ):
        """
        Record request metrics.

        Args:
            endpoint: API endpoint path
            duration: Request duration in seconds
            slow_threshold: Threshold for slow request warning
            very_slow_threshold: Threshold for very slow request error
        """
        self.request_count += 1
        self.total_duration += duration

        # Track slow requests
        if duration >= very_slow_threshold:
            self.very_slow_requests += 1
        elif duration >= slow_threshold:
            self.slow_requests += 1

        # Track per-endpoint metrics
        if endpoint not in self.endpoint_metrics:
            self.endpoint_metrics[endpoint] = {
                'count': 0,
                'total_duration': 0.0,
                'min_duration': float('inf'),
                'max_duration': 0.0,
                'slow_count': 0,
                'very_slow_count': 0
            }

        endpoint_data = self.endpoint_metrics[endpoint]
        endpoint_data['count'] += 1
        endpoint_data['total_duration'] += duration
        endpoint_data['min_duration'] = min(endpoint_data['min_duration'], duration)
        endpoint_data['max_duration'] = max(endpoint_data['max_duration'], duration)

        if duration >= very_slow_threshold:
            endpoint_data['very_slow_count'] += 1
        elif duration >= slow_threshold:
            endpoint_data['slow_count'] += 1

    def get_summary(self) -> dict:
        """
        Get performance metrics summary.

        Returns:
            Dictionary with aggregated metrics
        """
        avg_duration = (
            self.total_duration / self.request_count
            if self.request_count > 0
            else 0.0
        )

        summary = {
            'total_requests': self.request_count,
            'average_duration': round(avg_duration, 3),
            'slow_requests': self.slow_requests,
            'very_slow_requests': self.very_slow_requests,
            'endpoints': {}
        }

        # Add per-endpoint stats
        for endpoint, endpoint_data in self.endpoint_metrics.items():
            endpoint_avg = (
                endpoint_data['total_duration'] / endpoint_data['count']
                if endpoint_data['count'] > 0
                else 0.0
            )

            summary['endpoints'][endpoint] = {
                'count': endpoint_data['count'],
                'avg_duration': round(endpoint_avg, 3),
                'min_duration': round(endpoint_data['min_duration'], 3),
                'max_duration': round(endpoint_data['max_duration'], 3),
                'slow_count': endpoint_data['slow_count'],
                'very_slow_count': endpoint_data['very_slow_count']
            }

        return summary

    def log_summary(self):
        """Log performance metrics summary."""
        summary = self.get_summary()
        logger.info(f"Performance Summary: {summary}")


# Global metrics instance
metrics = RequestMetrics()
