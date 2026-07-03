import asyncio
import json
import os
import subprocess
import sys
import uuid
import time
import hashlib
import tempfile
from typing import AsyncGenerator, Dict, Any, Optional, List
from pathlib import Path
from contextvars import ContextVar

from claude_code_sdk import query, ClaudeCodeOptions, Message
from claude_code_sdk._errors import MessageParseError
from config.logging_config import get_logger
from datetime import datetime, timedelta
import shutil

# File discovery for /sc:research
from src.file_discovery import FileDiscoveryService, FileMetadata, SDKMessageParsingError, DirectoryScanError

logger = get_logger(__name__)


# =============================================================================
# MONKEY-PATCH: Make Python SDK resilient to unknown message types
# =============================================================================
# The Python SDK (claude-code-sdk) crashes on unrecognized message types like
# "rate_limit_event" (introduced in Node.js SDK v2.1.62+). This kills the entire
# async generator, losing all subsequent messages.
#
# CRITICAL FIX (Apr 2026):
# "rate_limit_event" is NOT an error — it means the CLI detected a rate limit
# and is WAITING to retry internally. The SDK should let the CLI handle it.
# Previously we raised WorkerUnavailableError on rate-limit text detection,
# which ABORTED the task. Now we:
# 1. Parse rate_limit_event → log + track state (for monitoring/new request routing)
# 2. Let the stream continue → CLI waits + retries → task completes
# 3. Only abort if the overall timeout is exceeded
# =============================================================================

# Sentinel class for rate_limit_event messages (SDK doesn't have a type for these)
class RateLimitEvent:
    """Parsed rate_limit_event from Claude Code CLI. Not an error — CLI is retrying."""
    def __init__(self, data: dict):
        self.type = "rate_limit_event"
        self.retry_after = data.get("retry_after", None)
        self.reset_at = data.get("reset_at", None)
        self.message = data.get("message", "")
        self.raw = data

    def __repr__(self):
        return f"RateLimitEvent(retry_after={self.retry_after}, message={self.message[:80]})"

try:
    import claude_code_sdk._internal.client as _sdk_client
    from claude_code_sdk._internal.message_parser import parse_message as _original_parse_message

    _SKIPPED_TYPES_LOG = set()  # Track which types we've logged (avoid spam)

    def _resilient_parse_message(data):
        """Wrapper that handles unknown message types instead of crashing."""
        try:
            return _original_parse_message(data)
        except MessageParseError as e:
            error_msg = str(e).lower()
            if "unknown message type" in error_msg:
                msg_type = data.get("type", "unknown") if isinstance(data, dict) else "unknown"

                # SPECIAL HANDLING: rate_limit_event — parse it, don't skip
                if msg_type == "rate_limit_event":
                    # Diagnostic: dump raw payload so we can verify whether
                    # Anthropic is sending real throttle info (retry_after,
                    # message, reset_at) or whether these are SDK-internal
                    # heartbeats with empty payload.
                    try:
                        _keys = list(data.keys()) if isinstance(data, dict) else "non-dict"
                        _raw_str = str(data)[:500]
                        logger.info(
                            f"⏳ rate_limit_event received — keys={_keys} raw={_raw_str}"
                        )
                    except Exception as _log_err:
                        logger.info(
                            f"⏳ rate_limit_event received (failed to dump raw: {_log_err})"
                        )
                    return RateLimitEvent(data)

                # All other unknown types: skip silently
                if msg_type not in _SKIPPED_TYPES_LOG:
                    logger.info(f"ℹ️ Skipping unrecognized SDK message type: {msg_type}")
                    _SKIPPED_TYPES_LOG.add(msg_type)
                return None  # Skip this message, continue stream
            raise  # Re-raise genuine parse errors

    _sdk_client.parse_message = _resilient_parse_message
    logger.info("✅ SDK message parser patched (rate_limit_event aware)")
except Exception as patch_err:
    logger.warning(f"⚠️ Could not patch SDK message parser: {patch_err}")
    logger.warning("   Unknown message types will crash the stream (fallback to except handler)")
# =============================================================================


# =============================================================================
# LARGE PROMPT HANDLING: Avoid OS ARG_MAX (typically ~128 KB on Linux)
#
# The Python SDK spawns (via anyio.open_process):
#   claude --output-format stream-json --system-prompt <value> ...
# When <value> exceeds ARG_MAX the OS rejects exec() with E2BIG (errno 7,
# "Argument list too long"). The SDK surfaces this silently as 503/capacity_busy.
#
# FIX for system_prompt: Intercept anyio.open_process (the function the SDK
# actually calls — NOT asyncio.create_subprocess_exec) before it reaches the OS.
# A ContextVar carries the temp-file path for the current async task; when set,
# --system-prompt-file <path> is appended to the claude command list instead of
# --system-prompt <large_value>.
# ContextVar gives each concurrent task its own isolated context (no global state).
#
# FIX for run_native_cli prompt: pass via stdin (input= in subprocess.run).
# =============================================================================

# Threshold: 100 KB — comfortably below the Linux ARG_MAX of ~128 KB
LARGE_ARG_THRESHOLD_BYTES = 100_000

# Per-async-task path to the temp file for the current request's large system_prompt.
# None when system_prompt is small enough to pass via argv.
_current_sys_prompt_tempfile: ContextVar[Optional[str]] = ContextVar(
    "_bridge_sys_prompt_tempfile", default=None
)

_open_process_patch_applied: bool = False


def _apply_open_process_patch() -> None:
    """Monkey-patch anyio.open_process once at module load.

    The claude_code_sdk subprocess transport spawns the CLI via
    `anyio.open_process(cmd, ...)` (claude_code_sdk/_internal/transport/
    subprocess_cli.py) — NOT asyncio.create_subprocess_exec. `cmd` is a list
    whose first element is the claude binary path (shutil.which("claude")).

    When _current_sys_prompt_tempfile is set for the current async task, this
    patch appends ["--system-prompt-file", <path>] to that command list so the
    CLI reads the (oversized) system prompt from a file instead of argv.
    ContextVar isolates concurrent async tasks.

    The SDK does `import anyio` and calls `anyio.open_process` (attribute lookup
    at call time), so patching the anyio module attribute is sufficient. We
    still verify the SDK transport actually resolves to our wrapper and FAIL
    LOUD if it does not — a silently-ineffective patch would drop large system
    prompts without error, which is exactly the failure mode this guards against.
    """
    global _open_process_patch_applied

    import anyio

    _original_open_process = anyio.open_process

    async def _intercepted_open_process(command, *args, **kwargs):
        tempfile_path = _current_sys_prompt_tempfile.get()
        if (
            tempfile_path
            and isinstance(command, (list, tuple))
            and command
            and str(command[0]).endswith("claude")
        ):
            # options.system_prompt was intentionally NOT set for this request
            # (so the SDK did not add --system-prompt <big>). Inject the file
            # variant instead, which the claude CLI reads off-argv.
            command = list(command) + ["--system-prompt-file", tempfile_path]
            logger.debug(
                f"🔧 open_process patch: injected --system-prompt-file {tempfile_path!r}"
            )
        return await _original_open_process(command, *args, **kwargs)

    anyio.open_process = _intercepted_open_process  # type: ignore[assignment]

    # Defensive: patch any module that bound the reference directly via
    # `from anyio import open_process` at import time (the 0.0.22 transport uses
    # `import anyio`, so this normally finds nothing — kept for forward-compat).
    patched_modules: list = []
    for _mod_name, _mod in list(sys.modules.items()):
        if "claude_code_sdk" in _mod_name:
            if getattr(_mod, "open_process", None) is _original_open_process:
                setattr(_mod, "open_process", _intercepted_open_process)
                patched_modules.append(_mod_name)

    # FAIL LOUD: confirm the SDK transport will actually hit our wrapper. If the
    # SDK ever changes its spawn path, this raises at startup instead of silently
    # dropping large system prompts at request time.
    import claude_code_sdk._internal.transport.subprocess_cli as _sdk_transport
    _effective = getattr(getattr(_sdk_transport, "anyio", None), "open_process", None)
    if _effective is not _intercepted_open_process and _sdk_transport.__dict__.get("open_process") is not _intercepted_open_process:
        raise RuntimeError(
            "anyio.open_process patch did not reach the SDK transport "
            "(claude_code_sdk._internal.transport.subprocess_cli). The SDK spawn "
            "path changed; large system_prompt delivery would silently break. "
            "Re-check how the installed claude_code_sdk spawns the CLI."
        )

    _open_process_patch_applied = True
    logger.info(
        f"✅ anyio.open_process patched for large system_prompt (ARG_MAX guard). "
        f"Extra SDK modules rebound: {patched_modules or 'none (uses import anyio)'}"
    )


try:
    _apply_open_process_patch()
except Exception as _sp_patch_err:
    logger.warning(
        f"⚠️ Could not apply anyio.open_process patch for large system_prompt: {_sp_patch_err}. "
        f"Requests with system_prompt > {LARGE_ARG_THRESHOLD_BYTES:,} bytes will fail loudly."
    )
# =============================================================================


# Custom Exceptions for Progress Tracking
class ProgressTrackingError(Exception):
    """Base exception for progress tracking failures"""
    pass


class SessionDirectoryError(ProgressTrackingError):
    """Failed to create or access session directory"""
    pass


class ProgressWriteError(ProgressTrackingError):
    """Failed to write progress data"""
    pass


class WorkerUnavailableError(Exception):
    """
    Raised when the Claude Code SDK fails in a way that indicates
    this worker cannot handle requests (auth error, rate limit, etc.).

    This triggers HTTP 503 response which allows Nginx to failover
    to another worker automatically.
    """
    pass


class RateLimitError(Exception):
    """
    Raised when rate limit is detected. Contains reset time information.
    This triggers HTTP 429 with Retry-After header.
    """
    def __init__(self, message: str, reset_time: Optional[datetime] = None, retry_after_seconds: Optional[int] = None):
        super().__init__(message)
        self.reset_time = reset_time
        self.retry_after_seconds = retry_after_seconds or self._calculate_retry_after()

    def _calculate_retry_after(self) -> int:
        """Calculate seconds until reset, default 3600 (1 hour) if unknown"""
        if self.reset_time:
            delta = self.reset_time - datetime.now()
            return max(60, int(delta.total_seconds()))  # Minimum 60 seconds
        return 3600  # Default 1 hour


from src.middleware.bridge_error import SDKDisconnectError  # noqa: F401 (re-exported)

import re as _re
_QUOTA_EXHAUSTION_RE = _re.compile(
    r'out of extra usage|ran out of context|resets\s+\d+:\d+\s*(?:am|pm)',
    _re.IGNORECASE
)


def detect_quota_exhaustion(content_text: str) -> bool:
    """Return True if response content signals account quota is exhausted.

    Catches Claude's inline quota messages that arrive as response text
    rather than API errors (e.g. weekly-cap reached, context exhausted).
    """
    return bool(_QUOTA_EXHAUSTION_RE.search(content_text))


