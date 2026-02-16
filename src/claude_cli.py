import asyncio
import json
import os
import subprocess
import uuid
import time
import hashlib
from typing import AsyncGenerator, Dict, Any, Optional, List
from pathlib import Path

from claude_code_sdk import query, ClaudeCodeOptions, Message
from config.logging_config import get_logger
from datetime import datetime, timedelta
import shutil

# File discovery for /sc:research
from src.file_discovery import FileDiscoveryService, FileMetadata, SDKMessageParsingError, DirectoryScanError

logger = get_logger(__name__)


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


class RateLimitTracker:
    """
    Tracks rate limit status per worker instance.
    Parses reset times from Claude's rate limit messages.
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._rate_limits: Dict[str, datetime] = {}  # worker_id -> reset_time
        self._initialized = True
        self._logger = get_logger(__name__)

    def parse_reset_time(self, message: str) -> Optional[datetime]:
        """
        Parse reset time from Claude's rate limit messages.
        Examples:
        - "resets 1pm (Europe/Vienna)"
        - "resets 2pm (Europe/Vienna)"
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

            # Convert to 24-hour format
            if am_pm == 'pm' and hour != 12:
                hour += 12
            elif am_pm == 'am' and hour == 12:
                hour = 0

            try:
                # Try to parse timezone
                tz = pytz.timezone(timezone_str)
                now = datetime.now(tz)
                reset_time = now.replace(hour=hour, minute=0, second=0, microsecond=0)

                # If reset time is in the past, it's for tomorrow
                if reset_time <= now:
                    reset_time += timedelta(days=1)

                self._logger.info(f"📅 Parsed reset time: {reset_time} ({timezone_str})")
                return reset_time
            except Exception as e:
                self._logger.warning(f"Failed to parse timezone '{timezone_str}': {e}")

        # Fallback: 1 hour from now
        return datetime.now() + timedelta(hours=1)

    def mark_rate_limited(self, worker_id: str, message: str) -> datetime:
        """Mark a worker as rate-limited and extract reset time"""
        reset_time = self.parse_reset_time(message)
        self._rate_limits[worker_id] = reset_time
        self._logger.warning(f"🚫 Worker {worker_id} rate-limited until {reset_time}")
        return reset_time

    def is_rate_limited(self, worker_id: str) -> bool:
        """Check if a specific worker is rate-limited"""
        if worker_id not in self._rate_limits:
            return False
        if datetime.now() >= self._rate_limits[worker_id]:
            # Rate limit expired
            del self._rate_limits[worker_id]
            return False
        return True

    def get_retry_after(self, worker_id: str) -> Optional[int]:
        """Get seconds until rate limit resets for a worker"""
        if worker_id in self._rate_limits:
            delta = self._rate_limits[worker_id] - datetime.now()
            return max(60, int(delta.total_seconds()))
        return None

    def get_all_rate_limits(self) -> Dict[str, datetime]:
        """Get all current rate limits"""
        # Clean up expired limits
        now = datetime.now()
        self._rate_limits = {k: v for k, v in self._rate_limits.items() if v > now}
        return self._rate_limits.copy()