class RateLimitTracker:
    """Tracks rate limit status per worker instance with soft routing.

    Two levels of rate limiting:
    - SOFT penalty: Worker got a transient rate_limit_event. Short cooldown (60s).
      Pre-check returns 503 so NGINX tries next worker, BUT if ALL workers have
      soft penalty, the pre-check lets requests through (no total block).
    - HARD limit: Worker got an explicit rate-limit text with reset time.
      Longer cooldown (parsed from message, capped to 10 min).
      Pre-check always blocks (real rate limit, Anthropic won't accept).

    In-progress tasks NEVER abort on rate_limit_event — only new request routing.
    """
    _instance = None
    # 60s instead of 600s — bounds the blast radius of a false-positive
    # detect_in_text match. A real Anthropic rate-limit reset is typically
    # minutes-to-hours, but 60s is enough to let the request that hit the
    # limit complete elsewhere; further requests will trigger the limit
    # again and re-arm the cooldown if it's real. False positives (Claude
    # generating a response that mentions one of the patterns in user
    # content) recover in 60s instead of 10 minutes.
    MAX_COOLDOWN_SECONDS = 60
    SOFT_PENALTY_SECONDS = 15   # Transient rate_limit_event — short penalty
    # Note: 15s (down from 60s) — SDK is also retrying internally, so this is
    # only protection for *new* request routing. Long lock created cascades
    # where Pool-Router shut down all 4 accounts on phantom heartbeat events.

    # Anthropic account-level exhaustion phrasings. Single source of truth —
    # the streaming and non-streaming response paths both read this list via
    # detect_in_text() so a new wording only has to be added here. Wider is
    # safer than narrower: a false positive parks the worker for 10min (capped
    # by MAX_COOLDOWN_SECONDS) while a false negative leaks the rate-limit
    # text to the client AND keeps the dead worker in the routing pool, so the
    # next request lands back on it instead of a healthy account.
    RATE_LIMIT_PATTERNS = (
        "hit your limit",
        "you've hit your limit",
        "rate limit",
        "usage limit",
        "quota exceeded",
        "too many requests",
        # Anthropic Pro/Max "extra usage" budget exhausted —
        # wording seen in the wild 2026-04 (Vienna reset).
        "out of extra usage",
        "out of usage",
        # Weekly / monthly plan caps
        "weekly limit",
        "monthly limit",
        # Alternate phrasings we've seen
        "reached your limit",
        "reached your usage",
    )

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._rate_limits: Dict[str, datetime] = {}  # worker_id -> reset_time
        self._hard_limits: set = set()  # worker_ids with hard limits
        self._initialized = True
        self._logger = get_logger(__name__)

    def _cap_reset_time(self, reset_time: datetime) -> datetime:
        """Cap reset time to MAX_COOLDOWN_SECONDS from now."""
        max_reset = datetime.now() + timedelta(seconds=self.MAX_COOLDOWN_SECONDS)
        if reset_time > max_reset:
            self._logger.info(f"⏱️ Capping cooldown to {self.MAX_COOLDOWN_SECONDS}s (original: {reset_time})")
            return max_reset
        return reset_time

    def parse_reset_time(self, message: str) -> datetime:
        """Parse reset time from Claude's rate limit messages.
        Always capped to MAX_COOLDOWN_SECONDS (10 min).
        """
        import re
        from datetime import datetime
        import pytz

        # Pattern: "resets Xpm" or "resets Xam"
        pattern = r'resets\s+(\d{1,2})(am|pm)\s*\(([^)]+)\)'
        match = re.search(pattern, message.lower())

        if match:
            hour = int(match.group(1))
            am_pm = match.group(2)
            timezone_str = match.group(3).strip()

            if am_pm == 'pm' and hour != 12:
                hour += 12
            elif am_pm == 'am' and hour == 12:
                hour = 0

            try:
                tz = pytz.timezone(timezone_str)
                now = datetime.now(tz)
                reset_time = now.replace(hour=hour, minute=0, second=0, microsecond=0)

                if reset_time <= now:
                    reset_time += timedelta(days=1)

                self._logger.info(f"📅 Parsed reset time: {reset_time} ({timezone_str})")
                return self._cap_reset_time(reset_time.replace(tzinfo=None))
            except Exception as e:
                self._logger.warning(f"Failed to parse timezone '{timezone_str}': {e}")

        # Fallback: 10 minutes
        return datetime.now() + timedelta(seconds=self.MAX_COOLDOWN_SECONDS)

    def mark_soft_penalty(self, worker_id: str, retry_after: Optional[int] = None) -> datetime:
        """Set a soft penalty on a worker (transient rate_limit_event).

        Soft penalties are short (60s default) and can be overridden by NGINX
        if all workers have penalties (safety valve).
        """
        seconds = min(retry_after or self.SOFT_PENALTY_SECONDS, self.MAX_COOLDOWN_SECONDS)
        reset_time = datetime.now() + timedelta(seconds=seconds)
        self._rate_limits[worker_id] = reset_time
        self._hard_limits.discard(worker_id)  # Soft, not hard
        self._logger.info(f"⏳ Worker {worker_id} soft penalty for {seconds}s")
        return reset_time

    def mark_rate_limited(self, worker_id: str, message: str) -> datetime:
        """Mark a worker as hard rate-limited. Cooldown parsed from message."""
        reset_time = self.parse_reset_time(message)
        self._rate_limits[worker_id] = reset_time
        self._hard_limits.add(worker_id)
        remaining = int((reset_time - datetime.now()).total_seconds())
        self._logger.warning(f"🚫 Worker {worker_id} HARD rate-limited for {remaining}s (until {reset_time})")
        return reset_time

    # Real Anthropic rate-limit messages are short (typically <500 chars,
    # almost always <1500). Long content with the phrase embedded somewhere
    # — e.g. a status table, a markdown report, documentation — is the user
    # talking ABOUT rate limits, not Anthropic delivering a rate-limit
    # message. Restricting detect_in_text to short responses eliminates the
    # false-positive class verified 2026-05-03 12:19 UTC where worker1 was
    # hard-limited because Claude generated a session-status table that
    # mentioned "out of extra usage" in a description.
    DETECT_MAX_TEXT_LEN = 1500

    def detect_in_text(self, text: str, worker_id: str) -> Optional[str]:
        """Scan a SHORT assistant text for an Anthropic rate-limit phrasing
        and, on hit, mark the worker HARD-limited.

        Single entry point for both response paths — streaming and the
        non-streaming SDK extractor. Without this the streaming loop saw the
        phrase and parked the worker, but a non-streaming sync request
        returned the same phrase as response content with the worker still
        marked available. The pool-router then kept routing every new
        request to the same exhausted worker.

        Length guard (DETECT_MAX_TEXT_LEN): a real Anthropic rate-limit
        message replaces the model's response with a short apology. If the
        assistant text is longer than that, it's the model GENERATING content
        that incidentally contains one of the patterns — false positive,
        skip detection. The previous behavior (match anywhere in any text
        length) hard-limited workers for 10 minutes whenever Claude wrote
        about rate limits in any context.

        Returns the matched pattern or None.
        """
        if not text:
            return None
        if len(text) > self.DETECT_MAX_TEXT_LEN:
            return None
        text_lower = text.lower()
        for pattern in self.RATE_LIMIT_PATTERNS:
            if pattern in text_lower:
                self.mark_rate_limited(worker_id, text)
                self._logger.warning(
                    f"🚫 Rate-limit phrase on {worker_id} (pattern={pattern!r}) "
                    f"— HARD limit set. Phrase: {text[:150]!r}"
                )
                return pattern
        return None

    def is_rate_limited(self, worker_id: str) -> bool:
        """Check if a specific worker is rate-limited (soft or hard)."""
        if worker_id not in self._rate_limits:
            return False
        if datetime.now() >= self._rate_limits[worker_id]:
            del self._rate_limits[worker_id]
            self._hard_limits.discard(worker_id)
            self._logger.info(f"✅ Worker {worker_id} cooldown expired, retrying")
            return False
        return True

    def is_hard_limited(self, worker_id: str) -> bool:
        """Check if worker has a HARD rate limit (real Anthropic rate limit)."""
        return self.is_rate_limited(worker_id) and worker_id in self._hard_limits

    def should_reject_new_request(self, worker_id: str) -> bool:
        """Decide if a new request should be rejected (503 for NGINX failover).

        Returns True if this worker should reject. NGINX routes to next worker.
        Safety valve: if ALL workers are soft-limited, allow through anyway.
        Hard limits always reject (real Anthropic rate limit).
        """
        if not self.is_rate_limited(worker_id):
            return False

        # Hard limit: always reject (Anthropic won't accept)
        if worker_id in self._hard_limits:
            return True

        # Soft limit: reject ONLY if other workers might be available.
        # We can't know from here, but NGINX will try all workers.
        # The safety valve: if this soft penalty is older than 30s,
        # there's a good chance the transient issue is resolved.
        remaining = self.get_retry_after(worker_id)
        if remaining is not None and remaining < 30:
            # Penalty almost expired — let it through
            return False

        return True

    def get_retry_after(self, worker_id: str) -> Optional[int]:
        """Get seconds until rate limit resets for a worker."""
        if worker_id in self._rate_limits:
            delta = self._rate_limits[worker_id] - datetime.now()
            seconds = int(delta.total_seconds())
            return max(0, min(seconds, self.MAX_COOLDOWN_SECONDS))
        return None

    def get_all_rate_limits(self) -> Dict[str, datetime]:
        """Get all current rate limits."""
        now = datetime.now()
        self._rate_limits = {k: v for k, v in self._rate_limits.items() if v > now}
        self._hard_limits = {w for w in self._hard_limits if w in self._rate_limits}
        return self._rate_limits.copy()


# Global rate limit tracker instance
rate_limit_tracker = RateLimitTracker()


def _handle_rate_limit_event(message, worker_id):
    """Process a rate_limit_event from the SDK.

    Returns None for heartbeat/warning/unknown status (caller continues).
    Raises RateLimitError for "hit" so the FastAPI exception flow can
    attempt a cross-worker retry. classify_exception in bridge_error.py
    maps RateLimitError to account_exhausted_error so the BridgeError
    handler runs _cross_worker_retry.
    """
    raw = getattr(message, "raw", None)
    rli = raw.get("rate_limit_info", {}) if isinstance(raw, dict) else {}
    rl_status = rli.get("status")
    rl_type = rli.get("rateLimitType") or "anthropic"

    reset_target = None
    if message.reset_at is not None:
        reset_target = float(message.reset_at)
    elif rli.get("resetsAt"):
        reset_target = float(rli["resetsAt"])

    is_heartbeat = rl_status in (None, "allowed")
    is_warning = (rl_status == "allowed_warning")
    is_hit = (
        not is_heartbeat
        and not is_warning
        and (rl_status is not None
             or message.retry_after is not None
             or (message.message and message.message.strip()))
    )

    if is_heartbeat:
        logger.debug(
            f"rate_limit_event heartbeat for {worker_id} "
            f"(status={rl_status}, type={rl_type}) — no penalty"
        )
        return None

    if is_warning:
        util = rli.get("utilization")
        surpassed = rli.get("surpassedThreshold")
        logger.info(
            f"⏳ Worker {worker_id} approaching {rl_type} limit "
            f"(status={rl_status}, util={util}, "
            f"surpassed={surpassed}) — soft penalty"
        )
        rate_limit_tracker.mark_soft_penalty(worker_id, 60)
        return None

    if is_hit:
        logger.info(
            f"🔒 Worker {worker_id} hit {rl_type} limit "
            f"(status={rl_status}, retry_after={message.retry_after}, "
            f"reset_at={reset_target}) — capacity lock + raise"
        )
        rate_limit_tracker.mark_soft_penalty(worker_id, message.retry_after or 60)
        try:
            from src.middleware.capacity_lock import get_capacity_lock as _get_cap_lock
            _cap = _get_cap_lock()
            if reset_target:
                _cap.lock_until(worker_id, reset_target, rl_type)
            else:
                logger.warning(
                    f"rate_limit hit on {worker_id} but no reset signal — "
                    f"soft penalty only, adaptive_limiter polling will catch >=95%"
                )
        except Exception as exc:
            logger.error(f"capacity_lock.lock_until failed: {exc}")
        try:
            from src.middleware.rolling_metrics import get_rolling_metrics
            get_rolling_metrics().record_rate_limit(worker_id)
        except Exception as exc:
            logger.debug(f"rolling_metrics.record_rate_limit failed: {exc}")

        reset_dt = datetime.fromtimestamp(reset_target) if reset_target else None
        raise RateLimitError(
            f"[{worker_id}] Anthropic {rl_type} limit hit",
            reset_time=reset_dt,
            retry_after_seconds=message.retry_after,
        )

    logger.info(
        f"rate_limit_event unknown status for {worker_id} "
        f"(status={rl_status}, raw={str(raw)[:200]}) — no action"
    )
    return None


def chunks_have_tool_use(chunks: list) -> bool:
    """True if any chunk contains a tool_use content block.

    Real SDK chunks are dataclass instances (AssistantMessage with
    content=[TextBlock|ToolUseBlock|...]) — NOT dicts. Tests in the
    bridge also pass dict shapes for legacy paths. Handle both.

    Tool-only responses (no text content but tool calls present) are
    valid responses; they shouldn't trigger incomplete detection.
    """
    for chunk in chunks:
        if chunk is None:
            continue
        # Find content list, supporting both dict and object shapes
        content_list = None
        if isinstance(chunk, dict):
            if isinstance(chunk.get("content"), list):
                content_list = chunk["content"]
            else:
                msg = chunk.get("message")
                if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                    content_list = msg["content"]
        else:
            attr_content = getattr(chunk, "content", None)
            if isinstance(attr_content, list):
                content_list = attr_content
        if content_list is None:
            continue
        for block in content_list:
            # dict-shape block
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return True
            # dataclass-shape block (ToolUseBlock from claude_code_sdk.types)
            if type(block).__name__ == "ToolUseBlock":
                return True
    return False


def is_incomplete_response(
    chunks_received: int,
    raw_content: str | None,
    has_tool_calls: bool,
) -> bool:
    """
    Detect SDK-early-termination: stream ended with chunks but no useful output.

    The Anthropic CLI SDK occasionally terminates streams mid-flight under
    capacity pressure (seven_day rate_limit warnings, upstream issues).
    Worker would otherwise return HTTP 200 with empty content — clients
    parse the empty body, retry to the same broken worker, infinite loop.

    Returns True when the response is broken and 503 should be surfaced.

    Decisions:
      - chunks_received == 0: separate failure mode (handled elsewhere)
      - has_tool_calls == True: tool-only response is valid (no text needed)
      - raw_content non-empty after strip(): legitimate text response
      - else: chunks arrived but nothing useful came out -> incomplete
    """
    if chunks_received == 0:
        return False
    if has_tool_calls:
        return False
    if raw_content and raw_content.strip():
        return False
    return True


TRUNCATION_MARKER_SUBTYPES = ("no_completion_marker", "timeout_incomplete")


def find_truncation_marker(chunks: list) -> Optional[dict]:
    """
    Find the explicit truncation marker run_completion yields when the SDK
    stream ended WITHOUT the CLI's result message.

    That happens e.g. when the Anthropic client inside the claude CLI hits
    its request timeout (API_TIMEOUT_MS, default 600s) mid-generation: the
    partial text streamed so far parses fine, so is_incomplete_response()
    does NOT catch it — without this check the caller gets HTTP 200 +
    finish_reason=stop with a silently truncated body (downstream JSON
    consumers then fail on "Unterminated string").

    Returns the marker dict (subtype in TRUNCATION_MARKER_SUBTYPES) or None.
    Deliberately narrow: genuine SDK error results (error_during_execution,
    error_max_turns, …) keep their existing handling paths.
    """
    for chunk in chunks:
        if (
            isinstance(chunk, dict)
            and chunk.get("type") == "result"
            and chunk.get("is_error")
            and chunk.get("subtype") in TRUNCATION_MARKER_SUBTYPES
        ):
            return chunk
    return None


def extract_result_usage(chunk) -> Optional[dict]:
    """
    Real API usage from a CLI result chunk, or None if this chunk carries none.

    Two result-chunk shapes reach the caller:
      - SDK mode: ResultMessage dataclass converted via its public attributes —
        that dict has 'subtype' + 'usage' but NO 'type' key (ResultMessage has
        no .type attribute), so `chunk.get("type") == "result"` never matches
        it. This gap is why usage was char-estimated for years.
      - CLI JSON mode / synthetic markers: dict WITH type='result'.

    The returned dict uses the ledger's field names; input_tokens is the
    UNCACHED input (Anthropic semantics), cache tokens are separate.
    """
    if not isinstance(chunk, dict):
        return None
    # Result chunks only: either explicitly typed, or the converted
    # ResultMessage (identified by its 'subtype' attribute). AssistantMessage
    # conversions have neither.
    if chunk.get("type") != "result" and "subtype" not in chunk:
        return None
    if chunk.get("type") not in (None, "result"):
        return None
    usage = chunk.get("usage")
    if not isinstance(usage, dict):
        return None

    def _tok(key: str) -> int:
        value = usage.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    return {
        "input_tokens": _tok("input_tokens"),
        "output_tokens": _tok("output_tokens"),
        "cache_creation_tokens": _tok("cache_creation_input_tokens"),
        "cache_read_tokens": _tok("cache_read_input_tokens"),
    }