# Global rate limit tracker instance
rate_limit_tracker = RateLimitTracker()


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
                    async for message in query(
                        prompt="Hello",
                        options=ClaudeCodeOptions(
                            max_turns=1,
                            cwd=self.cwd
                        )
                    ):
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

        # Append prompt as final argument
        cli_args.append(prompt)

        logger.info(f"📝 Executing native CLI: {' '.join(cli_args[:5])}...")

        try:
            # Run claude CLI directly
            result = subprocess.run(
                cli_args,
                cwd=str(session_dir),  # Execute in session directory
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
1. Use mcp__tavily__tavily-search for 2-3 TARGETED searches only
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

                # Build SDK options
                options = ClaudeCodeOptions(
                    max_turns=max_turns,
                    cwd=research_cwd
                )

                # Set permission mode if specified via environment variable
                permission_mode = os.getenv("CLAUDE_PERMISSION_MODE")
                if permission_mode:
                    options.permission_mode = permission_mode
                    logger.info(f"🔓 Permission mode set to: {permission_mode}")

                # Set model if specified
                if model:
                    options.model = model
                    
                # Set system prompt if specified
                if system_prompt:
                    options.system_prompt = system_prompt
                    
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
                    options.continue_session = True
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

                # === LARGE PROMPT HANDLING ===
                # OS has ARG_MAX limit (~128-256KB) for command-line arguments.
                # String prompts are passed as CLI args and fail with [Errno 7] if too large.
                # Solution: Use streaming mode (stdin) for large prompts.
                LARGE_PROMPT_THRESHOLD = 100_000  # ~100KB, safely under ARG_MAX

                async def _prompt_to_stream(prompt_text: str):
                    """Convert string prompt to AsyncIterable for streaming mode."""
                    yield {
                        "type": "user",
                        "message": {"role": "user", "content": prompt_text}
                    }

                if len(prompt) > LARGE_PROMPT_THRESHOLD:
                    logger.info(f"🔄 Large prompt detected ({len(prompt):,} chars > {LARGE_PROMPT_THRESHOLD:,}), using streaming mode to bypass ARG_MAX")
                    prompt_source = _prompt_to_stream(prompt)
                else:
                    prompt_source = prompt
                # === END LARGE PROMPT HANDLING ===

                try:
                    async with asyncio.timeout(self.timeout):
                        async for message in query(prompt=prompt_source, options=options):
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
                            # EARLY RATE LIMIT DETECTION
                            # Must detect BEFORE yielding - once data is sent to client,
                            # Nginx cannot failover to another worker!
                            # =================================================================
                            if type(message).__name__ == 'AssistantMessage':
                                if hasattr(message, 'content') and message.content:
                                    for block in message.content:
                                        if hasattr(block, 'text') and block.text:
                                            text_lower = block.text.lower()
                                            # Check for rate limit patterns
                                            rate_limit_patterns = [
                                                "hit your limit",
                                                "you've hit your limit",
                                                "rate limit",
                                                "usage limit",
                                                "quota exceeded",
                                                "too many requests",
                                                "capacity",
                                                "try again later"
                                            ]
                                            if any(pattern in text_lower for pattern in rate_limit_patterns):
                                                error_msg = block.text[:200]
                                                full_msg = block.text
                                                logger.warning(f"🚫 RATE LIMIT DETECTED (early): {error_msg}")

                                                # Track rate limit with reset time
                                                worker_id = os.environ.get("INSTANCE_NAME", "unknown")
                                                reset_time = rate_limit_tracker.mark_rate_limited(worker_id, full_msg)
                                                retry_after = rate_limit_tracker.get_retry_after(worker_id)

                                                logger.warning(f"   Worker: {worker_id}")
                                                logger.warning(f"   Reset time: {reset_time}")
                                                logger.warning(f"   Retry-After: {retry_after}s")
                                                logger.warning(f"   Raising WorkerUnavailableError for Nginx failover")

                                                # Mark session as failed before raising
                                                cli_session_manager.complete_session(cli_session_id, status="failed")

                                                # Raise WorkerUnavailableError (triggers 503 for Nginx failover)
                                                # The RateLimitError is used when ALL workers are exhausted
                                                raise WorkerUnavailableError(f"Rate limit detected: {error_msg}")

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

            # Check if this is an auth/rate-limit error that warrants failover
            error_str = str(e).lower()

            # Errors that indicate this worker cannot handle ANY requests right now
            is_worker_unavailable = any(x in error_str for x in [
                "credit balance", "balance is too low", "rate limit",
                "authentication failed", "unauthorized", "invalid token",
                "oauth token", "token expired", "401", "invalid api key",
                "exit code 1"  # Generic SDK failure - let another worker try
            ])

            if is_worker_unavailable:
                # Mark session as failed
                cli_session_manager.complete_session(cli_session_id, status="failed")

                # Try token rotation (but it may fail if no backup tokens)
                try:
                    from src.auth import token_rotator
                    token_rotator.mark_token_failed(str(e))
                    logger.debug(f"Token rotated for future requests")
                except RuntimeError as rotation_error:
                    logger.debug(f"Token rotation failed (expected with single token): {rotation_error}")

                # Raise exception to trigger HTTP 503 → Nginx failover to other worker
                logger.debug(f"Worker unavailable, Nginx failover: {e}")
                raise WorkerUnavailableError(f"Claude SDK failed: {e}") from e

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