class ClaudeCodeCLI:
    def __init__(self, timeout: int = 1200000, cwd: Optional[str] = None):
        self.timeout = timeout / 1000  # Convert ms to seconds
        self.cwd = Path(cwd) if cwd else Path.cwd()

        # Import auth manager
        from src.auth import auth_manager, validate_claude_code_auth

        # Validate authentication
        is_valid, auth_info = validate_claude_code_auth()
        if not is_valid:
            logger.warning(f"Claude Code authentication issues detected: {auth_info['errors']}")
        else:
            logger.info(f"Claude Code authentication method: {auth_info.get('method', 'unknown')}")

        # Store auth environment variables for SDK
        self.claude_env_vars = auth_manager.get_claude_code_env_vars()

        # Cache configuration
        self.cache_dir = Path("/tmp")
        self.max_cache_size_mb = 10  # 10MB limit per request

        # File discovery service
        wrapper_root = Path.cwd()  # Wrapper root directory
        self.file_discovery = FileDiscoveryService(wrapper_root)
        logger.info(f"✅ File discovery service initialized")

        # Cleanup old cache files on startup
        self._cleanup_old_cache_files()

    def _cleanup_old_cache_files(self):
        """Remove cache files older than 1 hour"""
        try:
            cutoff_time = time.time() - 3600  # 1 hour ago
            cleaned = 0

            for cache_file in self.cache_dir.glob("sdk_response_*.txt"):
                try:
                    if cache_file.stat().st_mtime < cutoff_time:
                        cache_file.unlink()
                        cleaned += 1
                except Exception as e:
                    logger.warning(f"Failed to cleanup {cache_file.name}: {e}")

            if cleaned > 0:
                logger.info(f"🧹 Cleaned up {cleaned} old SDK cache files")
        except Exception as e:
            logger.warning(f"Cache cleanup failed: {e}")

    async def verify_cli(self) -> bool:
        """
        Verify Claude Code SDK is working and authenticated.

        LAW 1: Never Silent Failures
        - Raises RuntimeError on critical failures (timeout, no messages)
        - Returns False only for recoverable errors (SDK not installed)
        - All failures are loudly logged with context

        Returns:
            bool: True if SDK verified, False if SDK not installed (non-critical)

        Raises:
            RuntimeError: Critical failure (timeout, empty response, SDK hanging)
        """
        try:
            # Test SDK with a simple query and timeout
            logger.info("Testing Claude Code SDK...")

            messages = []
            start_time = asyncio.get_event_loop().time()

            # Add 60 second timeout for SDK verification
            try:
                async with asyncio.timeout(60):
                    _verify_opts = ClaudeCodeOptions(max_turns=1, cwd=self.cwd)
                    _verify_opts.mcp_servers = {}
                    async for message in query(prompt="Hello", options=_verify_opts):
                        messages.append(message)
                        # Break early on first response to speed up verification
                        # Handle both dict and object types
                        msg_type = getattr(message, 'type', None) if hasattr(message, 'type') else message.get("type") if isinstance(message, dict) else None
                        if msg_type == "assistant":
                            break

            except asyncio.TimeoutError:
                # LAW 1: Timeout is CRITICAL failure - raise, don't return False
                elapsed = asyncio.get_event_loop().time() - start_time
                error_msg = (
                    f"CRITICAL: Claude Code SDK verification timed out after {elapsed:.1f}s!\n"
                    f"  Timeout Type: SDK Verification\n"
                    f"  Timeout Duration: 60s\n"
                    f"  Messages Received: {len(messages)}\n"
                    f"  Impact: SDK is hanging - cannot start server safely\n"
                    f"\n"
                    f"Possible causes:\n"
                    f"  1. Claude Code CLI is not installed or not in PATH\n"
                    f"  2. Authentication failed (run: claude login)\n"
                    f"  3. MCP servers are failing to load\n"
                    f"  4. System resource exhaustion (CPU/Memory)\n"
                    f"\n"
                    f"Debug steps:\n"
                    f"  1. Test CLI: claude --print 'Hello'\n"
                    f"  2. Check auth: claude --version\n"
                    f"  3. Check MCP: grep 'mcp.*failed' logs/app.log\n"
                    f"  4. Disable MCPs: DISABLE_MCPS=true ./start-wrappers.sh"
                )
                logger.error(error_msg)
                raise RuntimeError(error_msg)

            # LAW 1: Empty messages is CRITICAL failure - raise, don't return False
            if not messages:
                error_msg = (
                    f"CRITICAL: Claude Code SDK verification returned ZERO messages!\n"
                    f"  This should never happen - SDK is broken or misconfigured.\n"
                    f"  Expected: At least 1 message (type='init' or type='assistant')\n"
                    f"  Got: Empty list\n"
                    f"\n"
                    f"This indicates a fundamental SDK failure. Cannot proceed safely."
                )
                logger.error(error_msg)
                raise RuntimeError(error_msg)

            # Success
            elapsed = asyncio.get_event_loop().time() - start_time
            logger.info(f"✅ Claude Code SDK verified successfully (duration: {elapsed:.1f}s, messages: {len(messages)})")
            return True

        except RuntimeError:
            # Re-raise RuntimeError (critical failures)
            raise

        except ImportError as e:
            # SDK not installed - this is recoverable (user can install it)
            # Return False but log loudly
            logger.error(f"❌ Claude Code SDK not installed: {e}")
            logger.warning("SDK Installation required:")
            logger.warning("  pip install claude-code-sdk")
            logger.warning("  OR: npm install -g @anthropic-ai/claude-code")
            return False

        except Exception as e:
            # Unknown error - treat as CRITICAL (LAW 1)
            error_str = str(e).lower()

            # Specific error detection for actionable guidance
            if "credit balance" in error_str or "balance is too low" in error_str:
                error_msg = (
                    "=" * 70 + "\n"
                    "🚨 CRITICAL: OAuth Token Expired or Invalid!\n"
                    "=" * 70 + "\n"
                    f"  Error: {e}\n"
                    "\n"
                    "The OAuth token has expired or is linked to an account without credits.\n"
                    "\n"
                    "TO FIX THIS:\n"
                    "  1. Generate a new OAuth token (valid for 1 year):\n"
                    "     $ claude setup-token\n"
                    "\n"
                    "  2. Save the token to secrets file:\n"
                    "     $ echo 'YOUR_NEW_TOKEN' > /path/to/secrets/claude_token.txt\n"
                    "\n"
                    "  3. Restart the container:\n"
                    "     $ docker compose restart\n"
                    "\n"
                    "⚠️  IMPORTANT: This wrapper NEVER falls back to ANTHROPIC_API_KEY!\n"
                    "    If OAuth fails, the request fails. No silent API charges.\n"
                    "=" * 70
                )
            elif "authentication" in error_str or "unauthorized" in error_str:
                error_msg = (
                    "=" * 70 + "\n"
                    "🚨 CRITICAL: Claude Code Authentication Failed!\n"
                    "=" * 70 + "\n"
                    f"  Error: {e}\n"
                    "\n"
                    "Claude Code CLI is not authenticated.\n"
                    "\n"
                    "TO FIX THIS:\n"
                    "  1. Authenticate Claude Code:\n"
                    "     $ claude login\n"
                    "\n"
                    "  2. Or generate a long-lived token:\n"
                    "     $ claude setup-token\n"
                    "\n"
                    "  3. Save token and restart container\n"
                    "=" * 70
                )
            else:
                error_msg = (
                    "=" * 70 + "\n"
                    "🚨 CRITICAL: Claude Code SDK Verification Failed!\n"
                    "=" * 70 + "\n"
                    f"  Error Type: {type(e).__name__}\n"
                    f"  Error Message: {e}\n"
                    "\n"
                    "This is an unexpected failure.\n"
                    "\n"
                    "TROUBLESHOOTING:\n"
                    "  1. Check OAuth token is valid:\n"
                    "     $ claude setup-token\n"
                    "\n"
                    "  2. Verify CLI works:\n"
                    "     $ claude -p 'Hello' --max-turns 1\n"
                    "\n"
                    "  3. Check container logs for details\n"
                    "\n"
                    "⚠️  This wrapper NEVER falls back to ANTHROPIC_API_KEY!\n"
                    "    All requests use OAuth only - no silent API charges.\n"
                    "=" * 70
                )

            logger.error(error_msg)
            # Re-raise as RuntimeError with context
            raise RuntimeError(error_msg) from e

    async def run_native_cli(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_turns: int = 50,
        session_dir: Optional[Path] = None
    ) -> Dict[str, Any]:
        """
        Run Claude Code CLI directly for slash commands that don't execute via SDK.

        This is a fallback for /sc:* commands that are recognized but not executed
        by the SDK (they're treated as context, not executable commands).

        Returns session metadata including session_id and directory path.
        """
        logger.info("🔧 Using native CLI fallback for slash command execution")

        if not session_dir:
            raise ValueError("session_dir required for native CLI execution")

        # Build CLI command
        cli_args = [
            "claude",
            "--print",  # Non-interactive mode
            "--max-turns", str(max_turns),
            "--permission-mode", "bypassPermissions"  # Required for Docker
        ]

        if model:
            cli_args.extend(["--model", model])

        # Prompt is passed via stdin to avoid OS ARG_MAX limits.
        # claude --print reads the prompt from stdin when no positional arg is given.
        logger.info(f"📝 Executing native CLI: {' '.join(cli_args[:5])}... (prompt via stdin)")

        try:
            # Run claude CLI directly — prompt via stdin, NOT as argv arg
            result = subprocess.run(
                cli_args,
                input=prompt,            # prompt → stdin, avoids ARG_MAX
                cwd=str(session_dir),    # Execute in session directory
                capture_output=True,
                text=True,
                timeout=600  # 10 minute timeout for research
            )

            if result.returncode != 0:
                logger.error(f"❌ Native CLI failed with exit code {result.returncode}")
                logger.error(f"   stderr: {result.stderr[:500]}")
                raise RuntimeError(f"Native CLI execution failed: {result.stderr[:200]}")

            logger.info(f"✅ Native CLI completed successfully")
            logger.info(f"   stdout: {len(result.stdout)} chars")

            return {
                "success": True,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "session_dir": str(session_dir)
            }

        except subprocess.TimeoutExpired:
            logger.error("⏱️ Native CLI timeout after 10 minutes")
            raise RuntimeError("Native CLI execution timeout")
        except Exception as e:
            logger.error(f"❌ Native CLI execution error: {e}")
            raise

    async def run_completion(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        stream: bool = True,
        max_turns: int = 10,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        enable_file_discovery: bool = False,
        backend_env_vars: Optional[Dict[str, str]] = None
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Run Claude Code using the Python SDK and yield response chunks.

        Args:
            prompt: The user prompt to send to Claude
            system_prompt: Optional system prompt
            model: Model ID to use
            stream: Whether to stream responses
            max_turns: Maximum conversation turns
            allowed_tools: List of tools to allow
            disallowed_tools: List of tools to disallow
            session_id: Optional session ID for continuity
            continue_session: Whether to continue existing session
            enable_file_discovery: Enable file discovery for research
            backend_env_vars: Additional env vars for backend routing (e.g., Bedrock).
                              These are merged with auth env vars and override them if keys conflict.
                              Cleaned up after request completes.
        """

        # Register CLI session for tracking and cancellation
        from src.cli_session_manager import cli_session_manager
        cli_session = cli_session_manager.create_session(
            prompt=prompt,
            model=model
        )
        cli_session_id = cli_session.cli_session_id
        logger.info(f"📝 Created CLI session: {cli_session_id}")

        # SLASH COMMAND DETECTION: Transform /sc:research into executable protocol
        # SuperClaude commands are not executed by SDK - they're just expanded as context
        # Transform them into direct instructions that will be executed
        if prompt.strip().startswith("/sc:research") or prompt.strip().startswith("/research"):
            logger.info(f"🔍 Detected research command: {prompt[:60]}...")
            logger.info(f"   Transforming into direct execution protocol")

            # Extract the research query from the slash command
            # Format: /sc:research [flags] "query" or just /sc:research "query"
            import re

            # Try to extract query after the command
            query_match = re.search(r'/(?:sc:)?research\s+(?:--depth\s+\w+\s+)?"?(.+?)"?\s*$', prompt, re.DOTALL)
            research_query = query_match.group(1) if query_match else prompt.replace("/sc:research", "").replace("/research", "").strip()

            logger.info(f"   Extracted research query: {research_query[:100]}...")

            # Replace prompt with direct execution instructions
            # CRITICAL: Keep concise to avoid context overflow
            prompt = f"""Research this query and write output IMMEDIATELY:

QUERY: {research_query}

PROTOCOL (execute in order):
1. Use WebSearch and WebFetch for 2-3 TARGETED searches only
2. Extract ONLY key findings (keep summaries under 150 words each)
3. Write report to claudedocs/research_output.md IMMEDIATELY after searches
4. DO NOT conduct additional searches after writing file

OUTPUT STRUCTURE:
# Research Report

## Summary
[2-3 sentences maximum]

## Key Findings
- [Finding 1 with source]
- [Finding 2 with source]
- [Finding 3 with source]

## Analysis
[Brief analysis, max 200 words]

## Sources
[List URLs]

CRITICAL: Write file EARLY to avoid context overflow. Use Write tool for claudedocs/research_output.md.
"""

            # Set reasonable max_turns - increased to 25 for complex research queries
            if max_turns < 20:
                max_turns = 20
                logger.info(f"   Set max_turns to {max_turns} for research")
            elif max_turns > 25:
                max_turns = 25  # Cap at 25 to prevent overflow
                logger.info(f"   Capped max_turns to {max_turns} to prevent context overflow")

            # Enable file discovery for research output
            enable_file_discovery = True

        try:
            # CRITICAL: Verify ANTHROPIC_API_KEY is NOT in environment
            # If present, the SDK silently falls back to paid API when OAuth fails.
            # This must NEVER happen — OAuth failure = error, not silent fallback.
            if os.getenv("ANTHROPIC_API_KEY"):
                logger.error("=" * 70)
                logger.error("FATAL: ANTHROPIC_API_KEY found in environment!")
                logger.error("This causes silent fallback to paid API when OAuth fails.")
                logger.error("Remove ANTHROPIC_API_KEY from docker-compose.yml.")
                logger.error("Vision should use ANTHROPIC_VISION_API_KEY instead.")
                logger.error("=" * 70)
                raise RuntimeError(
                    "ANTHROPIC_API_KEY must not be in environment. "
                    "It causes silent fallback to paid API. "
                    "Use ANTHROPIC_VISION_API_KEY for vision-only access."
                )

            # Set authentication environment variables (if any)
            original_env = {}
            if self.claude_env_vars:  # Only set env vars if we have any
                for key, value in self.claude_env_vars.items():
                    original_env[key] = os.environ.get(key)
                    os.environ[key] = value

            # Apply per-request backend env vars (e.g., for Bedrock routing)
            # These OVERRIDE auth env vars if both specify the same key
            if backend_env_vars:
                for key, value in backend_env_vars.items():
                    if key not in original_env:  # Don't double-save original values
                        original_env[key] = os.environ.get(key)
                    os.environ[key] = value
                    # Log with masked value for security
                    masked_value = value[:8] + "..." if len(value) > 8 else "***"
                    logger.debug(f"🔀 Backend env var set: {key}={masked_value}")
                logger.info(f"🔀 Backend routing active: {len(backend_env_vars)} env vars set")

            # ALWAYS disable Coach MCP in wrapper to prevent infinite spawn loop
            # Coach MCP spawns Claude → /sc:research spawns Coach → LOOP!
            original_env['DISABLE_COACH_MCP'] = os.environ.get('DISABLE_COACH_MCP')
            os.environ['DISABLE_COACH_MCP'] = 'true'
            logger.info("🚫 Coach MCP disabled for wrapper session (prevents spawn loop)")
            
            try:
                # Session-specific directory for ALL sessions
                # Creates: instances/{instance}/YYYY-MM-DD-HHMM_{cli_session_id}/
                research_cwd = None
                research_dir = None

                # Create timestamped session directory
                timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
                session_dir_name = f"{timestamp}_{cli_session.cli_session_id}"

                # Validate instance directory exists
                if not self.cwd.exists():
                    error_msg = f"Instance directory does not exist: {self.cwd}"
                    logger.error(
                        f"❌ {error_msg}",
                        extra={"cwd": str(self.cwd)}
                    )
                    raise RuntimeError(error_msg)

                # Create session directory
                research_dir = self.cwd / session_dir_name

                try:
                    research_dir.mkdir(parents=True, exist_ok=False)
                    logger.info(
                        "✅ Session directory created",
                        extra={
                            "research_dir": str(research_dir),
                            "cli_session_id": cli_session.cli_session_id
                        }
                    )
                except FileExistsError as e:
                    error_msg = f"Session directory already exists: {research_dir}"
                    logger.error(
                        f"❌ {error_msg}",
                        exc_info=True,
                        extra={"research_dir": str(research_dir)}
                    )
                    raise RuntimeError(error_msg) from e
                except OSError as e:
                    error_msg = f"Failed to create session directory: {research_dir}"
                    logger.error(
                        f"❌ {error_msg}",
                        exc_info=True,
                        extra={"research_dir": str(research_dir)}
                    )
                    raise RuntimeError(error_msg) from e

                # Create claudedocs subdirectory (for /sc:research)
                claudedocs_dir = research_dir / "claudedocs"
                try:
                    claudedocs_dir.mkdir(parents=True, exist_ok=True)
                    logger.info(
                        "✅ Claudedocs directory created",
                        extra={"claudedocs_dir": str(claudedocs_dir)}
                    )
                except OSError as e:
                    error_msg = f"Failed to create claudedocs directory: {claudedocs_dir}"
                    logger.error(
                        f"❌ {error_msg}",
                        exc_info=True,
                        extra={"claudedocs_dir": str(claudedocs_dir)}
                    )
                    raise RuntimeError(error_msg) from e

                # Create comprehensive metadata.json with ALL SDK options
                metadata = {
                    "cli_session_id": cli_session.cli_session_id,
                    "created_at": datetime.now().isoformat(),
                    "prompt_preview": prompt[:200],  # First 200 chars for preview
                    "model": model,
                    "instance": os.getenv("INSTANCE_NAME", "unknown"),
                    "status": "running",
                    # SDK Options
                    "sdk_options": {
                        "max_turns": max_turns,
                        "cwd": str(research_dir),
                        "permission_mode": os.getenv("CLAUDE_PERMISSION_MODE"),
                        "system_prompt": system_prompt if system_prompt else None,
                        "allowed_tools": allowed_tools if allowed_tools else None,
                        "disallowed_tools": disallowed_tools if disallowed_tools else None,
                        "continue_session": continue_session,
                        "resume_session_id": session_id if session_id else None,
                        "disable_coach_mcp": os.getenv("DISABLE_COACH_MCP", "false").lower() in ("true", "1", "yes"),
                        "disable_all_mcps": os.getenv("DISABLE_MCPS", "false").lower() in ("true", "1", "yes"),
                    }
                }

                metadata_file = research_dir / "metadata.json"
                try:
                    import json
                    with open(metadata_file, 'w', encoding='utf-8') as f:
                        json.dump(metadata, f, indent=2)

                    logger.info(
                        "✅ Session metadata created",
                        extra={"metadata_file": str(metadata_file)}
                    )
                except OSError as e:
                    logger.warning(
                        "⚠️  Failed to create metadata file (non-critical)",
                        exc_info=True,
                        extra={"metadata_file": str(metadata_file)}
                    )
                    # Don't raise - metadata is nice-to-have, not critical

                # Set research_cwd for SDK
                research_cwd = str(research_dir)

                logger.info(
                    "📁 Session directory ready",
                    extra={
                        "cli_session_id": cli_session.cli_session_id,
                        "research_dir": str(research_dir),
                        "claudedocs": str(claudedocs_dir)
                    }
                )

                # Inject output path for file discovery (research or opt-in)
                if enable_file_discovery:
                    output_file = claudedocs_dir / "output.md"
                    prompt = inject_output_path_for_file_discovery(
                        prompt=prompt,
                        output_file=output_file,
                        cli_session_id=cli_session.cli_session_id
                    )

                # Initialize all variables before try block to avoid UnboundLocalError in exception handlers
                cache_file = None
                progress_tracking_enabled = False
                chunks_received = 0
                chunks_buffer = []
                _sys_prompt_tempfile_path: Optional[str] = None  # cleanup target for large system_prompt

                # Build SDK options
                options = ClaudeCodeOptions(
                    max_turns=max_turns,
                    cwd=research_cwd
                )
                options.mcp_servers = {}

                # Set permission mode if specified via environment variable
                permission_mode = os.getenv("CLAUDE_PERMISSION_MODE")
                if permission_mode:
                    options.permission_mode = permission_mode
                    logger.info(f"🔓 Permission mode set to: {permission_mode}")

                # Set model if specified
                if model:
                    options.model = model

                # === SYSTEM PROMPT: argv-safe handling ===
                # The SDK passes options.system_prompt as --system-prompt <value> to the
                # claude CLI subprocess.  Values > ARG_MAX (~128 KB) cause E2BIG: the CLI
                # never starts and the bridge reports a 503/capacity_busy instead of the
                # real error (unified-tester sends ~184 KB system prompts).
                # Fix: write large values to a temp file; the anyio.open_process patch
                # (see module-level _apply_open_process_patch) injects
                # --system-prompt-file <path> into the subprocess args.
                if system_prompt:
                    if len(system_prompt.encode("utf-8")) > LARGE_ARG_THRESHOLD_BYTES:
                        if not _open_process_patch_applied:
                            raise RuntimeError(
                                f"Large system_prompt ({len(system_prompt):,} chars) cannot be "
                                f"passed as argv (exceeds ARG_MAX) and the anyio.open_process patch "
                                f"was NOT applied at startup. Cannot proceed safely. "
                                f"Check bridge startup logs for the patch failure reason."
                            )
                        _tf = tempfile.NamedTemporaryFile(
                            mode="w", suffix=".txt",
                            prefix="bridge_sysprompt_",
                            delete=False, encoding="utf-8"
                        )
                        _tf.write(system_prompt)
                        _tf.close()
                        _sys_prompt_tempfile_path = _tf.name
                        _current_sys_prompt_tempfile.set(_tf.name)
                        # Do NOT set options.system_prompt — the subprocess patch will
                        # inject --system-prompt-file into the claude CLI args instead.
                        logger.info(
                            f"📁 Large system_prompt ({len(system_prompt):,} chars) written to "
                            f"temp file for argv-safe delivery: {_tf.name}"
                        )
                    else:
                        options.system_prompt = system_prompt
                # === END SYSTEM PROMPT HANDLING ===

                # Set tool restrictions
                if allowed_tools:
                    options.allowed_tools = allowed_tools
                if disallowed_tools:
                    options.disallowed_tools = disallowed_tools

                # DISABLE specific MCPs to prevent infinite spawning loops
                # Coach frontend needs Coach MCP, but /sc:research must not spawn Coach
                # Solution: Set DISABLE_COACH_MCP=true only when calling /sc:research
                disable_coach = os.getenv("DISABLE_COACH_MCP", "false").lower() in ("true", "1", "yes")
                disable_all_mcps = os.getenv("DISABLE_MCPS", "false").lower() in ("true", "1", "yes")

                if disable_all_mcps:
                    # Disable ALL MCPs
                    mcp_pattern = "mcp__*"
                    if disallowed_tools:
                        if mcp_pattern not in disallowed_tools:
                            options.disallowed_tools.append(mcp_pattern)
                    else:
                        options.disallowed_tools = [mcp_pattern]
                    logger.info("🚫 ALL MCPs disabled for this session (DISABLE_MCPS=true)")
                elif disable_coach:
                    # Disable ONLY Coach MCP (allows Context7, Sequential, etc.)
                    coach_pattern = "mcp__coach__*"
                    if disallowed_tools:
                        if coach_pattern not in disallowed_tools:
                            options.disallowed_tools.append(coach_pattern)
                    else:
                        options.disallowed_tools = [coach_pattern]
                    logger.info("🚫 Coach MCP disabled for this session (DISABLE_COACH_MCP=true)")
                    
                # Handle session continuity
                if continue_session:
                    options.continue_conversation = True
                elif session_id:
                    options.resume = session_id

                # === Save exact prompt BEFORE sending to Claude SDK ===
                prompt_file = research_dir / "prompt.txt"
                try:
                    with open(prompt_file, 'w', encoding='utf-8') as f:
                        # Save system prompt if exists
                        if system_prompt:
                            f.write("=== SYSTEM PROMPT ===\n")
                            f.write(system_prompt)
                            f.write("\n\n")

                        # Save user prompt (exact as sent to Claude)
                        f.write("=== USER PROMPT ===\n" if system_prompt else "")
                        f.write(prompt)

                    logger.info(
                        "✅ Prompt saved to file",
                        extra={
                            "prompt_file": str(prompt_file),
                            "has_system_prompt": system_prompt is not None,
                            "prompt_length": len(prompt)
                        }
                    )
                except Exception as e:
                    logger.warning(
                        "⚠️ Failed to save prompt to file",
                        exc_info=True,
                        extra={"prompt_file": str(prompt_file)}
                    )
                    # Continue - prompt saving is non-critical
                # === END: Save exact prompt ===

                # Run the query with timeout to prevent zombie processes
                # Timeout is set to self.timeout (converted from ms to seconds in __init__)
                logger.info(f"🕐 Starting SDK query with {self.timeout}s timeout")

                # Setup file-based caching for crash recovery
                # Use unique filename to prevent race conditions
                try:
                    cache_file = self.cache_dir / f"sdk_response_{cli_session_id}_{os.getpid()}_{uuid.uuid4().hex[:8]}.txt"
                except Exception as cache_init_err:
                    logger.warning(f"Failed to initialize cache file: {cache_init_err}")
                cache_enabled = True
                cache_size_bytes = 0
                max_cache_bytes = self.max_cache_size_mb * 1024 * 1024
                response_complete = False

                first_chunk_logged = False

                # Progress tracking setup - use research_dir for all tracking
                progress_file = research_dir / "progress.jsonl"
                messages_file = research_dir / "messages.jsonl"
                final_file = research_dir / "final_response.json"
                progress_tracking_enabled = True

                # Tracking variables for final response
                accumulated_text_parts = []
                tools_used = set()
                start_time = datetime.now()

                # === LARGE USER PROMPT HANDLING ===
                # The SDK passes the user prompt as a positional argv arg.
                # Values > ARG_MAX (~128 KB) cause E2BIG at exec() time.
                # Solution: pass as an AsyncIterable so the SDK uses stdin (stream-json).
                # LARGE_ARG_THRESHOLD_BYTES is defined at module level.

                async def _prompt_to_stream(prompt_text: str):
                    """Convert string prompt to AsyncIterable for streaming mode (stdin)."""
                    yield {
                        "type": "user",
                        "message": {"role": "user", "content": prompt_text}
                    }

                if len(prompt.encode("utf-8")) > LARGE_ARG_THRESHOLD_BYTES:
                    logger.info(
                        f"🔄 Large user prompt ({len(prompt):,} chars) → streaming mode (stdin) "
                        f"to bypass ARG_MAX"
                    )
                    prompt_source = _prompt_to_stream(prompt)
                else:
                    prompt_source = prompt
                # === END LARGE USER PROMPT HANDLING ===

                try:
                    async with asyncio.timeout(self.timeout):
                        async for message in query(prompt=prompt_source, options=options):
                            # Skip None messages (from monkey-patched parse_message for unknown types)
                            if message is None:
                                continue

                            # =============================================================
                            # RATE LIMIT EVENT: CLI is handling retry internally — DON'T abort.
                            # rate_limit_event means the CLI detected a transient rate limit
                            # and is WAITING to retry. We skip this message, let the stream
                            # continue, and set a soft penalty so NEW requests get routed
                            # to other workers (but this task completes normally).
                            # =============================================================
                            if isinstance(message, RateLimitEvent):
                                worker_id = os.environ.get("INSTANCE_NAME", "unknown")
                                _handle_rate_limit_event(message, worker_id)
                                continue

                            # Completion-marker detection MUST happen on the
                            # dataclass, BEFORE the attribute->dict conversion
                            # below: ResultMessage has no `.type` attribute, so
                            # the converted dict lacks 'type' and the dict-based
                            # check further down never fires. That latent bug
                            # made "NO completion marker detected" fire on EVERY
                            # SDK request — harmless log noise until the 503
                            # truncation guard (find_truncation_marker) trusted
                            # it and rejected healthy responses.
                            if type(message).__name__ == 'ResultMessage':
                                _result_subtype = getattr(message, 'subtype', None)
                                if _result_subtype in ('complete', 'success'):
                                    response_complete = True
                                    logger.debug("✅ Response completion marker detected (ResultMessage)")
                                elif _result_subtype == 'error_max_turns':
                                    response_complete = True
                                    logger.warning(
                                        "⚠️ Result hit max_turns limit - output may be incomplete",
                                        extra={"cli_session_id": cli_session_id},
                                    )

                            chunks_received += 1

                            # Collect message for file discovery
                            chunks_buffer.append(message)

                            # Log first chunk for debugging
                            if not first_chunk_logged:
                                logger.info(f"📨 First chunk received from SDK (type: {type(message).__name__})")
                                first_chunk_logged = True

                            # Check for cancellation request
                            if cli_session.cancellation_token and cli_session.cancellation_token.is_set():
                                logger.info(f"🚫 CLI session {cli_session_id} cancelled by user request")
                                raise asyncio.CancelledError("Session cancelled by user")

                            # Debug logging
                            logger.debug(f"Raw SDK message type: {type(message)}")
                            logger.debug(f"Raw SDK message: {message}")

                            # Progress tracking: Write all messages for debug
                            if progress_tracking_enabled:
                                message_data = {
                                    'index': chunks_received,
                                    'type': str(type(message).__name__),
                                    'timestamp': datetime.now().isoformat(),
                                    'data': str(message)[:500]  # Truncate for file size
                                }
                                write_progress_safe(messages_file, message_data, cli_session_id)

                                # Extract and write progress
                                progress = extract_progress(message)
                                if progress:
                                    progress['timestamp'] = datetime.now().isoformat()
                                    write_progress_safe(progress_file, progress, cli_session_id)

                                    # Track tool usage
                                    if progress['type'] == 'tool_use':
                                        tools_used.add(progress['data']['tool'])

                            # Convert message object to dict if needed
                            if hasattr(message, '__dict__') and not isinstance(message, dict):
                                # Convert object to dict for consistent handling
                                message_dict = {}

                                # Get all attributes from the object
                                for attr_name in dir(message):
                                    if not attr_name.startswith('_'):  # Skip private attributes
                                        try:
                                            attr_value = getattr(message, attr_name)
                                            if not callable(attr_value):  # Skip methods
                                                message_dict[attr_name] = attr_value
                                        except (AttributeError, TypeError) as e:
                                            # Expected for properties that raise or computed attributes
                                            logger.debug(f"Could not get attribute '{attr_name}': {e}")

                                logger.debug(f"Converted message dict: {message_dict}")
                                message = message_dict

                            # Cache chunk to file for crash recovery (with error handling)
                            if cache_enabled:
                                try:
                                    chunk_json = json.dumps(message, default=str) + "\n"
                                    chunk_size = len(chunk_json.encode('utf-8'))

                                    # Check size limit
                                    if cache_size_bytes + chunk_size > max_cache_bytes:
                                        logger.warning(f"📦 Cache size limit ({self.max_cache_size_mb}MB) reached - disabling cache")
                                        cache_enabled = False
                                    else:
                                        with open(cache_file, 'a', encoding='utf-8') as f:
                                            f.write(chunk_json)
                                        cache_size_bytes += chunk_size
                                except (IOError, OSError) as e:
                                    logger.warning(f"💾 Cache write failed: {e} - disabling cache for this request")
                                    cache_enabled = False
                                except Exception as e:
                                    logger.warning(f"💾 Unexpected cache error: {e} - disabling cache")
                                    cache_enabled = False

                            # Check for completion marker (ONLY in SDK result messages, not content)
                            if message.get('type') == 'result':
                                if message.get('subtype') in ['complete', 'success']:
                                    response_complete = True
                                    logger.debug("✅ Response completion marker detected")
                                elif message.get('subtype') == 'error_max_turns':
                                    response_complete = True
                                    logger.warning(
                                        f"⚠️ Research hit max_turns limit - output may be incomplete",
                                        extra={
                                            "cli_session_id": cli_session_id,
                                            "num_turns": message.get('num_turns'),
                                            "total_cost_usd": message.get('total_cost_usd')
                                        }
                                    )

                            # Progress tracking: Accumulate text for final response
                            if progress_tracking_enabled:
                                # Extract text content from message
                                try:
                                    # Check for AssistantMessage type
                                    if type(message).__name__ == 'AssistantMessage':
                                        if hasattr(message, 'content') and message.content:
                                            for block in message.content:
                                                # TextBlock with .text attribute
                                                if hasattr(block, 'text') and block.text:
                                                    accumulated_text_parts.append(block.text)
                                                    logger.debug(f"📝 Extracted {len(block.text)} chars from TextBlock")
                                    # Fallback: Object with .content attribute
                                    elif hasattr(message, 'content'):
                                        for block in message.content:
                                            if hasattr(block, 'text'):
                                                accumulated_text_parts.append(block.text)
                                    # Fallback: Dict format
                                    elif isinstance(message, dict) and 'content' in message:
                                        content = message['content']
                                        if isinstance(content, list):
                                            for block in content:
                                                if isinstance(block, dict) and 'text' in block:
                                                    accumulated_text_parts.append(block['text'])
                                except (AttributeError, TypeError, KeyError) as e:
                                    logger.debug(f"🔍 Could not extract text from message: {e}")

                            # =================================================================
                            # RATE LIMIT TEXT DETECTION (track only, DON'T abort)
                            #
                            # The CLI handles rate limits internally — it waits and retries.
                            # We track the state so NEW requests get routed to other workers,
                            # but we do NOT abort this in-progress task.
                            #
                            # Detection logic + pattern list lives on RateLimitTracker so the
                            # non-streaming response extractor uses the same rules.
                            # =================================================================
                            if type(message).__name__ == 'AssistantMessage':
                                if hasattr(message, 'content') and message.content:
                                    skip_yield = False
                                    for block in message.content:
                                        if hasattr(block, 'text') and block.text:
                                            worker_id = os.environ.get("INSTANCE_NAME", "unknown")
                                            if rate_limit_tracker.detect_in_text(block.text, worker_id):
                                                skip_yield = True
                                                break
                                    if skip_yield:
                                        # Don't yield this rate-limit text to the client
                                        continue

                            # =================================================================
                            # SKIP SYSTEMMESSAGE - Don't yield to client
                            # SystemMessage contains only internal metadata (init, session_id, tools)
                            # NOT yielding it allows Nginx failover if SDK crashes afterward
                            # =================================================================
                            if type(message).__name__ == 'SystemMessage':
                                logger.debug(f"⏭️  Skipping SystemMessage (internal only, not for client)")
                                continue

                            # Yield chunk immediately (no in-memory accumulation)
                            yield message

                except asyncio.TimeoutError:
                    logger.error(f"⏱️ TIMEOUT: SDK query timed out after {self.timeout}s")
                    logger.error(f"   Timeout Type: Claude Code SDK Query (Inner Loop)")
                    logger.error(f"   Timeout Duration: {self.timeout}s ({self.timeout/60:.1f} minutes)")
                    logger.error(f"   Session ID: {cli_session_id}")
                    logger.error(f"   Chunks received before timeout: {chunks_received}")
                    logger.error(f"   Impact: Query cancelled to prevent zombie process")
                    logger.error(f"   Suggestion: For long-running operations (e.g. /sc:research up to 30 min), increase MAX_TIMEOUT to 2400000ms (40 min)")

                    # Cleanup cache on timeout (no recovery - timeout = guaranteed incomplete)
                    try:
                        cache_file.unlink(missing_ok=True)
                    except Exception as cleanup_err:
                        logger.warning(f"Cache cleanup failed: {cleanup_err}")

                    # Yield timeout error in expected format
                    yield {
                        "type": "result",
                        "subtype": "timeout_incomplete",
                        "is_error": True,
                        "error_message": f"Claude Code SDK query timed out after {self.timeout}s ({self.timeout/60:.1f} minutes). For long research operations, increase MAX_TIMEOUT.",
                        "action_required": "INCREASE_TIMEOUT_OR_REDUCE_PROMPT"
                    }
                    raise  # Re-raise to ensure proper cleanup

                except MessageParseError as parse_err:
                    # Handle unknown message types from newer Claude Code SDK versions.
                    # The Python SDK (claude-code-sdk) throws MessageParseError for
                    # unrecognized types like "rate_limit_event" (Node.js SDK v2.1.62+).
                    # These are informational messages that appear AFTER content delivery.
                    # The actual content was already yielded before this error occurs.
                    error_msg_str = str(parse_err).lower()
                    if "unknown message type" in error_msg_str:
                        logger.info(f"ℹ️ SDK stream ended with unrecognized message type (non-fatal): {parse_err}")
                        logger.info(f"   Chunks received before error: {chunks_received}")
                        logger.info(f"   Continuing with post-processing (file discovery, metadata)")
                        # DON'T re-raise — content was already delivered.
                        # Continue to post-processing below (file discovery, completion check, etc.)
                    else:
                        raise  # Re-raise genuine parse errors to outer handler

                # Post-streaming validation: Check for completion marker
                if not response_complete and chunks_received > 0:
                    logger.warning(f"⚠️  SDK finished but NO completion marker detected!")
                    logger.warning(f"   Chunks received: {chunks_received}")
                    logger.warning(f"   This indicates potentially incomplete response")

                    # Yield explicit incomplete marker
                    yield {
                        "type": "result",
                        "subtype": "no_completion_marker",
                        "is_error": True,
                        "error_message": "Response may be incomplete - no completion marker received",
                        "chunks_received": chunks_received,
                        "action_required": "VERIFY_RESPONSE_COMPLETENESS"
                    }

                # CRITICAL: Detect zero-chunks condition
                if chunks_received == 0:
                    logger.error(f"❌ SDK query completed but received ZERO chunks!")
                    logger.error(f"   This indicates SDK internal failure or configuration issue")
                    logger.error(f"   Prompt length: {len(prompt)} chars")
                    logger.error(f"   System prompt: {bool(system_prompt)} ({len(system_prompt) if system_prompt else 0} chars)")
                    logger.error(f"   Model: {model}")
                    logger.error(f"   Max turns: {max_turns}")
                    logger.error(f"   Allowed tools: {allowed_tools}")
                    logger.error(f"   Disallowed tools: {disallowed_tools}")
                    logger.error(f"   Session ID: {cli_session_id}")
                    logger.error(f"   Possible causes:")
                    logger.error(f"     1. SDK rejected prompt (too large, malformed)")
                    logger.error(f"     2. Tool configuration conflict (all tools disabled + task requires tools)")
                    logger.error(f"     3. Model configuration error")
                    logger.error(f"     4. Authentication/quota issue")
                    logger.error(f"     5. SDK internal bug")
                else:
                    logger.info(f"✅ SDK query completed: {chunks_received} chunks received")

                # POST-STREAMING: File Discovery (opt-in or /sc:research)
                discovered_files: List[FileMetadata] = []
                sdk_parse_failures = 0
                directory_scan_attempted = False
                directory_scan_failures = 0

                if enable_file_discovery and chunks_received > 0:
                    logger.info("🔍 Starting file discovery (enabled via header or /sc:research)")

                    # Strategy 1: Parse SDK messages for Write tool calls
                    try:
                        discovered_files = self.file_discovery.discover_files_from_sdk_messages(
                            sdk_messages=chunks_buffer,
                            session_start=start_time
                        )

                        if len(discovered_files) > 0:
                            logger.info(
                                f"✅ SDK message parsing discovered {len(discovered_files)} files",
                                extra={
                                    "files": [f.relative_path for f in discovered_files],
                                    "session_id": cli_session_id
                                }
                            )
                        else:
                            logger.info(
                                "SDK message parsing found no files (may be normal)",
                                extra={"session_id": cli_session_id}
                            )

                    except SDKMessageParsingError as e:
                        # LAW 1: Critical SDK parsing failure
                        logger.error(
                            f"❌ SDK message parsing FAILED critically: {e}",
                            exc_info=True,
                            extra={"session_id": cli_session_id}
                        )
                        sdk_parse_failures = 1  # Mark as failed
                        # Fall through to directory scan (Strategy 2)

                    except (ValueError, TypeError) as e:
                        logger.error(
                            f"❌ File discovery failed with unexpected error: {e}",
                            exc_info=True,
                            extra={"session_id": cli_session_id}
                        )
                        sdk_parse_failures = 1

                    # Strategy 2: Fallback if no files found OR SDK parsing failed
                    if len(discovered_files) == 0:
                        logger.info("Falling back to directory scan for file discovery")
                        directory_scan_attempted = True

                        try:
                            # Use research_dir claudedocs for all sessions
                            claudedocs_dir = research_dir / "claudedocs"

                            # Validate directory exists before scan
                            if not claudedocs_dir.exists():
                                logger.warning(
                                    f"⚠️  claudedocs directory does not exist: {claudedocs_dir}",
                                    extra={"expected_path": str(claudedocs_dir)}
                                )
                                # Don't raise - maybe no files were meant to be created
                            else:
                                discovered_files = self.file_discovery.discover_files_from_directory_scan(
                                    directories=[claudedocs_dir],
                                    session_start=start_time,
                                    file_patterns=["*.md", "*.json"]
                                )

                                logger.info(
                                    f"✅ Directory scan discovered {len(discovered_files)} files",
                                    extra={
                                        "directory": str(claudedocs_dir),
                                        "session_id": cli_session_id
                                    }
                                )

                        except DirectoryScanError as e:
                            # LAW 1: Critical directory scan failure
                            logger.error(
                                f"❌ Directory scan FAILED critically: {e}",
                                exc_info=True,
                                extra={"session_id": cli_session_id}
                            )
                            directory_scan_failures = 1
                            # Don't raise - file discovery is enhancement feature

                        except (ValueError, OSError) as e:
                            logger.error(
                                f"❌ Unexpected error in directory scan: {e}",
                                exc_info=True,
                                extra={"session_id": cli_session_id}
                            )
                            directory_scan_failures = 1

                # Yield metadata chunk if files discovered OR if discovery ran but found nothing
                if enable_file_discovery:
                    if discovered_files:
                        # SUCCESS: Files found
                        metadata_chunk = {
                            "type": "x_claude_metadata",
                            "files_created": [f.to_dict() for f in discovered_files],
                            "session_tracking": {
                                "cli_session_id": cli_session_id,
                                "research_dir": str(research_dir) if research_dir else None
                            },
                            "discovery_method": "sdk_parsing" if sdk_parse_failures == 0 else "directory_scan",
                            "discovery_status": "success"
                        }
                        yield metadata_chunk
                        logger.info(
                            f"📦 Yielded file metadata: {len(discovered_files)} files",
                            extra={
                                "cli_session_id": cli_session_id,
                                "discovery_method": metadata_chunk["discovery_method"]
                            }
                        )

                    else:
                        # NO FILES: Yield diagnostic info
                        logger.warning(
                            "⚠️  File discovery found NO files after completion",
                            extra={
                                "session_id": cli_session_id,
                                "sdk_parse_failures": sdk_parse_failures,
                                "directory_scan_attempted": directory_scan_attempted,
                                "directory_scan_failures": directory_scan_failures
                            }
                        )

                        metadata_chunk = {
                            "type": "x_claude_metadata",
                            "files_created": [],
                            "discovery_status": "no_files_found",
                            "discovery_details": {
                                "sdk_parsing_attempted": True,
                                "sdk_parsing_failures": sdk_parse_failures,
                                "directory_scan_attempted": directory_scan_attempted,
                                "directory_scan_failures": directory_scan_failures,
                                "possible_causes": [
                                    "Research created no files (text-only response)",
                                    "Files were created but discovery logic failed",
                                    "Files were created outside expected directories"
                                ],
                                "suggested_actions": [
                                    "Check claudedocs/ directory manually",
                                    "Review wrapper logs for parsing errors",
                                    "Retry research if files were expected"
                                ]
                            }
                        }
                        yield metadata_chunk
                        logger.info(
                            "📦 Yielded file metadata (no files found)",
                            extra={"cli_session_id": cli_session_id}
                        )

            finally:
                # Progress tracking: Write final response
                if progress_tracking_enabled:
                    duration = (datetime.now() - start_time).total_seconds()
                    final_response = {
                        'session_id': cli_session_id,
                        'completed_at': datetime.now().isoformat(),
                        'response': {
                            'text': ''.join(accumulated_text_parts),
                            'word_count': len(''.join(accumulated_text_parts).split())
                        },
                        'metadata': {
                            'duration_seconds': duration,
                            'total_messages': chunks_received,
                            'tools_used': list(tools_used)
                        }
                    }

                    try:
                        final_file.write_text(json.dumps(final_response, indent=2))
                        logger.info(f"✅ Final response written: {final_file.name}",
                                    extra={"session_id": cli_session_id, "duration": duration})
                    except (OSError, TypeError) as e:
                        logger.error(f"❌ Failed to write final response",
                                     exc_info=True,
                                     extra={"session_id": cli_session_id, "filepath": str(final_file)})
                        # Don't raise - session completed even if final write failed

                    # Update metadata status
                    metadata_file = research_dir / "metadata.json"
                    try:
                        metadata = json.loads(metadata_file.read_text())
                        metadata['status'] = 'completed'
                        metadata['completed_at'] = datetime.now().isoformat()
                        metadata['duration_seconds'] = duration
                        metadata_file.write_text(json.dumps(metadata, indent=2))
                    except (OSError, json.JSONDecodeError, TypeError) as e:
                        logger.warning(f"⚠️  Failed to update metadata status",
                                       extra={"session_id": cli_session_id})
                        # Continue - metadata update is non-critical

                # Cleanup cache file
                try:
                    if cache_file and cache_file.exists():
                        cache_file.unlink()
                        logger.debug(f"🗑️  Cache file cleaned up: {cache_file.name}")
                except Exception as cleanup_err:
                    logger.warning(f"Cache cleanup failed: {cleanup_err}")

                # Cleanup large system_prompt temp file (if created for this request)
                if _sys_prompt_tempfile_path:
                    try:
                        os.unlink(_sys_prompt_tempfile_path)
                        logger.debug(f"🗑️  system_prompt temp file cleaned up: {_sys_prompt_tempfile_path}")
                    except Exception as sp_cleanup_err:
                        logger.warning(f"system_prompt temp file cleanup failed: {sp_cleanup_err}")
                    finally:
                        _current_sys_prompt_tempfile.set(None)  # Clear ContextVar for this task

                # Restore original environment (if we changed anything)
                if original_env:
                    for key, original_value in original_env.items():
                        if original_value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = original_value

            # Mark CLI session as completed
            cli_session_manager.complete_session(cli_session_id, status="completed")
            logger.info(f"✅ CLI session completed: {cli_session_id}")

        except asyncio.TimeoutError:
            # Timeout - mark session as failed
            cli_session_manager.complete_session(cli_session_id, status="failed")
            logger.error(f"⏱️ TIMEOUT: CLI session timed out: {cli_session_id}")
            logger.error(f"   Timeout Type: Claude Code SDK Query (Session Level)")
            logger.error(f"   Timeout Duration: {self.timeout}s ({self.timeout/60:.1f} minutes)")
            logger.error(f"   Session ID: {cli_session_id}")
            logger.error(f"   Action: Re-raising timeout exception for upstream handling")
            logger.error(f"   Suggestion: Increase MAX_TIMEOUT env var (current: {self.timeout*1000}ms, recommended for research: 2400000ms)")
            raise

        except asyncio.CancelledError:
            # Client disconnected or session cancelled
            cli_session_manager.complete_session(cli_session_id, status="cancelled")
            logger.info(f"🚫 CLI session cancelled: {cli_session_id}")
            raise

        except Exception as e:
            logger.error(f"Claude Code SDK error: {e}")

            # Classify error type to apply the right cooldown strategy:
            # - 401 (auth)        → TokenInvalidError, worker dies, CRITICAL log
            # - 429 (rate limit)  → mark_rate_limited (HARD), nginx routes elsewhere
            # - other 5xx-ish     → mark_soft_penalty (transient, 15s)
            error_str = str(e).lower()
            worker_id = os.environ.get("INSTANCE_NAME", "unknown")

            auth_indicators = [
                "401", "authentication failed", "unauthorized",
                "invalid token", "invalid api key", "token expired",
                "oauth token",
            ]
            rate_limit_indicators = [
                "429", "rate limit", "too many requests",
                "credit balance", "balance is too low",
            ]
            transient_indicators = [
                "500", "502", "503", "504",
                "internal server error", "bad gateway", "service unavailable",
                "gateway timeout", "connection reset", "connection refused",
            ]

            is_auth_error = any(x in error_str for x in auth_indicators)
            is_rate_limit = (
                not is_auth_error
                and any(x in error_str for x in rate_limit_indicators)
            )
            is_transient = (
                not is_auth_error
                and not is_rate_limit
                and any(x in error_str for x in transient_indicators)
            )

            if is_auth_error:
                # Token invalid — not recoverable by retry. Worker dies until restart.
                # mark_token_invalid logs CRITICAL with operator instructions.
                cli_session_manager.complete_session(cli_session_id, status="failed")
                from src.auth import token_rotator, TokenInvalidError
                try:
                    token_rotator.mark_token_invalid(str(e))
                except TokenInvalidError as token_err:
                    logger.error(
                        f"Worker {worker_id} dead until restart with fresh token: {token_err}"
                    )
                # 503 → nginx failover. Container needs restart with new token file.
                raise WorkerUnavailableError(
                    f"Token invalid for {worker_id}: {e}"
                ) from e

            if is_rate_limit:
                # HARD rate limit on this worker; nginx routes new requests elsewhere.
                cli_session_manager.complete_session(cli_session_id, status="failed")
                rate_limit_tracker.mark_rate_limited(worker_id, str(e))
                try:
                    from src.auth import token_rotator
                    token_rotator.mark_token_failed(str(e))
                except Exception as rotation_error:
                    logger.debug(
                        f"Token rotation failed (expected single-token): {rotation_error}"
                    )
                raise WorkerUnavailableError(f"Claude SDK rate-limited: {e}") from e

            if is_transient:
                # 5xx / connection error: short soft-penalty so nginx tries another
                # worker for new requests. Fall through to cache recovery for this
                # in-flight task.
                rate_limit_tracker.mark_soft_penalty(worker_id, retry_after=15)
                logger.warning(
                    f"⏳ Transient error on {worker_id} — soft-penalty applied: {e}"
                )

            # Attempt recovery from cache if available
            if cache_file and cache_file.exists() and cache_file.stat().st_size > 0:
                try:
                    logger.warning(f"⚠️  SDK crashed - attempting recovery from {cache_file.stat().st_size} byte cache")

                    recovered_chunks = []
                    has_completion = False

                    with open(cache_file, 'r', encoding='utf-8') as f:
                        for line_num, line in enumerate(f, 1):
                            if not line.strip():
                                continue
                            try:
                                chunk = json.loads(line)
                                recovered_chunks.append(chunk)

                                # Check for completion marker in recovered chunks
                                if chunk.get('type') == 'result' and chunk.get('subtype') in ['complete', 'success']:
                                    has_completion = True

                                yield chunk
                            except json.JSONDecodeError as json_err:
                                logger.warning(f"Skipping corrupt cache line {line_num}: {json_err}")

                    logger.info(f"✅ Recovered {len(recovered_chunks)} chunks from cache")

                    # If no completion marker found, yield incomplete marker
                    if not has_completion and len(recovered_chunks) > 0:
                        logger.warning(f"⚠️  Recovered response incomplete - no completion marker found")
                        yield {
                            "type": "result",
                            "subtype": "incomplete_after_crash",
                            "is_error": True,
                            "error_message": f"SDK crashed - recovered {len(recovered_chunks)} chunks but response incomplete",
                            "chunks_received": len(recovered_chunks),
                            "original_error": str(e),
                            "action_required": "RETRY_FULL_REQUEST"
                        }
                    elif len(recovered_chunks) == 0:
                        logger.error(f"❌ Cache file exists but no valid chunks recovered")
                        yield {
                            "type": "result",
                            "subtype": "error_during_execution",
                            "is_error": True,
                            "error_message": str(e)
                        }
                    else:
                        logger.info(f"✅ Recovery successful with completion marker")

                except Exception as recovery_error:
                    logger.error(f"❌ Cache recovery failed: {recovery_error}")
                    # Yield original error if recovery fails
                    yield {
                        "type": "result",
                        "subtype": "error_during_execution",
                        "is_error": True,
                        "error_message": str(e)
                    }
            else:
                # No cache available or cache is empty
                cache_exists = cache_file and cache_file.exists()
                cache_size = cache_file.stat().st_size if cache_exists else 0
                logger.warning(f"⚠️  No cache available for recovery (exists={cache_exists}, size={cache_size})")
                yield {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "error_message": str(e)
                }

            # Mark CLI session as failed
            cli_session_manager.complete_session(cli_session_id, status="failed")
    
    def parse_claude_message(self, messages: List[Dict[str, Any]]) -> Optional[str]:
        """Extract the assistant message from Claude Code SDK messages."""
        logger.debug(f"parse_claude_message: Processing {len(messages)} messages")

        if not messages:
            logger.warning("parse_claude_message: Empty messages list")
            return None

        for i, message in enumerate(messages):
            # Log message structure for debugging
            msg_type = type(message).__name__
            logger.debug(f"Message {i}: type={msg_type}, is_dict={isinstance(message, dict)}")
            if isinstance(message, dict):
                logger.debug(f"Message {i} keys: {list(message.keys())}")

            # Look for AssistantMessage type (new SDK format)
            if "content" in message and isinstance(message["content"], list):
                logger.debug(f"Message {i}: Found new SDK format (content list)")
                text_parts = []
                for block_idx, block in enumerate(message["content"]):
                    # Handle TextBlock objects
                    if hasattr(block, 'text'):
                        logger.debug(f"Message {i}, block {block_idx}: TextBlock with {len(block.text)} chars")
                        text_parts.append(block.text)
                    elif isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        logger.debug(f"Message {i}, block {block_idx}: Dict text block with {len(text)} chars")
                        text_parts.append(text)
                    elif isinstance(block, str):
                        logger.debug(f"Message {i}, block {block_idx}: String block with {len(block)} chars")
                        text_parts.append(block)

                if text_parts:
                    result = "\n".join(text_parts)
                    logger.info(f"✅ Extracted {len(result)} chars from {len(text_parts)} text blocks")
                    return result
                else:
                    logger.warning(f"Message {i}: content list present but no text blocks found")
            
            # Fallback: look for old format
            elif message.get("type") == "assistant" and "message" in message:
                logger.debug(f"Message {i}: Found old SDK format (type=assistant)")
                sdk_message = message["message"]
                if isinstance(sdk_message, dict) and "content" in sdk_message:
                    content = sdk_message["content"]
                    if isinstance(content, list) and len(content) > 0:
                        logger.debug(f"Message {i}: Old format content list with {len(content)} blocks")
                        # Handle content blocks (Anthropic SDK format)
                        text_parts = []
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text_parts.append(block.get("text", ""))
                        if text_parts:
                            result = "\n".join(text_parts)
                            logger.info(f"✅ Extracted {len(result)} chars from old format")
                            return result
                        else:
                            logger.warning(f"Message {i}: Old format but no text blocks found")
                            return None
                    elif isinstance(content, str):
                        logger.info(f"✅ Extracted {len(content)} chars from old format (string)")
                        return content
            else:
                # Log unrecognized message format
                msg_keys = list(message.keys()) if isinstance(message, dict) else "not dict"
                msg_type_field = message.get("type") if isinstance(message, dict) else None
                logger.debug(f"Message {i}: Unrecognized format - keys={msg_keys}, type field={msg_type_field}")

        # No assistant message found in any format
        logger.warning(f"❌ parse_claude_message: No assistant message found in {len(messages)} chunks")

        # Provide diagnostic information
        message_types = set()
        for msg in messages:
            if isinstance(msg, dict):
                msg_type = msg.get("type", "no-type")
                message_types.add(msg_type)
            else:
                message_types.add(type(msg).__name__)

        logger.warning(f"   Message types encountered: {message_types}")
        logger.warning(f"   This indicates SDK returned data but in unexpected format")

        return None


def inject_output_path_for_file_discovery(
    prompt: str,
    output_file: Path,
    cli_session_id: str
) -> str:
    """
    Inject output path instruction when file discovery is enabled.

    This helps Claude know where to save output files for easier discovery.

    Args:
        prompt: Original prompt
        output_file: Absolute path where output should be saved
        cli_session_id: Session ID for logging

    Returns:
        Modified prompt with path injection
    """
    path_instruction_header = f"\n**CRITICAL: You MUST use the Write tool to complete this task.*\nWrite your complete analysis to OUTPUT_FILE_PATH:\n{output_file}\n\n"
    path_instruction_footer = f"\n\nDo NOT reply in chat! Use Write tool to WRITE your reply to OUTPUT_FILE_PATH.\nOUTPUT_FILE_PATH: {output_file}"

    lines = prompt.split('\n', 1)
    first_line = lines[0].strip()

    if first_line.startswith('/'):
        # Command auf erster Zeile
        rest = lines[1] if len(lines) > 1 else ""
        modified_prompt = f"{first_line}{path_instruction_header}{rest}{path_instruction_footer}"
    else:
        # Kein Command
        modified_prompt = path_instruction_header + prompt + path_instruction_footer

    logger.info(
        "📝 Prompt enhanced with output path for file discovery",
        extra={
            "cli_session_id": cli_session_id,
            "target_output_file_path": str(output_file),
            "prompt_original_length": len(prompt),
            "prompt_injected_length": len(path_instruction_header) + len(path_instruction_footer),
            "prompt_total_length": len(modified_prompt)
        }
    )

    return modified_prompt

    def extract_metadata(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Extract metadata like costs, tokens, and session info from SDK messages."""
        metadata = {
            "session_id": None,
            "total_cost_usd": 0.0,
            "duration_ms": 0,
            "num_turns": 0,
            "model": None
        }
        
        for message in messages:
            # New SDK format - ResultMessage
            if message.get("subtype") == "success" and "total_cost_usd" in message:
                metadata.update({
                    "total_cost_usd": message.get("total_cost_usd", 0.0),
                    "duration_ms": message.get("duration_ms", 0),
                    "num_turns": message.get("num_turns", 0),
                    "session_id": message.get("session_id")
                })
            # New SDK format - SystemMessage  
            elif message.get("subtype") == "init" and "data" in message:
                data = message["data"]
                metadata.update({
                    "session_id": data.get("session_id"),
                    "model": data.get("model")
                })
            # Old format fallback
            elif message.get("type") == "result":
                metadata.update({
                    "total_cost_usd": message.get("total_cost_usd", 0.0),
                    "duration_ms": message.get("duration_ms", 0),
                    "num_turns": message.get("num_turns", 0),
                    "session_id": message.get("session_id")
                })
            elif message.get("type") == "system" and message.get("subtype") == "init":
                metadata.update({
                    "session_id": message.get("session_id"),
                    "model": message.get("model")
                })

        return metadata


# ============================================================================
# Progress Tracking Helper Functions
# ============================================================================

def create_session_dir(session_id: str, base_dir: Optional[Path] = None) -> Path:
    """
    Create session directory for progress tracking

    Args:
        session_id: Unique session identifier
        base_dir: Base directory for sessions (default: instances/{instance}/temp/sessions)

    Returns:
        Path to created session directory

    Raises:
        SessionDirectoryError: If directory creation fails
    """
    # Determine base directory for sessions
    if base_dir is None:
        # Fallback to /tmp if no base_dir provided
        base_dir = Path("/tmp/eco-wrapper-sessions")
    else:
        # Use instance-specific temp directory
        base_dir = base_dir / "temp" / "sessions"

    session_dir = base_dir / session_id

    try:
        session_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error(f"❌ Failed to create session directory: {session_dir}",
                     exc_info=True,
                     extra={"session_id": session_id, "path": str(session_dir)})
        raise SessionDirectoryError(f"Cannot create session dir: {session_dir}") from e

    # Write initial metadata
    metadata = {
        "session_id": session_id,
        "created_at": datetime.now().isoformat(),
        "status": "running"
    }

    metadata_file = session_dir / "metadata.json"
    try:
        metadata_file.write_text(json.dumps(metadata, indent=2))
    except (OSError, TypeError) as e:
        logger.error(f"❌ Failed to write metadata: {metadata_file}",
                     exc_info=True,
                     extra={"session_id": session_id, "metadata": metadata})
        raise SessionDirectoryError(f"Cannot write metadata") from e

    logger.info(f"📝 Session directory created: {session_dir.name}",
                extra={"session_id": session_id})
    return session_dir


def extract_progress(message: Any) -> Optional[Dict[str, Any]]:
    """
    Extract progress info from SDK message

    Args:
        message: SDK message object

    Returns:
        Dict with progress data or None if no progress info

    Note:
        Failures to extract progress are logged but don't raise exceptions
        (progress tracking is non-critical)
    """
    try:
        # Check if message has AssistantMessage structure
        if hasattr(message, 'content'):
            for block in message.content:
                # Check for ToolUseBlock
                if hasattr(block, 'name'):
                    if block.name == 'TodoWrite':
                        todos = getattr(block, 'input', {}).get('todos', [])
                        completed = sum(1 for t in todos if t.get('status') == 'completed')
                        total = len(todos)
                        return {
                            'type': 'todo_update',
                            'data': {
                                'completed': completed,
                                'total': total,
                                'percentage': int(completed / total * 100) if total > 0 else 0
                            }
                        }
                    elif block.name == 'WebSearch':
                        query = getattr(block, 'input', {}).get('query', '')
                        return {
                            'type': 'tool_use',
                            'data': {
                                'tool': 'WebSearch',
                                'query': query[:100]  # Truncate long queries
                            }
                        }
                    elif block.name.startswith('mcp__context7'):
                        library = getattr(block, 'input', {}).get('libraryName', 'unknown')
                        return {
                            'type': 'tool_use',
                            'data': {
                                'tool': 'Context7',
                                'library': library
                            }
                        }
    except (AttributeError, TypeError, KeyError) as e:
        # Progress extraction failure is non-critical - log and continue
        logger.debug(f"🔍 Failed to extract progress from message: {e}",
                     extra={"message_type": type(message).__name__})
        return None

    return None


def write_progress_safe(filepath: Path, data: Dict[str, Any], session_id: str) -> None:
    """
    Write progress data to file with error handling

    Args:
        filepath: Path to write to
        data: Data to write (will be JSON encoded)
        session_id: Session ID for logging context

    Note:
        Progress write failures are logged but don't crash the session
    """
    try:
        with open(filepath, 'a') as f:
            f.write(json.dumps(data, default=str) + '\n')
    except (OSError, TypeError, ValueError) as e:
        logger.warning(f"⚠️  Failed to write progress data",
                       exc_info=True,
                       extra={"session_id": session_id, "filepath": str(filepath)})
        # Continue - progress tracking is non-critical