import os
import json
import asyncio
import secrets
import string
import re
import time
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
import shutil
from typing import Optional, AsyncGenerator, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, Response
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from dotenv import load_dotenv

# Load environment variables first
load_dotenv()

# Import centralized logging configuration
from config.logging_config import setup_logging, get_logger

from src.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionStreamResponse,
    Choice,
    Message,
    Usage,
    StreamChoice,
    ErrorResponse,
    ErrorDetail,
    SessionInfo,
    SessionListResponse,
    ResearchRequest,
    ResearchResponse,
    BackendType,
    PrivacyMode,
    BackendInfo,
    SmartAnonymizeRequest,
    SmartAnonymizeResponse,
    ConvertPdfResponse
)
from src.claude_cli import ClaudeCodeCLI, WorkerUnavailableError, RateLimitError, rate_limit_tracker
from src.message_adapter import MessageAdapter
from src.tool_leak_detector import hardened_system_prompt, looks_like_tool_leak
from src.vision_provider import VisionProvider, get_vision_provider
from src.routing.vision_router import check_and_route_vision, prepare_messages_for_vision, has_vision_content
from src.routing.backend_router import resolve_backend_config, get_backend_info_dict, BackendConfig
from src.auth import verify_api_key, security, validate_claude_code_auth, get_claude_code_auth_info, bedrock_credential_manager
from src.parameter_validator import ParameterValidator, CompatibilityReporter
from src.model_registry import (
    get_models_for_api,
    resolve_model,
    ModelResolutionError,
    get_all_model_ids
)
from src.file_discovery import FileDiscoveryService
from src.session_manager import session_manager
# Privacy: Use lightweight HTTP client (no Presidio/spaCy in worker)
from src.privacy_client import get_privacy_client
from src.tenant import (
    TenantMiddleware,
    get_tenant_from_request,
    get_privacy_mode_from_request,
    get_user_id_from_request,
    get_app_id_from_request,
    get_agent_id_from_request,
    get_session_id_from_request,
    get_workflow_id_from_request,
    get_job_id_from_request,
    track_request_usage
)
# Rate limiting - required in production, optional in development
try:
    from src.rate_limiter import limiter, rate_limit_exceeded_handler, get_rate_limit_for_endpoint, rate_limit_endpoint
    RATE_LIMITING_ENABLED = True
except ImportError:
    RATE_LIMITING_ENABLED = False
    limiter = None

    # Check if we're in production (Docker) - rate limiting should be required
    IN_DOCKER = os.path.exists('/.dockerenv') or os.getenv('DOCKER_CONTAINER', 'false').lower() == 'true'
    if IN_DOCKER:
        raise RuntimeError(
            "CRITICAL: Rate limiting is required in production but slowapi is not installed. "
            "Install with: pip install slowapi"
        )

    # Development fallback with visible warning
    import warnings
    warnings.warn(
        "Rate limiting disabled (slowapi not installed). This is a SECURITY RISK in production!",
        RuntimeWarning
    )

    # No-op decorator for development only
    def rate_limit_endpoint(endpoint_name: str):
        def decorator(func):
            return func
        return decorator

# Legacy request limiter — kept only for the /stats, /ready, /worker-capacity
# endpoints, which expose active_requests + memory stats. The real concurrency
# gating is done by AdaptiveLoadLimiter below; the legacy counter is never
# incremented anymore (was wired via concurrency_limit / middleware, both
# removed). Marked for full removal once consumers migrate to the new
# endpoints (/v1/metrics/limiter-trajectory, /v1/metrics/upstream-health).
from src.request_limiter import get_limiter
# Adaptive token-budget limiter (replaces static MAX_CONCURRENT_REQUESTS gating)
from src.middleware.adaptive_limiter import (
    adaptive_limit_dependency,
    get_adaptive_limiter,
    estimate_request_tokens,
)
from src.middleware.bridge_error import (
    BridgeError,
    bridge_error,
    classify_exception,
    config_error,
    SOURCE_BRIDGE_INTERNAL,
    SOURCE_UPSTREAM_ANTHROPIC,
    TYPE_INTERNAL,
)
# Starlette's HTTPException is raised by the router itself (e.g. for 404 Not
# Found). It's a PARENT class of fastapi.HTTPException — the FastAPI one inherits
# from it — so a handler registered for the Starlette base class also catches
# every FastAPI HTTPException. Without this, 404s from missing routes return a
# raw {"detail": "Not Found"} instead of our envelope.
from starlette.exceptions import HTTPException as StarletteHTTPException

# Configure centralized logging
# Backwards compatibility: Support DEBUG_MODE/VERBOSE for log level override
DEBUG_MODE = os.getenv('DEBUG_MODE', 'false').lower() in ('true', '1', 'yes', 'on')
VERBOSE = os.getenv('VERBOSE', 'false').lower() in ('true', '1', 'yes', 'on')

# Determine log level (backwards compatible with DEBUG_MODE/VERBOSE)
if DEBUG_MODE or VERBOSE:
    log_level = 'DEBUG'
else:
    log_level = os.getenv('LOG_LEVEL', 'INFO').upper()

# Initialize centralized logging with environment-based configuration
setup_logging(
    log_level=log_level,
    enable_diagnostic=os.getenv('ENABLE_DIAGNOSTIC', 'false').lower() in ('true', '1', 'yes', 'on'),
    log_to_console=True,
    log_to_file=os.getenv('LOG_TO_FILE', 'true').lower() in ('true', '1', 'yes', 'on'),
    filter_sensitive_data=os.getenv('FILTER_SENSITIVE_DATA', 'true').lower() in ('true', '1', 'yes', 'on')
)

# Get module logger
logger = get_logger(__name__)

# Global variable to store runtime-generated API key
runtime_api_key = None

def generate_secure_token(length: int = 32) -> str:
    """Generate a secure random token for API authentication."""
    alphabet = string.ascii_letters + string.digits + '-_'
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def prompt_for_api_protection() -> Optional[str]:
    """
    Interactively ask user if they want API key protection.
    Returns the generated token if user chooses protection, None otherwise.
    """
    # Don't prompt if API_KEY is already set via environment variable
    if os.getenv("API_KEY"):
        return None
    
    print("\n" + "="*60)
    print("🔐 API Endpoint Security Configuration")
    print("="*60)
    print("Would you like to protect your API endpoint with an API key?")
    print("This adds a security layer when accessing your server remotely.")
    print("")
    
    while True:
        try:
            choice = input("Enable API key protection? (y/N): ").strip().lower()
            
            if choice in ['', 'n', 'no']:
                print("✅ API endpoint will be accessible without authentication")
                print("="*60)
                return None
            
            elif choice in ['y', 'yes']:
                token = generate_secure_token()
                print("")
                print("🔑 API Key Generated!")
                print("="*60)
                print(f"API Key: {token}")
                print("="*60)
                print("📋 IMPORTANT: Save this key - you'll need it for API calls!")
                print("   Example usage:")
                print(f'   curl -H "Authorization: Bearer {token}" \\')
                print("        http://localhost:8000/v1/models")
                print("="*60)
                return token
            
            else:
                print("Please enter 'y' for yes or 'n' for no (or press Enter for no)")
                
        except (EOFError, KeyboardInterrupt):
            print("\n✅ Defaulting to no authentication")
            return None

# Initialize Claude CLI
# MAX_TIMEOUT: 2400000ms (40 min) for SuperClaude research (up to 30 min + buffer)
claude_cli = ClaudeCodeCLI(
    timeout=int(os.getenv("MAX_TIMEOUT", "2400000")),
    cwd=os.getenv("CLAUDE_CWD")
)


async def cleanup_old_sessions():
    """
    Background task to cleanup sessions older than 24h

    This task runs indefinitely and never crashes the app.
    All errors are logged but don't propagate.

    Scans all instance directories for temp/sessions folders.
    """
    # Use environment variable with Docker-friendly default
    INSTANCES_DIR = Path(os.getenv("INSTANCES_DIR", "/app/instances"))
    RETENTION_HOURS = 24
    CHECK_INTERVAL_SECONDS = 3600  # 1 hour

    logger.info(f"🧹 Session cleanup task started (retention: {RETENTION_HOURS}h, "
                f"interval: {CHECK_INTERVAL_SECONDS}s)")

    while True:
        try:
            cutoff = datetime.now() - timedelta(hours=RETENTION_HOURS)
            total_cleaned = 0
            total_failed = 0

            # Scan all instance directories
            if not INSTANCES_DIR.exists():
                logger.warning(f"⚠️  Instances directory not found: {INSTANCES_DIR}")
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
                continue

            try:
                instance_dirs = [d for d in INSTANCES_DIR.iterdir() if d.is_dir()]
            except OSError as e:
                logger.error(f"❌ Failed to list instances directory: {INSTANCES_DIR}",
                             exc_info=True,
                             extra={"path": str(INSTANCES_DIR)})
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
                continue

            # Check each instance's temp/sessions directory AND research_dir directories
            for instance_dir in instance_dirs:
                cleaned = 0
                failed = 0

                # Strategy 1: Cleanup temp/sessions (legacy progress tracking)
                sessions_dir = instance_dir / "temp" / "sessions"
                session_dirs = []

                if sessions_dir.exists():
                    try:
                        session_dirs.extend(list(sessions_dir.iterdir()))
                    except OSError as e:
                        logger.error(f"❌ Failed to list temp/sessions in instance {instance_dir.name}",
                                     exc_info=True,
                                     extra={"path": str(sessions_dir)})

                # Strategy 2: Cleanup research_dir directories (YYYY-MM-DD-HHMM_{uuid})
                # Pattern: 2025-10-31-1706_fd2862f5-502b-4318-871c-9ea28ccf3456
                research_dir_pattern = re.compile(r'^\d{4}-\d{2}-\d{2}-\d{4}_[a-f0-9-]{36}$')

                try:
                    for item in instance_dir.iterdir():
                        if item.is_dir() and research_dir_pattern.match(item.name):
                            session_dirs.append(item)
                except OSError as e:
                    logger.error(f"❌ Failed to list research_dirs in instance {instance_dir.name}",
                                 exc_info=True,
                                 extra={"path": str(instance_dir)})

                if len(session_dirs) == 0:
                    logger.debug(f"🔍 No sessions to cleanup in instance: {instance_dir.name}")
                    continue

                # Iterate all session directories in this instance (both temp/sessions and research_dir)

                for session_dir in session_dirs:
                    if not session_dir.is_dir():
                        continue

                    metadata_file = session_dir / "metadata.json"
                    if not metadata_file.exists():
                        logger.debug(f"🔍 No metadata in session: {session_dir.name}")
                        continue

                    try:
                        # Read and parse metadata
                        metadata_text = metadata_file.read_text()
                        metadata = json.loads(metadata_text)

                        # Determine session timestamp (prefer completed_at, fallback to created_at)
                        timestamp_str = metadata.get('completed_at') or metadata.get('created_at')
                        if not timestamp_str:
                            logger.warning(f"⚠️  No timestamp in metadata: {session_dir.name}",
                                           extra={"session_id": session_dir.name})
                            continue

                        # Parse timestamp and check age
                        timestamp = datetime.fromisoformat(timestamp_str)
                        age = datetime.now() - timestamp

                        if timestamp < cutoff:
                            # Session is old enough to delete
                            # Security: Validate path is actually under INSTANCES_DIR (no symlink escape)
                            real_session_path = Path(os.path.realpath(session_dir))
                            real_instances_path = Path(os.path.realpath(INSTANCES_DIR))

                            if not str(real_session_path).startswith(str(real_instances_path)):
                                logger.error(f"❌ Security: Session path escapes instances dir (symlink?): {session_dir}",
                                             extra={"session_dir": str(session_dir), "real_path": str(real_session_path)})
                                continue

                            try:
                                shutil.rmtree(session_dir)
                                cleaned += 1
                                total_cleaned += 1
                                logger.info(f"🧹 Cleaned up session: {session_dir.name} [{instance_dir.name}]",
                                            extra={
                                                "session_id": session_dir.name,
                                                "instance": instance_dir.name,
                                                "age_hours": age.total_seconds() / 3600
                                            })
                            except OSError as e:
                                failed += 1
                                total_failed += 1
                                logger.error(f"❌ Failed to delete session directory: {session_dir.name}",
                                             exc_info=True,
                                             extra={
                                                 "session_id": session_dir.name,
                                                 "instance": instance_dir.name,
                                                 "path": str(session_dir)
                                             })

                    except json.JSONDecodeError as e:
                        logger.error(f"❌ Invalid JSON in metadata: {metadata_file}",
                                     exc_info=True,
                                     extra={"session_id": session_dir.name, "instance": instance_dir.name, "filepath": str(metadata_file)})
                        continue

                    except (ValueError, TypeError) as e:
                        logger.error(f"❌ Invalid timestamp in metadata: {session_dir.name}",
                                     exc_info=True,
                                     extra={"session_id": session_dir.name, "instance": instance_dir.name, "timestamp": timestamp_str})
                        continue

                    except OSError as e:
                        logger.error(f"❌ Failed to read metadata: {metadata_file}",
                                     exc_info=True,
                                     extra={"session_id": session_dir.name, "instance": instance_dir.name, "filepath": str(metadata_file)})
                        continue

                # Log cleanup summary for this instance
                if cleaned > 0 or failed > 0:
                    logger.info(f"✅ Cleanup cycle for {instance_dir.name}",
                                extra={
                                    "instance": instance_dir.name,
                                    "cleaned": cleaned,
                                    "failed": failed
                                })

            # Log total cleanup summary
            if total_cleaned > 0 or total_failed > 0:
                logger.info(f"✅ Total cleanup cycle complete",
                            extra={
                                "total_cleaned": total_cleaned,
                                "total_failed": total_failed,
                                "retention_hours": RETENTION_HOURS
                            })

        except Exception as e:
            # Catch-all for unexpected errors - log but never crash
            logger.error(f"❌ Unexpected error in cleanup task",
                         exc_info=True,
                         extra={"error_type": type(e).__name__})

        # Wait before next cleanup cycle
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Verify Claude Code authentication and CLI on startup."""
    logger.info("Verifying Claude Code authentication and CLI...")
    
    # Validate authentication first
    auth_valid, auth_info = validate_claude_code_auth()
    
    if not auth_valid:
        logger.error("❌ Claude Code authentication failed!")
        for error in auth_info.get('errors', []):
            logger.error(f"  - {error}")
        logger.warning("Authentication setup guide:")
        logger.warning("  1. For Claude CLI (RECOMMENDED): claude login (OAuth, free)")
        logger.warning("  2. For Bedrock: Set CLAUDE_CODE_USE_BEDROCK=1 + AWS credentials")
        logger.warning("  3. For Vertex AI: Set CLAUDE_CODE_USE_VERTEX=1 + GCP credentials")
        logger.warning("")
        logger.warning("⚠️  ANTHROPIC_API_KEY is NOT supported - use OAuth via 'claude login'")
    else:
        logger.info(f"✅ Claude Code authentication validated: {auth_info['method']}")

    # Then verify CLI (unless skipped for debugging)
    skip_verification = os.getenv('SKIP_SDK_VERIFICATION', 'false').lower() in ('true', '1', 'yes', 'on')
    if skip_verification:
        logger.warning("⚠️  SKIP_SDK_VERIFICATION is set - skipping Claude Code SDK verification!")
        logger.warning("   This is for debugging only. SDK calls may fail at runtime.")
        cli_verified = True
    else:
        # LAW 1: verify_cli() now raises RuntimeError on critical failures
        # We catch it here to provide additional context before re-raising
        try:
            cli_verified = await claude_cli.verify_cli()

            if cli_verified:
                logger.info("✅ Claude Code CLI verified successfully")
            else:
                # verify_cli() returned False = non-critical failure (SDK not installed)
                # This is NOT a RuntimeError, so we handle it here
                logger.error("❌ Claude Code CLI verification failed (non-critical)!")
                logger.error("   Reason: SDK not installed or not in PATH")
                logger.error("   Impact: Server will NOT start")
                logger.error("")
                logger.error("Action required: Install Claude Code SDK")
                raise RuntimeError(
                    "Claude Code SDK not installed - server startup aborted. "
                    "Install with: npm install -g @anthropic-ai/claude-code"
                )

        except RuntimeError as e:
            # LAW 1: Critical failure from verify_cli() - add context and re-raise
            logger.error("="*70)
            logger.error("STARTUP ABORTED: Claude Code SDK verification failed")
            logger.error("="*70)
            logger.error(f"Error: {e}")
            logger.error("")
            logger.error("The wrapper cannot start without a working Claude Code SDK.")
            logger.error("Please fix the issue above and restart the wrapper.")
            logger.error("="*70)
            # Re-raise to trigger uvicorn shutdown
            raise
    
    # Log debug information if debug mode is enabled
    if DEBUG_MODE or VERBOSE:
        logger.debug("🔧 Debug mode enabled - Enhanced logging active")
        logger.debug(f"🔧 Environment variables:")
        logger.debug(f"   DEBUG_MODE: {DEBUG_MODE}")
        logger.debug(f"   VERBOSE: {VERBOSE}")
        logger.debug(f"   PORT: {os.getenv('PORT', '8000')}")
        cors_default = '["*"]'
        logger.debug(f"   CORS_ORIGINS: {os.getenv('CORS_ORIGINS', cors_default)}")
        logger.debug(f"   MAX_TIMEOUT: {os.getenv('MAX_TIMEOUT', '600000')}")
        logger.debug(f"   CLAUDE_CWD: {os.getenv('CLAUDE_CWD', 'Not set')}")
        logger.debug(f"🔧 Available endpoints:")
        logger.debug(f"   POST /v1/chat/completions - Main chat endpoint")
        logger.debug(f"   GET  /v1/models - List available models")
        logger.debug(f"   POST /v1/debug/request - Debug request validation")
        logger.debug(f"   GET  /v1/auth/status - Authentication status")
        logger.debug(f"   GET  /health - Health check")
        logger.debug(f"🔧 API Key protection: {'Enabled' if (os.getenv('API_KEY') or runtime_api_key) else 'Disabled'}")
    
    # Start session cleanup task
    session_manager.start_cleanup_task()

    # Start progress monitoring cleanup task
    asyncio.create_task(cleanup_old_sessions())
    logger.info("🧹 Progress monitoring cleanup task started (24h retention)")

    # Start Gemini daily rate limit reset task
    async def _gemini_daily_reset():
        """Reset Gemini rate limit counter at midnight UTC."""
        from datetime import datetime, timezone, timedelta
        while True:
            now = datetime.now(timezone.utc)
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            seconds_until_midnight = (tomorrow - now).total_seconds()
            await asyncio.sleep(seconds_until_midnight)
            try:
                from src.providers.gemini_oauth import gemini_oauth_manager
                gemini_oauth_manager.reset_daily_counter()
            except Exception as e:
                logger.warning(f"Gemini daily reset failed: {e}")

    asyncio.create_task(_gemini_daily_reset())
    logger.info("🔄 Gemini daily rate limit reset task scheduled (midnight UTC)")

    # Initialize adaptive token-budget limiter and start its background tune loop.
    # The limiter persists its cap across restarts, so we don't reset state here —
    # we just spin up the periodic auto-tune task.
    try:
        _ad_limiter = get_adaptive_limiter()
        _ad_limiter.start_tune_loop()
        logger.info(
            f"🎚️  AdaptiveLoadLimiter active: cap={_ad_limiter.state.cap_tokens:,} tokens "
            f"(floor={_ad_limiter.state.floor_tokens:,}, ceiling={_ad_limiter.state.ceiling_tokens:,})"
        )
    except Exception as _e:
        logger.error(f"Failed to start AdaptiveLoadLimiter: {_e}")

    yield

    # Cleanup on shutdown
    logger.info("Shutting down session manager...")
    session_manager.shutdown()
    try:
        get_adaptive_limiter().stop()
    except Exception:
        pass


# Create FastAPI app
app = FastAPI(
    title="Claude Code OpenAI API Wrapper",
    description="OpenAI-compatible API for Claude Code",
    version="1.0.0",
    lifespan=lifespan
)

# Configure CORS
cors_origins = json.loads(os.getenv("CORS_ORIGINS", '["*"]'))
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add tenant-aware middleware for WerkingFlow integration
# Provides per-tenant privacy modes, rate limiting, and usage tracking
app.add_middleware(TenantMiddleware)

# Add performance monitoring middleware (FIRST - to track all requests)
# Pure ASGI implementation - streaming-safe
from src.middleware.performance_monitor import PerformanceMonitorMiddleware
from src.middleware.event_logger import EventLogger
# Re-enabled: Pure ASGI implementation, not affected by BaseHTTPMiddleware bug
app.add_middleware(PerformanceMonitorMiddleware)

# Concurrency limiter — only memory-threshold safety net.
# Adaptive cap_tokens does the real throttling per worker; hardcoded
# concurrency caps would override that learning. Default is effectively
# unlimited; set MAX_CONCURRENT_REQUESTS in env only to override.
max_concurrent = int(os.getenv("MAX_CONCURRENT_REQUESTS", "1000"))
memory_threshold = float(os.getenv("MEMORY_THRESHOLD_PERCENT", "90.0"))
request_limiter = get_limiter(max_concurrent=max_concurrent, memory_threshold=memory_threshold)

# Add rate limiting error handler
if limiter:
    app.state.limiter = limiter
    app.add_exception_handler(429, rate_limit_exceeded_handler)

# Debug logging is handled by LOG_LEVEL environment variable
# Use: LOG_LEVEL=DEBUG poetry run uvicorn main:app for detailed logging


# Custom exception handler for 422 validation errors
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle request validation errors with detailed debugging information."""
    
    # Log the validation error details
    logger.error(f"❌ Request validation failed for {request.method} {request.url}")
    logger.error(f"❌ Validation errors: {exc.errors()}")
    
    # Create detailed error response
    error_details = []
    for error in exc.errors():
        location = " -> ".join(str(loc) for loc in error.get("loc", []))
        error_details.append({
            "field": location,
            "message": error.get("msg", "Unknown validation error"),
            "type": error.get("type", "validation_error"),
            "input": error.get("input")
        })
    
    # If debug mode is enabled, include the raw request body
    debug_info = {}
    if DEBUG_MODE or VERBOSE:
        try:
            body = await request.body()
            if body:
                debug_info["raw_request_body"] = body.decode('utf-8', errors='replace')
        except UnicodeDecodeError as e:
            debug_info["raw_request_body"] = f"Could not decode request body: {e}"
            logger.debug(f"Request body decode error: {e}")
        except Exception as e:
            debug_info["raw_request_body"] = f"Could not read request body: {type(e).__name__}: {e}"
            logger.debug(f"Request body read error: {e}")
    
    # Validation errors originate in the bridge (before any upstream call) but
    # they're client-caused: retrying won't fix them. Tag as bridge_internal /
    # validation_error with retryable=false so callers know not to retry.
    error_response = {
        "error": {
            "message": f"[Bridge {os.getenv('INSTANCE_NAME', 'unknown')}] Request validation failed - the request body doesn't match the expected format",
            "type": "validation_error",
            "code": "invalid_request_error",
            "source": SOURCE_BRIDGE_INTERNAL,
            "bridge_type": "validation_error",
            "retryable": False,
            "retry_after_s": None,
            "bridge_worker": os.getenv("INSTANCE_NAME", "unknown"),
            "timestamp": int(time.time()),
            "details": error_details,
            "help": {
                "common_issues": [
                    "Missing required fields (model, messages)",
                    "Invalid field types (e.g. messages should be an array)",
                    "Invalid role values (must be 'system', 'user', or 'assistant')",
                    "Invalid parameter ranges (e.g. temperature must be 0-2)"
                ],
                "debug_tip": "Set DEBUG_MODE=true or VERBOSE=true environment variable for more detailed logging"
            }
        }
    }

    # Add debug info if available
    if debug_info:
        error_response["error"]["debug"] = debug_info

    return JSONResponse(
        status_code=422,
        content=error_response
    )


# =============================================================================
# Worker Unavailable Exception Handler - Enables Nginx Failover
# =============================================================================
@app.exception_handler(WorkerUnavailableError)
async def worker_unavailable_handler(request: Request, exc: WorkerUnavailableError):
    """
    Handle WorkerUnavailableError by returning HTTP 503 Service Unavailable.

    This triggers Nginx's proxy_next_upstream directive to automatically
    retry the request on another worker. The user never sees this error
    because Nginx seamlessly fails over.

    Nginx config required:
        proxy_next_upstream error timeout http_500 http_502 http_503 http_504 http_429;
        proxy_next_upstream_tries 2;
    """
    worker_id = os.getenv("INSTANCE_NAME", "unknown")
    retry_after = rate_limit_tracker.get_retry_after(worker_id) or 0

    logger.debug(
        f"Worker failover (normal operation) - Nginx retries on next worker",
        extra={
            "path": str(request.url),
            "method": request.method,
            "error": str(exc),
            "worker_id": worker_id,
            "retry_after": retry_after
        }
    )

    # Include rate limit info if this worker is rate-limited
    rate_limited = rate_limit_tracker.is_rate_limited(worker_id)
    error_message = "Worker temporarily unavailable, request will be retried on another worker"
    if rate_limited:
        error_message = f"Worker {worker_id} rate-limited. Retrying on another worker. Reset in {retry_after}s."

    # 429 = "come back later". nginx listens via proxy_next_upstream http_429
    # and routes to the next worker exactly like the previous 503 did. When all
    # workers are exhausted the final 429 is the correct client-facing contract
    # (no 5xx needed; Retry-After header tells the client when to try again).
    return JSONResponse(
        status_code=429,
        content={
            "error": {
                # OpenAI-compat
                "message": f"[Bridge {worker_id}] {error_message}",
                "type": "service_unavailable",
                "code": "429",
                # Bridge discriminators — let apps distinguish "bridge failover"
                # from a real upstream error.
                "source": "bridge_account",
                "bridge_type": "worker_unavailable",
                "reason": "worker_unavailable_for_failover",
                "bridge_worker": worker_id,
                "retryable": True,
                "retry": True,
                "rate_limited": rate_limited,
                "retry_after_s": retry_after,
                "retry_after_seconds": retry_after,  # legacy alias
            }
        },
        headers={
            "Retry-After": str(retry_after) if retry_after > 0 else "0",
            "X-Worker-Failover": "true",
            "X-Worker-Id": worker_id,
            "X-Rate-Limited": str(rate_limited).lower()
        }
    )


# =============================================================================
# Rate Limit Exception Handler - Returns 429 with Retry-After
# =============================================================================
# CROSS-WORKER RETRY — when one worker is rate-limited but others have
# capacity, the bridge transparently retries on the next-best worker so the
# client only sees the final successful response. Bounded to 2 retries via
# X-Bridge-Retry-Count header (3 attempts total). The X-Bridge-Retry-Excluded
# header carries the cumulative list of already-tried workers so loop
# prevention is local to the call chain (no shared state required).
# =============================================================================
async def _pick_alternative_worker(
    exclude_workers: set[str], min_capacity_tokens: int = 500
) -> Optional[str]:
    """Pick the worker with the highest free capacity, excluding any in the
    given set. Capacity = effective_cap_tokens − current_in_flight_tokens.
    Returns None if no eligible worker has at least min_capacity_tokens free.
    """
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(
                "http://metrics-reader:8000/v1/metrics/account-pool-state"
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.warning(f"_pick_alternative_worker: fetch failed: {exc}")
        return None

    candidates: list[tuple[str, int]] = []
    for _account, info in (data.get("accounts") or {}).items():
        worker = info.get("worker")
        if not worker or worker in exclude_workers:
            continue
        if not info.get("available"):
            continue
        if int(info.get("cooldown_remaining_s") or 0) > 0:
            continue
        eff_cap = int(info.get("effective_cap_tokens") or 0)
        in_flight = int(info.get("current_in_flight_tokens") or 0)
        capacity = eff_cap - in_flight
        if capacity > min_capacity_tokens:
            candidates.append((worker, capacity))

    if not candidates:
        return None
    return max(candidates, key=lambda x: x[1])[0]


async def _cross_worker_retry(
    request: Request, self_worker: str
) -> Optional[JSONResponse]:
    """Try the same chat-completions request on a different worker.
    Returns the alternative worker's JSONResponse on attempt, or None when
    no retry is possible (caller falls back to its own error envelope).

    Loop prevention via X-Bridge-Retry-Count (max 2) and X-Bridge-Retry-Excluded
    (cumulative worker exclusion set). Streaming requests are skipped — those
    need buffer-and-replay (separate streaming-truthfulness phase).
    """
    retry_count = int(request.headers.get("x-bridge-retry-count") or "0")
    if retry_count >= 2:
        return None

    excluded_raw = request.headers.get("x-bridge-retry-excluded") or ""
    excluded: set[str] = {w for w in excluded_raw.split(",") if w}
    excluded.add(self_worker)

    cached_body = getattr(request.state, "cached_body_dict", None)
    if not cached_body or cached_body.get("stream"):
        return None  # streaming retries need different machinery

    target = await _pick_alternative_worker(excluded)
    if not target:
        logger.info(
            f"🔄 cross-worker retry: no alternative for {self_worker} "
            f"(excluded={sorted(excluded)})"
        )
        return None

    import httpx
    headers = {"content-type": "application/json"}
    for h in ("authorization", "x-claude-allowed-tools", "x-claude-max-turns",
              "x-claude-file-discovery", "x-priority"):
        if h in request.headers:
            headers[h] = request.headers[h]
    headers["x-bridge-retry-count"] = str(retry_count + 1)
    headers["x-bridge-retry-excluded"] = ",".join(sorted(excluded))

    logger.info(
        f"🔄 cross-worker retry #{retry_count + 1}: {self_worker} → {target} "
        f"(excluded={sorted(excluded)})"
    )

    # Path-dependent timeout: chat is sub-30s, research is 5-40 minutes.
    # Use a generous research timeout so the alternative worker has room to
    # complete a real research run; chat keeps the original tight 30s budget
    # so a hung retry does not itself become the 429 source.
    request_path = str(request.url.path) or "/v1/chat/completions"
    if request_path.endswith("/v1/research"):
        retry_timeout_s = 2400.0  # match SDK 2400s ceiling
    else:
        retry_timeout_s = 30.0

    try:
        async with httpx.AsyncClient(timeout=retry_timeout_s) as client:
            r = await client.post(
                f"http://{target}:8000{request_path}",
                json=cached_body,
                headers=headers,
            )
        # Pass through whatever the alternative worker returned. If it also
        # 429s, the recursive _cross_worker_retry on that worker either found
        # yet another candidate or reached the retry-count cap; either way
        # the answer is now authoritative.
        try:
            content = r.json()
        except Exception:
            content = {"error": {"message": "non-json retry response"}}
        return JSONResponse(status_code=r.status_code, content=content)
    except Exception as exc:
        logger.error(
            f"cross-worker retry HTTP call to {target} failed: {exc}", exc_info=True
        )
        return None


@app.exception_handler(RateLimitError)
async def rate_limit_handler(request: Request, exc: RateLimitError):
    """
    Handle RateLimitError by attempting a cross-worker retry first, then
    falling back to HTTP 429 if no eligible alternative is available.

    Cross-worker retry is bounded to 2 attempts via X-Bridge-Retry-Count.
    """
    worker_id = os.getenv("INSTANCE_NAME", "unknown")
    retry_after = exc.retry_after_seconds

    logger.error(
        f"🚫 RateLimitError on worker {worker_id} - attempting cross-worker retry",
        extra={
            "path": str(request.url),
            "method": request.method,
            "error": str(exc),
            "worker_id": worker_id,
            "retry_after": retry_after,
            "reset_time": exc.reset_time.isoformat() if exc.reset_time else None
        }
    )

    from src.middleware.rolling_metrics import get_rolling_metrics
    get_rolling_metrics().record_rate_limit(worker_id)
    logger.info(f"📊 Recorded 429 for adaptive limiter feedback (worker={worker_id})")

    # Try cross-worker retry on /v1/chat/completions AND /v1/research sync
    # requests before surfacing the 429 to the client. Research uses the same
    # adaptive_limit_dependency that caches request.state.cached_body_dict, so
    # the retry machinery applies symmetrically. Streaming requests are still
    # skipped inside _cross_worker_retry (chat streaming only).
    if request.url.path.endswith(("/v1/chat/completions", "/v1/research")):
        retry_response = await _cross_worker_retry(request, worker_id)
        if retry_response is not None:
            return retry_response

    return JSONResponse(
        status_code=429,
        content={
            "error": {
                # OpenAI-compat
                "message": (
                    f"[Bridge {worker_id}] All Anthropic accounts at weekly limit. "
                    f"Retry after {retry_after}s."
                ),
                "type": "rate_limit_exceeded",
                "code": "429",
                # Bridge discriminators
                "source": "bridge_account",
                "bridge_type": "account_exhausted",
                "bridge_worker": worker_id,
                "retryable": True,
                "retry": True,
                "retry_after_s": retry_after,
                "retry_after_seconds": retry_after,  # legacy alias
                "reset_time": exc.reset_time.isoformat() if exc.reset_time else None,
            }
        },
        headers={
            "Retry-After": str(retry_after),
            "X-Rate-Limit-Reset": exc.reset_time.isoformat() if exc.reset_time else "",
            "X-Worker-Id": worker_id
        }
    )


def handle_file_discovery_header(
    request_headers: dict,
    prompt: str,
    claude_options: dict
) -> None:
    """
    Handle X-Claude-File-Discovery header for both streaming and non-streaming.

    Modifies claude_options in-place to set enable_file_discovery based on:
    - X-Claude-File-Discovery header (values: 'enabled', 'true', '1')
    - Prompt containing '/sc:research' (automatic activation)

    Args:
        request_headers: FastAPI request headers dict
        prompt: User prompt text
        claude_options: SDK options dict (modified in-place)

    Returns:
        None (modifies claude_options in-place)
    """
    x_claude_file_discovery = request_headers.get('X-Claude-File-Discovery', '').strip()

    enable_file_discovery = (
        x_claude_file_discovery.lower() in ('enabled', 'true', '1')
        or '/sc:research' in prompt
    )

    if enable_file_discovery:
        logger.info(
            "✅ File Discovery enabled",
            extra={
                "method": "header" if x_claude_file_discovery else "research_prompt",
                "header_value": x_claude_file_discovery or "N/A"
            }
        )
        claude_options['enable_file_discovery'] = True
    else:
        logger.debug("File Discovery disabled (no header or /sc:research)")
        claude_options['enable_file_discovery'] = False


async def generate_streaming_response(
    request: ChatCompletionRequest,
    request_id: str,
    claude_headers: Optional[Dict[str, Any]] = None,
    fastapi_request: Optional[Request] = None,
    backend_config: Optional[BackendConfig] = None
) -> AsyncGenerator[str, None]:
    """Generate SSE formatted streaming response with automatic disconnect detection.

    Args:
        request: The chat completion request
        request_id: Unique request ID
        claude_headers: Optional Claude-specific headers
        fastapi_request: FastAPI request object for tenant/privacy context
        backend_config: Backend routing configuration (Anthropic vs Bedrock)
    """
    cli_session_for_disconnect = None  # Track CLI session for disconnect detection
    streaming_started = asyncio.Event()  # Signal when streaming starts (prevents race condition)
    stream_start_time = time.time()

    try:
        # VISION ROUTING: Check for images and route to direct Anthropic API
        messages_for_vision = prepare_messages_for_vision(request.messages)

        if has_vision_content(messages_for_vision):
            logger.info("🖼️ Vision streaming request detected")

            try:
                vision_result = await check_and_route_vision(
                    messages=request.messages,
                    model=request.model,
                    max_tokens=request.max_tokens,
                    temperature=request.temperature
                )

                # Stream vision response as SSE chunks
                initial_chunk = ChatCompletionStreamResponse(
                    id=request_id,
                    model=request.model,
                    choices=[StreamChoice(index=0, delta={"role": "assistant", "content": ""}, finish_reason=None)]
                )
                yield f"data: {initial_chunk.model_dump_json()}\n\n"

                content_chunk = ChatCompletionStreamResponse(
                    id=request_id,
                    model=request.model,
                    choices=[StreamChoice(index=0, delta={"content": vision_result.content}, finish_reason=None)]
                )
                yield f"data: {content_chunk.model_dump_json()}\n\n"

                final_chunk = ChatCompletionStreamResponse(
                    id=request_id,
                    model=request.model,
                    choices=[StreamChoice(index=0, delta={}, finish_reason="stop")]
                )
                yield f"data: {final_chunk.model_dump_json()}\n\n"
                yield "data: [DONE]\n\n"

                logger.info("✅ Vision streaming completed")
                return

            except Exception as e:
                logger.error(f"❌ Vision streaming failed: {e}", exc_info=True)
                yield f"data: {json.dumps({'error': {'message': f'Vision analysis failed: {str(e)}', 'type': 'vision_error'}})}\n\n"
                return

        # Process messages with session management
        all_messages, actual_session_id = session_manager.process_messages(
            request.messages, request.session_id
        )

        # Log chat completion start
        logger.info(f"Chat completion: session_id={actual_session_id}, total_messages={len(all_messages)}")

        # Privacy: Anonymize user messages before sending to Claude
        # Uses tenant's privacy mode from request.state (set by TenantMiddleware)
        # OR backend_config's privacy_enabled if backend routing is active
        privacy_client = get_privacy_client()
        anonymization_mapping = {}
        privacy_mode = get_privacy_mode_from_request(fastapi_request) if fastapi_request else "full"

        # Backend routing can override privacy (e.g., Bedrock EU disables privacy automatically)
        privacy_enabled = backend_config.privacy_enabled if backend_config else privacy_client.enabled

        if privacy_enabled:
            messages_for_anon = [
                {'role': m.role, 'content': m.content}
                for m in all_messages
            ]
            anon_messages, anonymization_mapping = await privacy_client.anonymize_messages(
                messages_for_anon,
                privacy_mode=privacy_mode
            )
            # Update all_messages with anonymized content
            all_messages = [
                Message(role=m['role'], content=m['content'])
                for m in anon_messages
            ]
            if anonymization_mapping:
                logger.info(f"Privacy (streaming): Anonymized {len(anonymization_mapping)} PII entities (mode={privacy_mode})")

        # Convert messages to prompt
        prompt, system_prompt = MessageAdapter.messages_to_prompt(all_messages)

        # Log subject (first 80 chars of prompt for monitoring)
        subject = prompt[:80].replace('\n', ' ') if prompt else "(no prompt)"
        logger.info(f"Chat subject: {subject}...")
        
        # Filter content for unsupported features
        prompt = MessageAdapter.filter_content(prompt)
        if system_prompt:
            system_prompt = MessageAdapter.filter_content(system_prompt)
        
        # Get Claude Code SDK options from request
        claude_options = request.to_claude_options()
        
        # Merge with Claude-specific headers if provided
        if claude_headers:
            claude_options.update(claude_headers)
        
        # Validate model
        if claude_options.get('model'):
            ParameterValidator.validate_model(claude_options['model'])

        # Handle X-Claude-Allowed-Tools header (for /sc:research support)
        request_headers = fastapi_request.headers if fastapi_request else {}
        x_claude_allowed_tools = request_headers.get('X-Claude-Allowed-Tools', '').strip()
        x_claude_max_turns = request_headers.get('X-Claude-Max-Turns', '').strip()
        x_claude_file_discovery = request_headers.get('X-Claude-File-Discovery', '').strip()

        if x_claude_allowed_tools:
            # Special case: '*' means "allow all tools" (don't set allowed_tools, use SDK default)
            if x_claude_allowed_tools == '*':
                logger.info("X-Claude-Allowed-Tools='*' → Using SDK default (all tools allowed)")
            else:
                # Parse allowed tools from header
                allowed_tools_list = [t.strip() for t in x_claude_allowed_tools.split(',') if t.strip()]
                claude_options['allowed_tools'] = allowed_tools_list
                logger.info(f"X-Claude-Allowed-Tools: {allowed_tools_list}")

        # Handle tools - disabled by default for OpenAI compatibility
        if not request.enable_tools and not x_claude_allowed_tools:
            # Set disallowed_tools to all available tools to disable them
            disallowed_tools = ['Task', 'Bash', 'Glob', 'Grep', 'LS', 'exit_plan_mode',
                                'Read', 'Edit', 'MultiEdit', 'Write', 'NotebookRead',
                                'NotebookEdit', 'WebFetch', 'TodoRead', 'TodoWrite', 'WebSearch']
            claude_options['disallowed_tools'] = disallowed_tools
            claude_options['max_turns'] = 1  # Single turn for Q&A (can be overridden by X-Claude-Max-Turns header)
            logger.info("Tools disabled (default behavior for OpenAI compatibility)")
        else:
            logger.info(f"Tools enabled by user request (enable_tools={request.enable_tools}, X-Claude-Allowed-Tools={bool(x_claude_allowed_tools)})")

        # X-Claude-Max-Turns header MUST be processed AFTER enable_tools logic to allow override
        if x_claude_max_turns:
            try:
                claude_options['max_turns'] = int(x_claude_max_turns)
                logger.info(f"X-Claude-Max-Turns: {x_claude_max_turns}")
            except ValueError:
                logger.warning(f"Invalid X-Claude-Max-Turns value: {x_claude_max_turns}")

        # Handle X-Claude-File-Discovery header (opt-in file discovery)
        handle_file_discovery_header(request_headers, prompt, claude_options)

        # Run Claude Code
        chunks_buffer = []
        role_sent = False  # Track if we've sent the initial role chunk
        content_sent = False  # Track if we've sent any content
        deanon_buffer = ""  # Buffer for streaming de-anonymization (handles split placeholders)

        # Background task for disconnect detection
        async def monitor_client_disconnect():
            """Monitor client connection and cancel CLI session on disconnect.

            Race Condition Prevention:
                FastAPI's StreamingResponse returns immediately before generator runs,
                causing is_disconnected() to return True prematurely. This monitor waits
                for the first chunk to be sent before checking for real disconnects.
            """
            if not fastapi_request:
                logger.debug("🔍 Disconnect monitor: No fastapi_request, skipping")
                return

            try:
                logger.debug("🔍 Disconnect monitor: Task started, waiting for streaming to begin...")

                # Wait with timeout (max 5s for first chunk)
                try:
                    await asyncio.wait_for(streaming_started.wait(), timeout=5.0)
                    logger.debug("✅ Disconnect monitor: Streaming started, now monitoring for disconnect")
                except asyncio.TimeoutError:
                    logger.warning("⏱️ TIMEOUT: Streaming didn't start within 5s")
                    logger.warning("   Timeout Type: Streaming Start Signal (Disconnect Monitor)")
                    logger.warning("   Timeout Duration: 5.0s")
                    logger.warning("   Likely Cause: Client disconnected early or Claude Code SDK initialization slow")
                    logger.warning("   Impact: Disconnect monitor aborting - client disconnect won't be detected")
                    return

                # Get the CLI session ID from the enclosing scope
                if not cli_session_for_disconnect:
                    logger.warning("⚠️ Disconnect monitor: Streaming started but no CLI session found")
                    return

                # NOW we can safely monitor for disconnects
                while True:
                    if await fastapi_request.is_disconnected():
                        logger.warning(f"🔌 Client disconnected! Auto-cancelling CLI session {cli_session_for_disconnect['cli_session_id']}")
                        from src.cli_session_manager import cli_session_manager
                        cli_session_manager.cancel_session(cli_session_for_disconnect['cli_session_id'])
                        logger.info(f"🚫 CLI session {cli_session_for_disconnect['cli_session_id']} cancelled due to client disconnect")
                        break
                    await asyncio.sleep(0.5)

            except Exception as e:
                logger.error(f"❌ Error in disconnect monitor: {e}", exc_info=True)

        # Start disconnect monitor if we have a request object
        monitor_task = None
        if fastapi_request:
            monitor_task = asyncio.create_task(monitor_client_disconnect())

        metadata_chunk = None  # Store file metadata for end of stream

        # Phase 1 of streaming-truthfulness fix: catch mid-stream SDK crashes
        # (e.g. claude_code_sdk Command-failed exit-1 from compaction events)
        # so the client sees an explicit error event instead of the previous
        # fake finish_reason="stop" that hid truncation. Phase 2 will add
        # buffer-and-replay with internal retry so most crashes never surface.
        sdk_stream_error: Optional[Exception] = None

        try:
            async for chunk in claude_cli.run_completion(
                prompt=prompt,
                system_prompt=system_prompt,
                model=claude_options.get('model'),
                max_turns=claude_options.get('max_turns', 10),
                allowed_tools=claude_options.get('allowed_tools'),
                disallowed_tools=claude_options.get('disallowed_tools'),
                stream=True,
                enable_file_discovery=claude_options.get('enable_file_discovery', False),
                backend_env_vars=backend_config.env_vars if backend_config else None
            ):
                # Capture metadata chunk (don't stream it in SSE)
                if isinstance(chunk, dict) and chunk.get("type") == "x_claude_metadata":
                    metadata_chunk = chunk
                    logger.info("📦 Captured file metadata from CLI (will send at end of stream)")
                    continue  # Don't add to chunks_buffer, don't stream

                # On first chunk, get the CLI session for disconnect monitoring
                if not cli_session_for_disconnect:
                    from src.cli_session_manager import cli_session_manager
                    cli_sessions = cli_session_manager.list_sessions(status_filter="running")
                    if cli_sessions:
                        # Get the most recent running session (just created by claude_cli.run_completion)
                        cli_session_for_disconnect = cli_sessions[-1]
                        logger.info(f"🔗 Monitoring CLI session {cli_session_for_disconnect['cli_session_id']} for client disconnect")

                        # SIGNAL: Streaming has started, monitor can now check disconnects
                        streaming_started.set()
                        logger.debug(f"✅ Streaming started signal sent, CLI session: {cli_session_for_disconnect['cli_session_id']}")

                chunks_buffer.append(chunk)

                # Check if we have an assistant message
                # Handle both old format (type/message structure) and new format (direct content)
                content = None
                if chunk.get("type") == "assistant" and "message" in chunk:
                    # Old format: {"type": "assistant", "message": {"content": [...]}}
                    message = chunk["message"]
                    if isinstance(message, dict) and "content" in message:
                        content = message["content"]
                elif "content" in chunk and isinstance(chunk["content"], list):
                    # New format: {"content": [TextBlock(...)]}  (converted AssistantMessage)
                    content = chunk["content"]
            
                if content is not None:
                    # Send initial role chunk if we haven't already
                    if not role_sent:
                        initial_chunk = ChatCompletionStreamResponse(
                            id=request_id,
                            model=request.model,
                            choices=[StreamChoice(
                                index=0,
                                delta={"role": "assistant", "content": ""},
                                finish_reason=None
                            )]
                        )
                        yield f"data: {initial_chunk.model_dump_json()}\n\n"
                        role_sent = True
                
                    # Handle content blocks
                    if isinstance(content, list):
                        for block in content:
                            # Handle TextBlock objects from Claude Code SDK
                            if hasattr(block, 'text'):
                                raw_text = block.text
                            # Handle dictionary format for backward compatibility
                            elif isinstance(block, dict) and block.get("type") == "text":
                                raw_text = block.get("text", "")
                            else:
                                continue
                            
                            # Filter out tool usage and thinking blocks
                            filtered_text = MessageAdapter.filter_content(raw_text)

                            # Privacy: De-anonymize chunk with buffering (handles split placeholders)
                            if anonymization_mapping and filtered_text:
                                filtered_text, deanon_buffer = privacy_client.deanonymize_streaming_chunk(
                                    filtered_text, deanon_buffer, anonymization_mapping
                                )

                            if filtered_text and not filtered_text.isspace():
                                # Create streaming chunk
                                stream_chunk = ChatCompletionStreamResponse(
                                    id=request_id,
                                    model=request.model,
                                    choices=[StreamChoice(
                                        index=0,
                                        delta={"content": filtered_text},
                                        finish_reason=None
                                    )]
                                )
                            
                                yield f"data: {stream_chunk.model_dump_json()}\n\n"
                                content_sent = True
                
                    elif isinstance(content, str):
                        # Filter out tool usage and thinking blocks
                        filtered_content = MessageAdapter.filter_content(content)

                        # Privacy: De-anonymize chunk with buffering (handles split placeholders)
                        if anonymization_mapping and filtered_content:
                            filtered_content, deanon_buffer = privacy_client.deanonymize_streaming_chunk(
                                filtered_content, deanon_buffer, anonymization_mapping
                            )

                        if filtered_content and not filtered_content.isspace():
                            # Create streaming chunk
                            stream_chunk = ChatCompletionStreamResponse(
                                id=request_id,
                                model=request.model,
                                choices=[StreamChoice(
                                    index=0,
                                    delta={"content": filtered_content},
                                    finish_reason=None
                                )]
                            )
                        
                            yield f"data: {stream_chunk.model_dump_json()}\n\n"
                            content_sent = True
        except Exception as e:
            sdk_stream_error = e
            logger.error(
                f"❌ Streaming SDK crash mid-stream: {type(e).__name__}: {e}",
                exc_info=True,
            )


        # Flush any remaining de-anonymization buffer at end of stream
        if anonymization_mapping and deanon_buffer:
            flushed_content = privacy_client.flush_streaming_buffer(deanon_buffer, anonymization_mapping)
            if flushed_content and not flushed_content.isspace():
                flush_chunk = ChatCompletionStreamResponse(
                    id=request_id,
                    model=request.model,
                    choices=[StreamChoice(
                        index=0,
                        delta={"content": flushed_content},
                        finish_reason=None
                    )]
                )
                yield f"data: {flush_chunk.model_dump_json()}\n\n"
                content_sent = True

        # Handle case where no role was sent (send at least role chunk)
        if not role_sent:
            # Send role chunk with empty content if we never got any assistant messages
            initial_chunk = ChatCompletionStreamResponse(
                id=request_id,
                model=request.model,
                choices=[StreamChoice(
                    index=0,
                    delta={"role": "assistant", "content": ""},
                    finish_reason=None
                )]
            )
            yield f"data: {initial_chunk.model_dump_json()}\n\n"
            role_sent = True
        
        # If we sent role but no content, send a minimal response
        if role_sent and not content_sent:
            fallback_chunk = ChatCompletionStreamResponse(
                id=request_id,
                model=request.model,
                choices=[StreamChoice(
                    index=0,
                    delta={"content": "I'm unable to provide a response at the moment."},
                    finish_reason=None
                )]
            )
            yield f"data: {fallback_chunk.model_dump_json()}\n\n"
        
        # Extract assistant response from all chunks for session storage
        if actual_session_id and chunks_buffer:
            assistant_content = claude_cli.parse_claude_message(chunks_buffer)
            if assistant_content:
                assistant_message = Message(role="assistant", content=assistant_content)
                session_manager.add_assistant_response(actual_session_id, assistant_message)
        
        # Send final chunk. On SDK crash: emit an explicit SSE `event: error`
        # envelope and skip the [DONE] sentinel — OpenAI-compatible clients
        # see the error event + missing [DONE] as failure signal instead of
        # misreading a half-truncated stream as complete.
        if sdk_stream_error is None:
            final_chunk = ChatCompletionStreamResponse(
                id=request_id,
                model=request.model,
                choices=[StreamChoice(
                    index=0,
                    delta={},
                    finish_reason="stop"
                )]
            )
            yield f"data: {final_chunk.model_dump_json()}\n\n"
        else:
            err_type = type(sdk_stream_error).__name__
            err_msg  = str(sdk_stream_error)[:300]
            error_payload = {
                "error": {
                    "message": f"[Bridge] SDK stream interrupted: {err_type}: {err_msg}",
                    "type": "sdk_stream_failure",
                    "code": "sdk_crash",
                    "source": "bridge_internal",
                    "retryable": True,
                }
            }
            yield f"event: error\ndata: {json.dumps(error_payload)}\n\n"
            # Skip [DONE] sentinel below — return early after metadata.

        # Send file metadata as separate SSE event (if available)
        if metadata_chunk:
            metadata_event = {
                "files_created": metadata_chunk.get("files_created", []),
                "session_tracking": metadata_chunk.get("session_tracking", {}),
                "discovery_status": metadata_chunk.get("discovery_status", "unknown")
            }

            # Include discovery_method if present
            if "discovery_method" in metadata_chunk:
                metadata_event["discovery_method"] = metadata_chunk["discovery_method"]

            # Include discovery_details if present (for no_files_found case)
            if "discovery_details" in metadata_chunk:
                metadata_event["discovery_details"] = metadata_chunk["discovery_details"]

            logger.info(
                f"📦 Sending file metadata in stream",
                extra={
                    "request_id": request_id,
                    "files_count": len(metadata_chunk.get("files_created", [])),
                    "discovery_status": metadata_chunk.get("discovery_status", "unknown")
                }
            )

            # Send as custom SSE event
            yield f"event: x_claude_metadata\n"
            yield f"data: {json.dumps(metadata_event)}\n\n"

        # [DONE] sentinel only on clean completion — its absence is the
        # "stream did not finish successfully" signal for the client.
        if sdk_stream_error is None:
            yield "data: [DONE]\n\n"

        # =====================================================================
        # USAGE TRACKING for streaming (estimated tokens)
        # Without this, streaming calls show 0 tokens in Bridge Monitor.
        # =====================================================================
        try:
            stream_duration = time.time() - stream_start_time

            # Estimate tokens from buffered chunks
            assistant_text = claude_cli.parse_claude_message(chunks_buffer) if chunks_buffer else ""
            est_prompt_tokens = MessageAdapter.estimate_tokens(prompt) if prompt else 0
            est_completion_tokens = MessageAdapter.estimate_tokens(assistant_text) if assistant_text else 0

            # Track with tenant (if available via request state)
            tenant = get_tenant_from_request(fastapi_request) if fastapi_request else None
            if tenant and (est_prompt_tokens > 0 or est_completion_tokens > 0):
                from src.tenant import track_request_usage
                attribution = extract_attribution_context(fastapi_request) if fastapi_request else {}
                await track_request_usage(
                    tenant=tenant,
                    model=request.model,
                    input_tokens=est_prompt_tokens,
                    output_tokens=est_completion_tokens,
                    endpoint="/v1/chat/completions/stream",
                    latency_ms=int(stream_duration * 1000),
                    status="success",
                    **attribution
                )
                logger.info(f"📊 Streaming usage tracked: {est_prompt_tokens}+{est_completion_tokens} tokens")

            # Log to prompt metrics (always, even without tenant)
            from src.middleware.prompt_metrics import prompt_metrics_collector
            if fastapi_request:
                attr = extract_attribution_context(fastapi_request)
                prompt_metrics_collector.record(
                    app_id=attr.get("app_id") or "unknown",
                    agent_id=attr.get("agent_id") or "unknown",
                    workflow_id=attr.get("workflow_id"),
                    model=request.model,
                    input_tokens=est_prompt_tokens,
                    output_tokens=est_completion_tokens,
                    duration_ms=int(stream_duration * 1000),
                    status="success",
                    user_id=attr.get("user_id"),
                    session_id=attr.get("session_id"),
                )
        except Exception as track_err:
            logger.warning(f"⚠️ Streaming usage tracking failed (non-fatal): {track_err}")

    except WorkerUnavailableError:
        # Re-raise to trigger HTTP 503 and Nginx failover
        raise

    except Exception as e:
        logger.error(f"Streaming error: {e}")
        error_chunk = {
            "error": {
                "message": str(e),
                "type": "streaming_error"
            }
        }
        yield f"data: {json.dumps(error_chunk)}\n\n"

    finally:
        # Cleanup disconnect monitor task
        if monitor_task and not monitor_task.done():
            logger.debug("🧹 Cleaning up disconnect monitor task")
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                logger.debug("✅ Disconnect monitor task cancelled successfully")
                pass
        elif monitor_task:
            logger.debug("✅ Disconnect monitor task already completed")

        # Rolling-metrics completion for streaming. Without this, streaming
        # requests would never decrement in_flight/in_flight_input_tokens —
        # poisoning the AdaptiveLoadLimiter's view of capacity.
        try:
            if fastapi_request and getattr(fastapi_request.state, "arrival_recorded", False):
                from src.middleware.rolling_metrics import get_rolling_metrics
                _stream_dur_ms = int((time.time() - stream_start_time) * 1000)
                # Best-effort token counts; if streaming-success block computed
                # them, use those. Otherwise fall back to estimate of 0.
                _in_tok = int(locals().get("est_prompt_tokens", 0) or 0)
                _out_tok = int(locals().get("est_completion_tokens", 0) or 0)
                get_rolling_metrics().record_completion(
                    worker=os.getenv("INSTANCE_NAME", "unknown"),
                    status="success" if _in_tok or _out_tok else "error",
                    duration_ms=_stream_dur_ms,
                    input_tokens=_in_tok,
                    output_tokens=_out_tok,
                    est_input_tokens_at_arrival=int(
                        getattr(fastapi_request.state, "adaptive_est_tokens", 0) or 0
                    ),
                )
                fastapi_request.state.arrival_recorded = False
        except Exception as _e:
            logger.debug(f"rolling_metrics completion (stream finally) failed: {_e}")


# =============================================================================
# ATTRIBUTION HELPER
# =============================================================================

# Known app IDs for attribution enforcement.
# Calls without X-App-ID (or X-Client-ID fallback) are warned, not rejected,
# during the rollout phase. Set ENFORCE_ATTRIBUTION=strict to reject.
KNOWN_APP_IDS = {
    "engelmann", "werking-report", "werking-energy", "werking-safety",
    "werking-noise", "cui", "platform", "acro-community",
}


def enforce_attribution(request: Request) -> dict:
    """
    Extract and enforce attribution headers.

    Phase 1 (default): Log warnings for missing attribution, allow request.
    Phase 2 (ENFORCE_ATTRIBUTION=strict): Reject requests without X-App-ID.

    Returns the attribution context dict.
    Raises HTTPException(400) in strict mode if X-App-ID is missing.
    """
    attribution = extract_attribution_context(request)
    enforce_mode = os.environ.get("ENFORCE_ATTRIBUTION", "warn")

    missing = []
    if not attribution.get("app_id"):
        missing.append("X-App-ID")
    if not attribution.get("agent_id"):
        missing.append("X-Agent-ID")

    if missing:
        client_id = request.headers.get("x-client-id") or request.headers.get("X-Client-ID") or "unknown"
        auth_header = request.headers.get("authorization") or ""
        bearer_hint = auth_header.split(" ")[-1][:16] + "..." if auth_header else "none"

        logger.warning(
            f"⚠️ ATTRIBUTION MISSING: {', '.join(missing)} | "
            f"X-Client-ID={client_id} | Bearer={bearer_hint} | "
            f"Resolved: app={attribution.get('app_id')}, agent={attribution.get('agent_id')}"
        )

        if enforce_mode == "strict" and "X-App-ID" in missing:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": "Missing required attribution header: X-App-ID",
                        "type": "attribution_error",
                        "code": "missing_app_id",
                        "missing_headers": missing,
                        "hint": "Add 'X-App-ID: <app-name>' header to your request. "
                                f"Known apps: {', '.join(sorted(KNOWN_APP_IDS))}",
                    }
                }
            )

    return attribution


def extract_attribution_context(request: Request) -> dict:
    """
    Extract multi-dimension attribution headers from request.

    Returns dict with all attribution dimensions:
    - user_id: User identifier (X-User-ID)
    - app_id: Application identifier (X-App-ID)
    - agent_id: Autonomous agent identifier (X-Agent-ID)
    - session_id: Session UUID (X-Session-ID)
    - workflow_id: Workflow type (X-Workflow-ID)
    - job_id: Job/run UUID (X-Job-ID)

    All values are optional (None if not provided).
    Fallback: Parses X-Client-ID (e.g., "werking-energy/wizard/analyze-smart")
    into app_id and agent_id if the dedicated headers are missing.
    """
    app_id = get_app_id_from_request(request)
    agent_id = get_agent_id_from_request(request)

    # Fallback: parse X-Client-ID if app_id or agent_id missing
    if not app_id or not agent_id:
        client_id = request.headers.get("x-client-id") or request.headers.get("X-Client-ID")
        if client_id:
            # Parse patterns:
            #   "werking-energy/wizard/generate-questions" → app=werking-energy, agent=generate-questions
            #   "werking-energy/api/llm-client"            → app=werking-energy, agent=llm-client
            #   "workflow/werking-energy/vision-handler/s1" → app=werking-energy, agent=vision-handler
            #   "phase10-standalone/context"                → app=phase10-standalone, agent=context
            parts = [p for p in client_id.strip().split("/") if p]
            # Skip "workflow" prefix — it's just a namespace
            if parts and parts[0] == "workflow":
                parts = parts[1:]
            if len(parts) >= 1 and not app_id:
                app_id = parts[0]  # e.g., "werking-energy"
            if len(parts) >= 3 and not agent_id:
                # 3+ parts: use middle part as category context
                # "werking-energy/wizard/generate-questions" → agent = "generate-questions"
                # "werking-energy/pipeline/research/ctx"     → agent = "research"
                agent_id = parts[-1] if len(parts) == 3 else parts[2]
            elif len(parts) >= 2 and not agent_id:
                agent_id = parts[-1]

    return {
        "user_id": get_user_id_from_request(request),
        "app_id": app_id,
        "agent_id": agent_id,
        "session_id": get_session_id_from_request(request),
        "workflow_id": get_workflow_id_from_request(request),
        "job_id": get_job_id_from_request(request),
    }


# =============================================================================
# API ENDPOINTS
# =============================================================================

@app.post("/v1/chat/completions")
@rate_limit_endpoint("chat")
def enforce_tools_policy(enable_tools: bool, x_claude_allowed_tools: str) -> bool:
    """Bridge contract: tools are research-only.

    chat_completions clients sending enable_tools=true without an
    X-Claude-Allowed-Tools header get silently dropped to false.
    Research opt-in MUST signal via the header (parsed downstream).
    """
    if enable_tools and not x_claude_allowed_tools:
        return False
    return enable_tools


async def chat_completions(
    request_body: ChatCompletionRequest,
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    _adaptive=Depends(adaptive_limit_dependency)
):
    """OpenAI-compatible chat completions endpoint."""
    import time
    from fastapi.responses import JSONResponse
    start_time = time.time()

    # Force tools-policy: tools are research-only (X-Claude-Allowed-Tools header).
    # Drop accidental enable_tools=true from any client unless they signal research.
    _hdr_allowed = (request.headers.get("X-Claude-Allowed-Tools") or "").strip()
    _orig_enable = request_body.enable_tools
    request_body.enable_tools = enforce_tools_policy(_orig_enable, _hdr_allowed)
    if _orig_enable != request_body.enable_tools:
        logger.warning(
            "Force-disabled enable_tools=true (no X-Claude-Allowed-Tools header). "
            "Bridge policy: tools are research-only."
        )

    # Check FastAPI API key if configured
    await verify_api_key(request, credentials)

    # Validate Claude Code authentication
    auth_valid, auth_info = validate_claude_code_auth()

    if not auth_valid:
        # Log authentication failure event
        EventLogger.log_authentication(
            success=False,
            error="Claude Code authentication failed",
            metadata={"errors": auth_info.get('errors', [])}
        )

        # Auth failure on this worker = config issue (credentials missing/expired).
        # Contract: worker returns 500 config_error (non-retryable on same worker)
        # so operators see it loud. Nginx failover to a healthy worker still works
        # because this is raised before we hit the chat path.
        detail = (
            f"Claude Code authentication failed "
            f"(method={auth_info.get('method', 'none')}, "
            f"errors={auth_info.get('errors', [])}). "
            f"See /v1/auth/status."
        )
        raise BridgeError(config_error(detail=detail))
    
    try:
        request_id = f"chatcmpl-{os.urandom(8).hex()}"

        # Rolling-metrics arrival event (per-worker rate tracking, in-memory).
        # We pass the same token estimate the AdaptiveLoadLimiter used so that
        # in-flight token accounting matches the gating decision exactly.
        _arrival_est_tokens = int(getattr(request.state, "adaptive_est_tokens", 0) or 0)
        request.state.arrival_recorded = False
        try:
            from src.middleware.rolling_metrics import get_rolling_metrics
            get_rolling_metrics().record_arrival(
                os.getenv("INSTANCE_NAME", "unknown"),
                est_input_tokens=_arrival_est_tokens,
            )
            request.state.arrival_recorded = True
        except Exception as _e:
            logger.debug(f"rolling_metrics arrival hook failed: {_e}")

        # =======================================================================
        # RATE LIMIT PRE-CHECK: Soft routing for new requests.
        # If this worker has a penalty, return 503 so NGINX routes to next worker.
        # Hard limits always reject. Soft penalties reject unless almost expired.
        # Safety: in-progress tasks NEVER abort — this only affects NEW requests.
        # =======================================================================
        worker_id = os.getenv("INSTANCE_NAME", "unknown")
        from src.claude_cli import rate_limit_tracker
        if rate_limit_tracker.should_reject_new_request(worker_id):
            retry_after = rate_limit_tracker.get_retry_after(worker_id) or 0
            limit_type = "HARD" if rate_limit_tracker.is_hard_limited(worker_id) else "soft"

            # =================================================================
            # AUTOBAHN-AUFFAHRT QUEUE: For SOFT penalties with bounded wait, hold
            # the request in-process until cooldown expires, then fall through to
            # the normal handling path. Apps see latency, not a 429 → no token-
            # waste from app-side retry/replan cycles.
            #
            # HARD limits (real Anthropic ceiling) reject immediately — no point
            # waiting if the account is hard-locked for minutes. Soft penalties
            # are short (max 15s post phantom-filter); we wait for those.
            # =================================================================
            MAX_QUEUE_WAIT_S = 30  # cap to avoid request-timeout cascade
            if limit_type == "soft" and 0 < retry_after <= MAX_QUEUE_WAIT_S:
                logger.info(
                    f"⏳ Worker {worker_id} soft-penalty {retry_after}s — "
                    f"queueing request (autobahn auffahrt)"
                )
                wait_start = time.time()
                # Sleep in 1s slices so we honor request cancellation
                while rate_limit_tracker.should_reject_new_request(worker_id):
                    if time.time() - wait_start >= retry_after + 1:
                        break
                    await asyncio.sleep(1)
                # Re-check after wait — if still penalised (e.g. fresh event landed),
                # fall through to 429 below
                if not rate_limit_tracker.should_reject_new_request(worker_id):
                    logger.info(
                        f"✅ Worker {worker_id} cooldown expired after "
                        f"{time.time() - wait_start:.1f}s — proceeding"
                    )
                    # Fall through to normal request handling — do NOT return
                else:
                    retry_after = rate_limit_tracker.get_retry_after(worker_id) or 30

            # Either: hard limit, soft penalty > MAX_QUEUE_WAIT_S, or queue timed out
            if rate_limit_tracker.should_reject_new_request(worker_id):
                logger.info(
                    f"⏳ Worker {worker_id} {limit_type} penalty — rejecting "
                    f"(NGINX failover), retry in {retry_after}s"
                )
                return JSONResponse(
                    status_code=429,
                    content={"error": {
                        "message": f"[Bridge {worker_id}] Worker rate-limited ({limit_type})",
                        "type": "api_error",
                        "code": "429",
                        "source": "bridge_account",
                        "bridge_type": "worker_unavailable",
                        "reason": "worker_account_rate_limited",
                        "bridge_worker": worker_id,
                        "retryable": True,
                        "retry_after_s": retry_after,
                    }},
                    headers={"Retry-After": str(retry_after or 30)}
                )

        # =======================================================================
        # BUDGET ENFORCEMENT: Check tenant limits before processing
        # =======================================================================
        tenant = get_tenant_from_request(request)
        if tenant:
            from src.tenant import check_budget
            budget_result = await check_budget(tenant)
            if not budget_result.allowed:
                logger.warning(f"Budget exceeded for {tenant.tenant_slug}: {budget_result.reason}")
                raise HTTPException(
                    status_code=402,
                    detail={
                        "error": {
                            "message": budget_result.reason,
                            "type": "budget_exceeded",
                            "code": "payment_required",
                            "billing_mode": budget_result.billing_mode,
                            "current_tokens": budget_result.current_tokens,
                            "token_limit": budget_result.token_limit,
                            "usage_percent": budget_result.token_usage_percent,
                        }
                    }
                )

        # =======================================================================
        # ATTRIBUTION ENFORCEMENT: Ensure callers identify themselves
        # =======================================================================
        attribution = enforce_attribution(request)

        # MODEL RESOLUTION: Fuzzy match model names (e.g., "sonnet" -> "claude-sonnet-4-5-20250929")
        original_model = request_body.model
        resolved_model, resolution_msg = resolve_model(original_model)

        if resolved_model is None:
            # Model not found - return clear error
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": f"Model '{original_model}' not supported.",
                        "type": "invalid_request_error",
                        "param": "model",
                        "code": "model_not_found",
                        "hint": f"Use 'sonnet', 'haiku', 'opus' for latest, or exact IDs: {', '.join(get_all_model_ids())}"
                    }
                }
            )

        # Update request with resolved model
        if resolved_model != original_model:
            logger.info(f"Model resolved: '{original_model}' -> '{resolved_model}'")
            request_body.model = resolved_model

        # BACKEND ROUTING: Resolve backend configuration (Anthropic / Bedrock / OpenAI-Compatible)
        backend_config = None
        try:
            backend_config = resolve_backend_config(
                backend=request_body.backend or BackendType.ANTHROPIC,
                model=resolved_model,
                privacy=request_body.privacy or PrivacyMode.AUTO,
                bedrock_region=request_body.bedrock_region,
                provider_tier=request_body.provider_tier,
            )
        except RuntimeError as e:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": str(e),
                        "type": "configuration_error",
                        "code": "provider_not_configured",
                        "hint": "Contact server administrator to configure the requested provider"
                    }
                }
            )

        # =======================================================================
        # BEDROCK ROUTING: Direct boto3 call, bypass Claude Code SDK
        # =======================================================================
        if backend_config and backend_config.backend == BackendType.BEDROCK:
            from src.bedrock_service import call_bedrock, stream_bedrock
            logger.info(f"🔀 Routing to Bedrock (region={backend_config.region})")

            if request_body.stream:
                return StreamingResponse(
                    stream_bedrock(request_body, backend_config.region),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Backend": "bedrock",
                        "X-Backend-Region": backend_config.region or "eu-central-1",
                    }
                )
            else:
                response = await call_bedrock(request_body, backend_config.region)
                duration = time.time() - start_time
                logger.info(f"✅ Bedrock request completed in {duration:.2f}s")

                # Track usage for Bedrock (non-streaming)
                if tenant and response.usage:
                    from src.tenant import track_request_usage
                    attribution = extract_attribution_context(request)
                    await track_request_usage(
                        tenant=tenant,
                        model=resolved_model,
                        input_tokens=response.usage.prompt_tokens,
                        output_tokens=response.usage.completion_tokens,
                        endpoint="/v1/chat/completions",
                        latency_ms=int(duration * 1000),
                        status="success",
                        **attribution
                    )

                return response

        # =======================================================================
        # OPENAI-COMPATIBLE ROUTING: Generic httpx call (IONOS, Mistral, etc.)
        # =======================================================================
        if backend_config and backend_config.backend == BackendType.OPENAI_COMPATIBLE:
            from src.providers.openai_compatible import call_openai_compatible, stream_openai_compatible
            tier = backend_config.provider_tier or "unknown"
            logger.info(f"🔀 Routing to OpenAI-compatible provider (tier={tier})")

            if request_body.stream:
                return StreamingResponse(
                    stream_openai_compatible(
                        request_body,
                        backend_config.provider_base_url,
                        backend_config.provider_api_key,
                        model_override=backend_config.provider_model,
                    ),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Backend": "openai_compatible",
                        "X-Provider-Tier": tier,
                    }
                )
            else:
                response_data = await call_openai_compatible(
                    request_body,
                    backend_config.provider_base_url,
                    backend_config.provider_api_key,
                    model_override=backend_config.provider_model,
                )
                duration = time.time() - start_time
                logger.info(f"✅ OpenAI-compatible request completed in {duration:.2f}s (tier={tier})")

                # Track usage
                usage = response_data.get("usage", {})
                if tenant and usage:
                    from src.tenant import track_request_usage
                    attribution = extract_attribution_context(request)
                    await track_request_usage(
                        tenant=tenant,
                        model=backend_config.provider_model or resolved_model,
                        input_tokens=usage.get("prompt_tokens", 0),
                        output_tokens=usage.get("completion_tokens", 0),
                        endpoint="/v1/chat/completions",
                        latency_ms=int(duration * 1000),
                        status="success",
                        **attribution
                    )

                return response_data

        # =======================================================================
        # GEMINI CLI ROUTING: Subprocess call to `gemini` binary
        # =======================================================================
        if backend_config and backend_config.backend == BackendType.GEMINI_CLI:
            from src.gemini_cli import call_gemini_cli
            tier = backend_config.provider_tier or "gemini-flash"
            gemini_model = backend_config.provider_model or "gemini-2.5-flash"
            logger.info(f"🔀 Routing to Gemini CLI (tier={tier}, model={gemini_model})")

            # Extract prompt from messages (last user message)
            user_messages = [m for m in request_body.messages if m.role == "user"]
            system_messages = [m for m in request_body.messages if m.role == "system"]

            prompt_text = user_messages[-1].content if user_messages else ""
            system_text = system_messages[0].content if system_messages else None

            if isinstance(prompt_text, list):
                # Multimodal: extract text parts only
                prompt_text = "\n".join(
                    p.text for p in prompt_text if hasattr(p, "text")
                )
            if system_text and isinstance(system_text, list):
                system_text = "\n".join(
                    p.text for p in system_text if hasattr(p, "text")
                )

            response_data = await call_gemini_cli(
                prompt=prompt_text,
                model=gemini_model,
                system_prompt=system_text,
            )

            duration = time.time() - start_time
            logger.info(f"✅ Gemini CLI request completed in {duration:.2f}s (tier={tier})")

            # Track usage
            usage = response_data.get("usage", {})
            if tenant and usage:
                from src.tenant import track_request_usage
                attribution = extract_attribution_context(request)
                await track_request_usage(
                    tenant=tenant,
                    model=gemini_model,
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                    endpoint="/v1/chat/completions",
                    latency_ms=int(duration * 1000),
                    status="success",
                    **attribution
                )

            return response_data

        # =======================================================================
        # ANTHROPIC ROUTING: Continue with Claude Code SDK (default)
        # =======================================================================

        # Extract Claude-specific parameters from headers
        claude_headers = ParameterValidator.extract_claude_headers(dict(request.headers))

        # Log compatibility info
        if DEBUG_MODE or VERBOSE:
            compatibility_report = CompatibilityReporter.generate_compatibility_report(request_body)
            logger.debug(f"Compatibility report: {compatibility_report}")
        
        if request_body.stream:
            # Worker instance for multi-worker deployments
            worker_instance = os.getenv("INSTANCE_NAME", "unknown")

            # Return streaming response with worker info header
            return StreamingResponse(
                generate_streaming_response(
                    request_body, request_id, claude_headers,
                    fastapi_request=request,
                    backend_config=backend_config
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Worker-Instance": worker_instance,
                    "X-Backend": backend_config.backend.value if backend_config else "anthropic",
                }
            )
        else:
            # Non-streaming response

            # VISION ROUTING: Check for images and route to direct Anthropic API
            try:
                vision_result = await check_and_route_vision(
                    messages=request_body.messages,
                    model=request_body.model,
                    max_tokens=request_body.max_tokens,
                    temperature=request_body.temperature
                )

                if vision_result:
                    # Count images in messages for AI usage tracking
                    image_count = sum(
                        1 for msg in request_body.messages
                        if hasattr(msg, 'has_images') and msg.has_images()
                    )

                    # Build OpenAI-compatible response from vision result
                    response = ChatCompletionResponse(
                        id=request_id,
                        model=vision_result.model,
                        choices=[Choice(
                            index=0,
                            message=Message(role="assistant", content=vision_result.content),
                            finish_reason="stop"
                        )],
                        usage=Usage(
                            prompt_tokens=vision_result.usage["prompt_tokens"],
                            completion_tokens=vision_result.usage["completion_tokens"],
                            total_tokens=vision_result.usage["total_tokens"],
                            image_count=image_count  # Track images for billing
                        )
                    )

                    # Log vision completion
                    duration = time.time() - start_time
                    EventLogger.log_chat_completion(
                        session_id="vision",
                        model=request_body.model,
                        message_count=len(request_body.messages),
                        stream=False,
                        duration=duration,
                        tokens=vision_result.usage["total_tokens"],
                        tools_enabled=False
                    )
                    logger.info(f"✅ Vision request completed", extra={"duration": duration, "tokens": vision_result.usage["total_tokens"]})

                    # Track usage for Vision
                    if tenant:
                        from src.tenant import track_request_usage
                        attribution = extract_attribution_context(request)
                        await track_request_usage(
                            tenant=tenant,
                            model=vision_result.model,
                            input_tokens=vision_result.usage["prompt_tokens"],
                            output_tokens=vision_result.usage["completion_tokens"],
                            endpoint="/v1/chat/completions/vision",
                            latency_ms=int(duration * 1000),
                            status="success",
                            **attribution
                        )

                    return response

            except Exception as e:
                logger.error(f"❌ Vision request failed: {e}", exc_info=True)
                # Vision provider errors are upstream — tag accordingly
                raise BridgeError(classify_exception(e))

            # Continue with normal SDK flow (no vision content)
            # Process messages with session management
            all_messages, actual_session_id = session_manager.process_messages(
                request_body.messages, request_body.session_id
            )

            logger.info(f"Chat completion: session_id={actual_session_id}, total_messages={len(all_messages)}")

            # Privacy: Anonymize user messages before sending to Claude
            # Uses tenant's privacy mode from request.state (set by TenantMiddleware)
            # OR backend_config's privacy_enabled if backend routing is active
            privacy_client = get_privacy_client()
            anonymization_mapping = {}
            privacy_mode = get_privacy_mode_from_request(request)

            # Backend routing can override privacy (e.g., Bedrock EU disables privacy automatically)
            privacy_enabled = backend_config.privacy_enabled if backend_config else privacy_client.enabled

            if privacy_enabled:
                messages_for_anon = [
                    {'role': m.role, 'content': m.content}
                    for m in all_messages
                ]
                anon_messages, anonymization_mapping = await privacy_client.anonymize_messages(
                    messages_for_anon,
                    privacy_mode=privacy_mode
                )
                # Update all_messages with anonymized content
                all_messages = [
                    Message(role=m['role'], content=m['content'])
                    for m in anon_messages
                ]
                if anonymization_mapping:
                    logger.info(f"Privacy: Anonymized {len(anonymization_mapping)} PII entities (mode={privacy_mode})")

            # Convert messages to prompt
            prompt, system_prompt = MessageAdapter.messages_to_prompt(all_messages)

            # Log subject (first 80 chars of prompt for monitoring)
            subject = prompt[:80].replace('\n', ' ') if prompt else "(no prompt)"
            logger.info(f"Chat subject: {subject}...")
            
            # Filter content
            prompt = MessageAdapter.filter_content(prompt)
            if system_prompt:
                system_prompt = MessageAdapter.filter_content(system_prompt)
            
            # Get Claude Code SDK options from request
            claude_options = request_body.to_claude_options()
            
            # Merge with Claude-specific headers
            if claude_headers:
                claude_options.update(claude_headers)
            
            # Validate model
            if claude_options.get('model'):
                ParameterValidator.validate_model(claude_options['model'])

            # Handle tools - disabled by default for OpenAI compatibility
            if not request_body.enable_tools:
                # Set disallowed_tools to all available tools to disable them
                disallowed_tools = ['Task', 'Bash', 'Glob', 'Grep', 'LS', 'exit_plan_mode',
                                    'Read', 'Edit', 'MultiEdit', 'Write', 'NotebookRead',
                                    'NotebookEdit', 'WebFetch', 'TodoRead', 'TodoWrite', 'WebSearch']
                claude_options['disallowed_tools'] = disallowed_tools
                claude_options['max_turns'] = 1  # Single turn for Q&A
                logger.info("Tools disabled (default behavior for OpenAI compatibility)")
            else:
                logger.info("Tools enabled by user request")

            # Process x-claude-max-turns header (applies to BOTH enable_tools paths)
            request_headers = request.headers if request else {}
            x_claude_max_turns = request_headers.get('x-claude-max-turns', '').strip()
            if x_claude_max_turns:
                try:
                    claude_options['max_turns'] = int(x_claude_max_turns)
                    logger.info(f"x-claude-max-turns header override: {x_claude_max_turns}")
                except ValueError:
                    logger.warning(f"Invalid x-claude-max-turns value: {x_claude_max_turns}")

            # Handle X-Claude-File-Discovery header (opt-in file discovery)
            # request_headers already defined above for max_turns processing
            handle_file_discovery_header(request_headers, prompt, claude_options)

            # Collect all chunks
            chunks = []
            metadata_chunk = None  # Store file metadata separately
            cli_session_id = None  # Store session ID for header
            async for chunk in claude_cli.run_completion(
                prompt=prompt,
                system_prompt=system_prompt,
                model=claude_options.get('model'),
                max_turns=claude_options.get('max_turns', 10),
                allowed_tools=claude_options.get('allowed_tools'),
                disallowed_tools=claude_options.get('disallowed_tools'),
                stream=False,
                enable_file_discovery=claude_options.get('enable_file_discovery', False),
                backend_env_vars=backend_config.env_vars if backend_config else None
            ):
                # Capture metadata chunk separately
                if isinstance(chunk, dict) and chunk.get("type") == "x_claude_metadata":
                    metadata_chunk = chunk
                    # Extract session ID from metadata
                    if not cli_session_id and "session_tracking" in chunk:
                        cli_session_id = chunk["session_tracking"].get("cli_session_id")
                    logger.info("📦 Captured file metadata from CLI")
                    continue  # Don't add to chunks (not part of message)

                chunks.append(chunk)

            # Log chunk collection for debugging
            logger.info(f"Collected {len(chunks)} chunks from Claude Code SDK")
            if DEBUG_MODE or VERBOSE:
                if chunks:
                    logger.debug(f"First chunk type: {type(chunks[0])}")
                    logger.debug(f"First chunk: {str(chunks[0])[:200]}...")
                    logger.debug(f"Last chunk type: {type(chunks[-1])}")
                    logger.debug(f"Last chunk: {str(chunks[-1])[:200]}...")
                else:
                    logger.debug("No chunks received from SDK")

            # Extract assistant message
            raw_assistant_content = claude_cli.parse_claude_message(chunks)

            # 2026-05-05 v2: detect SDK-early-termination (layered, post-parse).
            # Pure-function helpers in claude_cli.py validated by pytest.
            from src.claude_cli import is_incomplete_response, chunks_have_tool_use
            _has_tools = chunks_have_tool_use(chunks)
            if is_incomplete_response(len(chunks), raw_assistant_content, _has_tools):
                _self_worker = os.getenv("INSTANCE_NAME", "unknown")
                logger.warning(
                    f"⚠️  Incomplete SDK response on worker {_self_worker} "
                    f"(chunks={len(chunks)}, content_empty=True, has_tools=False) "
                    f"— surfacing 503 + adaptive_limiter feedback"
                )
                from src.claude_cli import rate_limit_tracker as _rl_tracker
                try:
                    _rl_tracker.mark_soft_penalty(_self_worker, 30)
                except Exception as exc:
                    logger.debug(f"mark_soft_penalty failed: {exc}")
                try:
                    from src.middleware.rolling_metrics import get_rolling_metrics
                    get_rolling_metrics().record_rate_limit(_self_worker)
                except Exception as exc:
                    logger.debug(f"record_rate_limit failed: {exc}")
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": {
                            "message": (
                                f"[Bridge {_self_worker}] Upstream SDK terminated early "
                                f"({len(chunks)} chunks, no content). Retry."
                            ),
                            "type": "service_unavailable",
                            "code": "503",
                            "source": "bridge_internal",
                            "bridge_type": "incomplete_response",
                            "reason": "sdk_early_termination",
                            "retryable": True,
                            "retry_after_s": 30,
                            "bridge_worker": _self_worker,
                            "chunks_received": len(chunks),
                        }
                    },
                )

            # Rate-limit phrasing in the assistant text (non-streaming path).
            # The streaming loop in claude_cli already calls detect_in_text per
            # block, but a sync chat completion comes back as a single message
            # — without this check the rate-limit phrase would be returned to
            # the caller as response content while the worker stayed marked
            # available, so the pool router would route the next request to
            # the same exhausted account.
            if raw_assistant_content:
                from src.claude_cli import rate_limit_tracker as _rl_tracker
                _self_worker = os.getenv("INSTANCE_NAME", "unknown")
                _rl_match = _rl_tracker.detect_in_text(raw_assistant_content, _self_worker)
                if _rl_match:
                    _retry_after = _rl_tracker.get_retry_after(_self_worker) or 60
                    logger.warning(
                        f"🚫 Rate-limit response from worker {_self_worker} "
                        f"(pattern={_rl_match!r}) — surfacing 429 instead of leaking phrase"
                    )
                    # Feed the AdaptiveLoadLimiter — direct-return path bypasses rate_limit_handler.
                    from src.middleware.rolling_metrics import get_rolling_metrics
                    get_rolling_metrics().record_rate_limit(_self_worker)
                    return JSONResponse(
                        status_code=429,
                        content={"error": {
                            "message": f"[Bridge {_self_worker}] Account exhausted ({_rl_match})",
                            "type": "api_error",
                            "code": "429",
                            "source": "bridge_account",
                            "bridge_type": "worker_unavailable",
                            "reason": "worker_account_rate_limited",
                            "bridge_worker": _self_worker,
                            "retryable": True,
                            "retry_after_s": _retry_after,
                        }},
                        headers={"Retry-After": str(_retry_after)}
                    )

                # Quota-content detection: phrases that arrive as response text, not API errors.
                from src.claude_cli import detect_quota_exhaustion as _dqe
                if _dqe(raw_assistant_content):
                    logger.warning(
                        f"🚫 Quota-exhaustion phrase in response from {_self_worker} "
                        f"— raising RateLimitError so adaptive limiter is notified"
                    )
                    raise RateLimitError(
                        f"[Bridge {_self_worker}] Quota exhaustion detected in response content"
                    )

            if not raw_assistant_content:
                # CRITICAL: Detailed error logging for debugging
                logger.error(f"❌ parse_claude_message returned None!")
                logger.error(f"   Chunks count: {len(chunks)}")
                if chunks:
                    logger.error(f"   Chunk types: {[type(c).__name__ for c in chunks[:3]]}")
                    logger.error(f"   First chunk keys: {list(chunks[0].keys()) if isinstance(chunks[0], dict) else 'not dict'}")
                    logger.error(f"   Sample chunks: {chunks[:2]}")
                else:
                    logger.error(f"   SDK returned ZERO chunks - possible causes:")
                    logger.error(f"     - SDK internal error")
                    logger.error(f"     - Prompt too large/malformed")
                    logger.error(f"     - Tool configuration conflict")
                    logger.error(f"     - Model: {claude_options.get('model')}")
                    logger.error(f"     - Max turns: {claude_options.get('max_turns')}")
                    logger.error(f"     - Tools enabled: {request_body.enable_tools}")

                error_detail = {
                    "message": "No response from Claude Code SDK",
                    "chunks_received": len(chunks),
                    "prompt_length": len(prompt),
                    "model": request_body.model,
                    "max_turns": claude_options.get('max_turns'),
                    "tools_enabled": request_body.enable_tools
                }
                # Feed adaptive limiter so it shrinks cap and Lua-routing avoids
                # this worker temporarily. Symmetric to the 429-feedback path.
                try:
                    from src.middleware.rolling_metrics import get_rolling_metrics
                    get_rolling_metrics().record_worker_crash(_self_worker)
                    logger.warning(
                        f"🚨 record_worker_crash({_self_worker}) — SDK silent stall "
                        f"(chunks={len(chunks)}, prompt_len={len(prompt)}) — "
                        f"adaptive cap will shrink at next tune tick"
                    )
                except Exception as _rce:
                    logger.error(f"record_worker_crash failed (non-fatal): {_rce}")

                raise HTTPException(status_code=500, detail=f"No response from Claude Code: {error_detail}")
            
            # Filter out tool usage and thinking blocks
            assistant_content = MessageAdapter.filter_content(raw_assistant_content)

            # Tool-leak guard: when tools are disallowed and max_turns=1, Claude can
            # emit a pre-tool-use intro ("I'll write…") whose subsequent tool call is
            # blocked, leaving only the intro as the response. Retry once with a
            # hardened system prompt and max_turns=2 so a blocked-tool turn can still
            # recover with real text.
            if (
                not request_body.enable_tools
                and looks_like_tool_leak(assistant_content, prompt_chars=len(prompt or ""))
            ):
                logger.warning(
                    "🚨 Tool-leak detected in response — retrying once with hardened system prompt: "
                    f"len={len(assistant_content)} chars, intro={assistant_content[:80]!r}"
                )
                retry_options = dict(claude_options)
                retry_options['max_turns'] = max(int(retry_options.get('max_turns') or 1), 2)
                retry_system_prompt = hardened_system_prompt(system_prompt)
                retry_chunks = []
                async for chunk in claude_cli.run_completion(
                    prompt=prompt,
                    system_prompt=retry_system_prompt,
                    model=retry_options.get('model'),
                    max_turns=retry_options.get('max_turns', 2),
                    allowed_tools=retry_options.get('allowed_tools'),
                    disallowed_tools=retry_options.get('disallowed_tools'),
                    stream=False,
                    enable_file_discovery=retry_options.get('enable_file_discovery', False),
                    backend_env_vars=backend_config.env_vars if backend_config else None
                ):
                    if isinstance(chunk, dict) and chunk.get("type") == "x_claude_metadata":
                        continue
                    retry_chunks.append(chunk)
                retry_raw = claude_cli.parse_claude_message(retry_chunks)
                if retry_raw:
                    retry_content = MessageAdapter.filter_content(retry_raw)
                    if not looks_like_tool_leak(retry_content, prompt_chars=len(prompt or "")):
                        logger.info(
                            f"✅ Tool-leak retry recovered: {len(retry_content)} chars "
                            f"(was {len(assistant_content)})"
                        )
                        assistant_content = retry_content
                    else:
                        logger.error(
                            "❌ Tool-leak retry still leaked — returning best-effort original response. "
                            f"retry_len={len(retry_content)}, retry_intro={retry_content[:80]!r}"
                        )
                else:
                    logger.error("❌ Tool-leak retry returned no parsable assistant message")

            # Privacy: De-anonymize response (restore original PII)
            if anonymization_mapping:
                assistant_content = privacy_client.deanonymize_response(
                    assistant_content, anonymization_mapping
                )
                logger.debug("Privacy: De-anonymized response content")

            # Add assistant response to session if using session mode
            if actual_session_id:
                assistant_message = Message(role="assistant", content=assistant_content)
                session_manager.add_assistant_response(actual_session_id, assistant_message)

            # Estimate tokens (rough approximation)
            prompt_tokens = MessageAdapter.estimate_tokens(prompt)
            completion_tokens = MessageAdapter.estimate_tokens(assistant_content)

            # Create response with backend info
            response = ChatCompletionResponse(
                id=request_id,
                model=request_body.model,
                choices=[Choice(
                    index=0,
                    message=Message(role="assistant", content=assistant_content),
                    finish_reason="stop"
                )],
                usage=Usage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                    image_count=0  # Text-only request (no images)
                ),
                x_backend_info=BackendInfo(**get_backend_info_dict(backend_config)) if backend_config else None
            )

            # Log successful chat completion event
            duration = time.time() - start_time
            EventLogger.log_chat_completion(
                session_id=actual_session_id or "none",
                model=request_body.model,
                message_count=len(all_messages),
                stream=False,
                duration=duration,
                tokens=prompt_tokens + completion_tokens,
                tools_enabled=request_body.enable_tools
            )

            # Track usage for SDK (non-streaming)
            if tenant:
                from src.tenant import track_request_usage
                attribution = extract_attribution_context(request)
                await track_request_usage(
                    tenant=tenant,
                    model=request_body.model,
                    input_tokens=prompt_tokens,
                    output_tokens=completion_tokens,
                    endpoint="/v1/chat/completions",
                    latency_ms=int(duration * 1000),
                    status="success",
                    **attribution
                )

            # Worker instance info for multi-worker deployments
            worker_instance = os.getenv("INSTANCE_NAME", "unknown")

            # Rolling-metrics completion (non-streaming success).
            # Releases this request's slice of the in-flight token budget.
            try:
                if getattr(request.state, "arrival_recorded", False):
                    from src.middleware.rolling_metrics import get_rolling_metrics
                    get_rolling_metrics().record_completion(
                        worker=worker_instance,
                        status="success",
                        duration_ms=int(duration * 1000),
                        input_tokens=int(prompt_tokens or 0),
                        output_tokens=int(completion_tokens or 0),
                        est_input_tokens_at_arrival=int(getattr(request.state, "adaptive_est_tokens", 0) or 0),
                    )
                    request.state.arrival_recorded = False
            except Exception as _e:
                logger.debug(f"rolling_metrics completion (success) hook failed: {_e}")

            # Track non-streaming success in prompt metrics (with token estimates)
            try:
                from src.middleware.prompt_metrics import get_prompt_metrics
                attribution = extract_attribution_context(request)
                get_prompt_metrics().record(
                    app_id=attribution.get("app_id"),
                    agent_id=attribution.get("agent_id"),
                    workflow_id=attribution.get("workflow_id"),
                    duration_ms=int(duration * 1000),
                    status="success",
                    model=request_body.model,
                    input_tokens=prompt_tokens,
                    output_tokens=completion_tokens,
                    user_id=attribution.get("user_id"),
                    session_id=attribution.get("session_id"),
                    job_id=attribution.get("job_id"),
                )
            except Exception as e:
                logger.warning(f"prompt_metrics record (success) failed: {e}")

            # Add file metadata if available (OpenAI-compatible extension)
            if metadata_chunk:
                # Convert response to dict and add metadata
                response_dict = response.model_dump()
                response_dict["x_claude_metadata"] = {
                    "files_created": metadata_chunk.get("files_created", []),
                    "session_tracking": metadata_chunk.get("session_tracking", {}),
                    "discovery_status": metadata_chunk.get("discovery_status", "unknown"),
                    "worker_instance": worker_instance
                }

                # Include discovery_method if present
                if "discovery_method" in metadata_chunk:
                    response_dict["x_claude_metadata"]["discovery_method"] = metadata_chunk["discovery_method"]

                # Include discovery_details if present (for no_files_found case)
                if "discovery_details" in metadata_chunk:
                    response_dict["x_claude_metadata"]["discovery_details"] = metadata_chunk["discovery_details"]

                logger.info(
                    f"✅ Added file metadata to response",
                    extra={
                        "request_id": request_id,
                        "files_count": len(metadata_chunk.get("files_created", [])),
                        "discovery_status": metadata_chunk.get("discovery_status", "unknown")
                    }
                )

                # Return enriched response with session ID and worker headers
                headers = {"X-Worker-Instance": worker_instance}
                if cli_session_id:
                    headers["X-Claude-Session-ID"] = cli_session_id
                else:
                    # No fallback - better no header than wrong session ID (multi-tenant safety)
                    logger.warning("Session ID not available - X-Claude-Session-ID header omitted")
                return JSONResponse(content=response_dict, headers=headers)

            # Return response with session ID and worker headers
            # Also add worker_instance to response for non-metadata cases
            response_dict = response.model_dump()
            response_dict["x_claude_metadata"] = {"worker_instance": worker_instance}
            headers = {"X-Worker-Instance": worker_instance}
            if cli_session_id:
                headers["X-Claude-Session-ID"] = cli_session_id
            else:
                # No fallback - better no header than wrong session ID (multi-tenant safety)
                logger.warning("Session ID not available - X-Claude-Session-ID header omitted")
            return JSONResponse(content=response_dict, headers=headers)

    except HTTPException as http_exc:
        # CRITICAL: Log HTTPException before re-raising (prevents silent failures)
        duration = time.time() - start_time
        EventLogger.log_chat_completion(
            session_id=request_body.session_id or "none",
            model=request_body.model,
            message_count=len(request_body.messages),
            stream=request_body.stream,
            duration=duration,
            error=f"HTTPException {http_exc.status_code}: {http_exc.detail}",
            tools_enabled=request_body.enable_tools
        )
        logger.error(f"Chat completion HTTP error: {http_exc.status_code} - {http_exc.detail}")
        # Rolling-metrics completion (HTTP error path). MUST decrement in-flight
        # so the AdaptiveLoadLimiter's budget recovers.
        try:
            if getattr(request.state, "arrival_recorded", False):
                from src.middleware.rolling_metrics import get_rolling_metrics
                get_rolling_metrics().record_completion(
                    worker=os.getenv("INSTANCE_NAME", "unknown"),
                    status="error",
                    duration_ms=int(duration * 1000),
                    input_tokens=0,
                    output_tokens=0,
                    est_input_tokens_at_arrival=int(getattr(request.state, "adaptive_est_tokens", 0) or 0),
                )
                request.state.arrival_recorded = False
        except Exception as _e:
            logger.debug(f"rolling_metrics completion (HTTPException) failed: {_e}")
        # Track error in prompt metrics (even without tenant)
        try:
            from src.middleware.prompt_metrics import get_prompt_metrics
            attribution = extract_attribution_context(request)
            # Estimate input tokens if prompt was already built
            est_input = MessageAdapter.estimate_tokens(prompt) if 'prompt' in locals() and prompt else 0
            get_prompt_metrics().record(
                app_id=attribution.get("app_id"),
                agent_id=attribution.get("agent_id"),
                workflow_id=attribution.get("workflow_id"),
                duration_ms=int(duration * 1000),
                status="error",
                model=request_body.model,
                input_tokens=est_input,
                output_tokens=0,
                error_code=str(http_exc.status_code),
                user_id=attribution.get("user_id"),
                session_id=attribution.get("session_id"),
                job_id=attribution.get("job_id"),
            )
        except Exception:
            pass
        raise
    except WorkerUnavailableError as wue:
        # Rolling-metrics completion (worker rate-limited 429 path).
        try:
            if getattr(request.state, "arrival_recorded", False):
                _wud = time.time() - start_time
                from src.middleware.rolling_metrics import get_rolling_metrics
                get_rolling_metrics().record_completion(
                    worker=os.getenv("INSTANCE_NAME", "unknown"),
                    status="error",
                    duration_ms=int(_wud * 1000),
                    input_tokens=0,
                    output_tokens=0,
                    est_input_tokens_at_arrival=int(getattr(request.state, "adaptive_est_tokens", 0) or 0),
                )
                request.state.arrival_recorded = False
        except Exception as _e:
            logger.debug(f"rolling_metrics completion (WorkerUnavailableError) failed: {_e}")
        # Track 429 in prompt metrics before cross-bridge fallback / re-raise.
        # The HTTP status returned to the client is 429 (see worker_unavailable_handler).
        try:
            duration = time.time() - start_time
            from src.middleware.prompt_metrics import get_prompt_metrics
            attribution = extract_attribution_context(request)
            # Estimate input tokens if prompt was already built
            est_input = MessageAdapter.estimate_tokens(prompt) if 'prompt' in locals() and prompt else 0
            get_prompt_metrics().record(
                app_id=attribution.get("app_id"),
                agent_id=attribution.get("agent_id"),
                workflow_id=attribution.get("workflow_id"),
                duration_ms=int(duration * 1000),
                status="error",
                model=request_body.model,
                input_tokens=est_input,
                output_tokens=0,
                error_code="429",
                user_id=attribution.get("user_id"),
                session_id=attribution.get("session_id"),
                job_id=attribution.get("job_id"),
            )
        except Exception:
            pass
        # =====================================================================
        # CROSS-BRIDGE FALLBACK on worker rate-limit
        # ---------------------------------------------------------------------
        # nginx's proxy_next_upstream can only retry on a WORKER that is still
        # in the upstream pool and not marked `down` by smart-routing. When all
        # non-current workers have weekly=100% (marked `down`), a rate-limit
        # on the only live worker would otherwise bubble out as a raw 503.
        #
        # Instead we route the request to the provider fallback chain — which
        # ends in `bridge-prod-emergency` (Sahori on production). The primary
        # tier call never happened, so this path is defence-in-depth without
        # cost: fallback chain only runs when our own workers are exhausted.
        # =====================================================================
        if not request_body.stream:
            try:
                from src.providers.fallback import (
                    record_failure as _fb_fail, record_success as _fb_ok,
                    get_fallback_tiers as _fb_tiers, FALLBACK_DELAY_SECONDS as _fb_delay,
                )
                import asyncio as _fb_asyncio

                _primary_tier = request_body.provider_tier or "claude-premium"
                # DO NOT record_failure for rate-limiting — rate-limited ≠ broken.
                # The provider works fine, workers just need a cooldown.
                # Recording failure here caused a deadlock: provider marked "down"
                # → never retried → no success → stays "down" forever.
                _fallback_chain = _fb_tiers(_primary_tier)[1:]  # skip primary

                for _ft in _fallback_chain:
                    try:
                        logger.warning(
                            f"⚠️ Worker rate-limited on {_primary_tier}. "
                            f"Cross-bridge fallback: {_ft}"
                        )
                        await _fb_asyncio.sleep(_fb_delay)

                        _fb_cfg = resolve_backend_config(
                            backend=request_body.backend or BackendType.ANTHROPIC,
                            model=(resolved_model if 'resolved_model' in locals() else request_body.model),
                            privacy=request_body.privacy or PrivacyMode.AUTO,
                            bedrock_region=request_body.bedrock_region,
                            provider_tier=_ft,
                        )

                        if _fb_cfg.backend == BackendType.OPENAI_COMPATIBLE:
                            from src.providers.openai_compatible import call_openai_compatible
                            _fb_resp = await call_openai_compatible(
                                request_body,
                                _fb_cfg.provider_base_url,
                                _fb_cfg.provider_api_key,
                                model_override=_fb_cfg.provider_model,
                            )
                            _fb_ok(_ft)
                            logger.info(
                                f"✅ Cross-bridge fallback {_ft} succeeded "
                                f"(primary worker rate-limited)"
                            )
                            _fb_resp["x_fallback"] = {
                                "used": True,
                                "original_provider": _primary_tier,
                                "fallback_provider": _ft,
                                "original_error": f"worker rate-limited: {wue}",
                                "trigger": "worker_unavailable",
                            }
                            return _fb_resp
                    except Exception as _fb_err:
                        _fb_fail(_ft, str(_fb_err)[:200])
                        logger.warning(f"⚠️ Cross-bridge fallback {_ft} failed: {_fb_err}")
                        continue

                logger.error(
                    f"❌ All cross-bridge fallbacks exhausted for {_primary_tier} "
                    f"(worker rate-limited)"
                )
            except ImportError:
                pass  # fallback module unavailable — fall through to raise
            except Exception as _fb_setup_err:
                logger.error(
                    f"Cross-bridge fallback setup error: {_fb_setup_err}"
                )
        # Fallback didn't help (or streaming) — re-raise so nginx can try
        # another worker and/or the 503 envelope reaches the client.
        raise
    except Exception as e:
        # =======================================================================
        # FALLBACK: Attempt alternate providers if primary failed (non-streaming)
        # =======================================================================
        if not request_body.stream:
            try:
                from src.providers.fallback import (
                    is_retryable_error, record_failure, record_success,
                    get_fallback_tiers, FALLBACK_DELAY_SECONDS,
                )
                import asyncio

                primary_tier = request_body.provider_tier or "claude-premium"

                if is_retryable_error(e):
                    record_failure(primary_tier, str(e)[:200])
                    fallback_tiers = get_fallback_tiers(primary_tier)[1:]  # Skip primary

                    for fallback_tier in fallback_tiers:
                        try:
                            logger.warning(
                                f"⚠️ {primary_tier} failed: {e}. "
                                f"Attempting fallback: {fallback_tier}"
                            )
                            await asyncio.sleep(FALLBACK_DELAY_SECONDS)

                            fallback_config = resolve_backend_config(
                                backend=request_body.backend or BackendType.ANTHROPIC,
                                model=resolved_model,
                                privacy=request_body.privacy or PrivacyMode.AUTO,
                                bedrock_region=request_body.bedrock_region,
                                provider_tier=fallback_tier,
                            )

                            if fallback_config.backend == BackendType.OPENAI_COMPATIBLE:
                                from src.providers.openai_compatible import call_openai_compatible
                                response_data = await call_openai_compatible(
                                    request_body,
                                    fallback_config.provider_base_url,
                                    fallback_config.provider_api_key,
                                    model_override=fallback_config.provider_model,
                                )

                                fb_duration = time.time() - start_time
                                record_success(fallback_tier)
                                logger.info(
                                    f"✅ Fallback to {fallback_tier} successful "
                                    f"in {fb_duration:.2f}s"
                                )

                                # Track usage
                                usage = response_data.get("usage", {})
                                if tenant and usage:
                                    from src.tenant import track_request_usage
                                    attribution = extract_attribution_context(request)
                                    await track_request_usage(
                                        tenant=tenant,
                                        model=fallback_config.provider_model or resolved_model,
                                        input_tokens=usage.get("prompt_tokens", 0),
                                        output_tokens=usage.get("completion_tokens", 0),
                                        endpoint="/v1/chat/completions",
                                        latency_ms=int(fb_duration * 1000),
                                        status="fallback_success",
                                        **attribution
                                    )

                                # Mark response with fallback metadata
                                response_data["x_fallback"] = {
                                    "used": True,
                                    "original_provider": primary_tier,
                                    "fallback_provider": fallback_tier,
                                    "original_error": str(e)[:200],
                                }
                                return response_data

                        except Exception as fallback_error:
                            record_failure(fallback_tier, str(fallback_error)[:200])
                            logger.warning(
                                f"⚠️ Fallback {fallback_tier} also failed: {fallback_error}"
                            )
                            continue

                    logger.error(
                        f"❌ All fallback providers exhausted for {primary_tier}"
                    )
            except ImportError:
                pass  # Fallback module not available, continue to error handler

        # Log error event
        duration = time.time() - start_time
        EventLogger.log_chat_completion(
            session_id=request_body.session_id or "none",
            model=request_body.model,
            message_count=len(request_body.messages),
            stream=request_body.stream,
            duration=duration,
            error=str(e),
            tools_enabled=request_body.enable_tools
        )

        # Rolling-metrics completion (generic exception path).
        try:
            if getattr(request.state, "arrival_recorded", False):
                from src.middleware.rolling_metrics import get_rolling_metrics
                get_rolling_metrics().record_completion(
                    worker=os.getenv("INSTANCE_NAME", "unknown"),
                    status="error",
                    duration_ms=int(duration * 1000),
                    input_tokens=0,
                    output_tokens=0,
                    est_input_tokens_at_arrival=int(getattr(request.state, "adaptive_est_tokens", 0) or 0),
                )
                request.state.arrival_recorded = False
        except Exception as _e:
            logger.debug(f"rolling_metrics completion (Exception) failed: {_e}")

        logger.error(f"Chat completion error: {e}")
        # Classify — upstream markers (overloaded/gateway/timeout) get tagged
        # as source=upstream_anthropic / upstream_network so apps can tell the
        # error came from Anthropic, not the bridge itself. Only unclassifiable
        # exceptions fall through to `bridge_internal` / internal.
        raise BridgeError(classify_exception(e))
    finally:
        # Safety net for inflight counter leaks. All three except-blocks above
        # (HTTPException / WorkerUnavailableError / Exception) call
        # record_completion and clear `arrival_recorded`. But some terminations
        # bypass ALL of them — most notably asyncio.CancelledError (Python 3.8+
        # inherits from BaseException, not Exception), which fires when the
        # client disconnects mid-stream, the worker is OOM-killed, or the
        # ASGI server cancels the task. Without this finally, those cases
        # leave `_in_flight[worker]` and `_in_flight_input_tokens[worker]`
        # permanently incremented, starving the AdaptiveLoadLimiter until
        # the process is restarted.
        if getattr(request.state, "arrival_recorded", False):
            try:
                from src.middleware.rolling_metrics import get_rolling_metrics
                _leak_dur_ms = int((time.time() - start_time) * 1000)
                _leak_worker = os.getenv("INSTANCE_NAME", "unknown")
                _leak_est = int(getattr(request.state, "adaptive_est_tokens", 0) or 0)
                get_rolling_metrics().record_completion(
                    worker=_leak_worker,
                    status="error",
                    duration_ms=_leak_dur_ms,
                    input_tokens=0,
                    output_tokens=0,
                    est_input_tokens_at_arrival=_leak_est,
                )
                request.state.arrival_recorded = False
                logger.warning(
                    f"inflight counter leak prevented: request ended without hitting "
                    f"any completion site (likely CancelledError/abort). "
                    f"worker={_leak_worker} est_tokens_released={_leak_est} "
                    f"duration_ms={_leak_dur_ms}"
                )
            except Exception as _e:
                logger.debug(f"rolling_metrics outer-finally safety net failed: {_e}")


@app.post("/v1/research", response_model=ResearchResponse)
async def research(
    request_body: ResearchRequest,
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    _adaptive=Depends(adaptive_limit_dependency)
):
    """
    Dedicated research endpoint for Claude Code research tasks.

    Executes /sc:research commands with automatic file discovery and
    host filesystem integration for Docker deployments.

    Features:
    - Custom model selection (default: claude-sonnet-4-5-20250929)
    - Automatic output path handling (container → host copy for Docker)
    - File discovery from Claude Code session
    - Execution time tracking

    Example:
        ```bash
        curl -X POST http://localhost:8000/v1/research \\
          -H 'Content-Type: application/json' \\
          -H "Authorization: Bearer $AI_BRIDGE_API_KEY" \\
          -d '{
            "query": "Latest AI developments in 2025",
            "model": "claude-sonnet-4-5-20250929",
            "output_path": "/Users/rafael/research/ai_2025.md"
          }'
        ```
    """
    # Verify API key
    await verify_api_key(request, credentials)

    # Attribution enforcement
    enforce_attribution(request)

    # Rate limit pre-check: soft routing (same logic as chat endpoint)
    worker_id = os.getenv("INSTANCE_NAME", "unknown")
    from src.claude_cli import rate_limit_tracker as _research_rlt
    if _research_rlt.should_reject_new_request(worker_id):
        retry_after = _research_rlt.get_retry_after(worker_id)
        limit_type = "HARD" if _research_rlt.is_hard_limited(worker_id) else "soft"
        logger.info(f"⏳ Worker {worker_id} has {limit_type} penalty — rejecting research (NGINX failover)")
        # 429 = correct semantics; nginx http_429 in proxy_next_upstream triggers
        # failover to the next worker. Same rationale as chat/completions path.
        return JSONResponse(
            status_code=429,
            content={"error": {
                "message": f"[Bridge {worker_id}] Worker rate-limited ({limit_type})",
                "type": "api_error",
                "code": "429",
                "source": "bridge_account",
                "bridge_type": "worker_unavailable",
                "reason": "worker_account_rate_limited",
                "bridge_worker": worker_id,
                "retryable": True,
                "retry_after_s": retry_after,
            }},
            headers={"Retry-After": str(retry_after or 30)}
        )

    start_time = time.time()
    session_id = None
    container_file = None
    output_file = None

    # MODEL RESOLUTION for research endpoint
    original_model = request_body.model
    resolved_model, resolution_msg = resolve_model(original_model)

    if resolved_model is None:
        return ResearchResponse(
            status="error",
            query=request_body.query,
            model=original_model,
            error=f"Model '{original_model}' not supported. Use 'sonnet', 'haiku', 'opus' for latest, or exact IDs: {', '.join(get_all_model_ids())}"
        )

    if resolved_model != original_model:
        logger.info(f"Research model resolved: '{original_model}' -> '{resolved_model}'")
        request_body.model = resolved_model

    # BACKEND ROUTING: Resolve backend configuration for research
    backend_config = None
    try:
        backend_config = resolve_backend_config(
            backend=request_body.backend or BackendType.ANTHROPIC,
            model=resolved_model,
            privacy=request_body.privacy or PrivacyMode.AUTO,
            bedrock_region=request_body.bedrock_region
        )
    except RuntimeError as e:
        return ResearchResponse(
            status="error",
            query=request_body.query,
            model=request_body.model,
            error=str(e)
        )

    try:
        logger.info(
            f"🔬 Research request received",
            extra={
                "query": request_body.query[:100],
                "model": request_body.model,
                "depth": request_body.depth,
                "strategy": request_body.strategy,
                "max_hops": request_body.max_hops,
                "output_path": request_body.output_path,
                "backend": backend_config.backend.value if backend_config else "anthropic"
            }
        )

        # Construct SuperClaude research command with options
        research_prompt = f"/sc:research \"{request_body.query}\""

        # Add depth parameter
        if request_body.depth:
            research_prompt += f" --depth {request_body.depth}"

        # Add strategy parameter
        if request_body.strategy:
            research_prompt += f" --strategy {request_body.strategy}"

        # Add max_hops if specified (overrides depth)
        if request_body.max_hops:
            research_prompt += f" --max-hops {request_body.max_hops}"

        # Add confidence threshold
        if request_body.confidence_threshold and request_body.confidence_threshold != 0.7:
            research_prompt += f" --confidence {request_body.confidence_threshold}"

        # Add parallel searches
        if request_body.parallel_searches and request_body.parallel_searches != 5:
            research_prompt += f" --parallel {request_body.parallel_searches}"

        # Add source filter
        if request_body.source_filter:
            filters = ",".join(request_body.source_filter)
            research_prompt += f" --sources {filters}"

        # Execute research via Claude Code SDK
        logger.info("🚀 Starting research execution...")

        # Execute research (note: claude_cli.run_completion is async generator)
        # Collect all chunks to extract session_id and file metadata
        all_chunks = []
        file_metadata = None
        session_id = None

        async for chunk in claude_cli.run_completion(
            prompt=research_prompt,
            model=request_body.model,
            max_turns=request_body.max_turns,
            allowed_tools=None,  # None means all tools allowed
            stream=True,
            enable_file_discovery=True,
            backend_env_vars=backend_config.env_vars if backend_config else None
        ):
            all_chunks.append(chunk)

            # Extract session_id from any chunk that has it
            if "session_id" in chunk:
                session_id = chunk["session_id"]

            # Extract file metadata from x_claude_metadata chunk
            if chunk.get("type") == "x_claude_metadata":
                file_metadata = chunk
                logger.info(f"📦 Found file metadata: {len(chunk.get('files_created', []))} files")
                # CRITICAL: Use cli_session_id for directory matching, not SDK's session_id
                if "session_tracking" in chunk:
                    cli_session_id = chunk["session_tracking"].get("cli_session_id")
                    if cli_session_id:
                        session_id = cli_session_id
                        logger.info(f"📁 Using cli_session_id: {session_id}")

        if not all_chunks:
            raise ValueError("No response received from Claude Code execution")

        execution_time = time.time() - start_time

        logger.info(
            f"✅ Research completed",
            extra={
                "session_id": session_id,
                "execution_time": execution_time,
                "total_chunks": len(all_chunks)
            }
        )

        # Extract discovered files from metadata
        discovered_files = []
        if file_metadata and file_metadata.get("files_created"):
            for file_info in file_metadata["files_created"]:
                file_path = Path(file_info["path"])  # Use "path" not "absolute_path"
                discovered_files.append(file_path)
                logger.info(f"📄 Discovered file from metadata: {file_path}")

        # Fallback: Manual discovery if metadata didn't contain files
        if not discovered_files and session_id:
            try:
                # Initialize file discovery service
                wrapper_root = Path(claude_cli.cwd) if claude_cli.cwd else Path.cwd()
                file_discovery = FileDiscoveryService(wrapper_root)

                # Try to discover files from session
                # Session directory format: YYYY-MM-DD-HHMM_{session_id}
                # Use glob pattern to find directory with matching session_id suffix
                matching_dirs = list(wrapper_root.glob(f"*_{session_id}"))

                if matching_dirs:
                    session_dir = matching_dirs[0]  # Take first match (should be only one)
                    logger.info(f"📁 Found session directory: {session_dir.name}")

                    # Scan claudedocs directory for research output
                    claudedocs_dir = session_dir / "claudedocs"
                    if claudedocs_dir.exists():
                        for file_path in claudedocs_dir.glob("*.md"):
                            discovered_files.append(file_path)
                            logger.info(f"📄 Discovered file: {file_path}")
                    else:
                        logger.warning(f"⚠️  Session directory found but no claudedocs/: {session_dir}")
                else:
                    logger.warning(f"⚠️  No session directory found for session_id: {session_id}")

            except Exception as e:
                logger.warning(
                    f"⚠️  File discovery failed (non-critical): {e}",
                    exc_info=True
                )

        # Determine output paths
        if discovered_files:
            container_file = str(discovered_files[0])  # Use first markdown file

            # Determine host output path
            if request_body.output_path:
                output_file = request_body.output_path
            else:
                # Default to /tmp/ with research filename
                filename = discovered_files[0].name
                output_file = f"/tmp/{filename}"

            # Copy file from container to host (if in Docker)
            try:
                # Check if we're in Docker by checking for /.dockerenv
                in_docker = Path("/.dockerenv").exists()

                if in_docker:
                    # In Docker: Copy file to output path
                    # Since we're inside Docker, we can directly copy the file
                    # if output_path is accessible
                    logger.info(f"🐳 Docker environment detected")

                    # For Docker, we just copy locally since we ARE in the container
                    if container_file and output_file:
                        shutil.copy2(container_file, output_file)
                        logger.info(f"📋 Copied: {container_file} → {output_file}")
                else:
                    # Not in Docker: Direct file access
                    if container_file and output_file:
                        shutil.copy2(container_file, output_file)
                        logger.info(f"📋 Copied: {container_file} → {output_file}")

            except Exception as e:
                logger.error(
                    f"❌ File copy failed: {e}",
                    exc_info=True,
                    extra={
                        "container_file": container_file,
                        "output_file": output_file
                    }
                )
                # Don't fail the request, just log the error

        # Get file size and content if available
        file_size_bytes = None
        content = None

        # Try to read content from output_file or container_file
        content_file = None
        if output_file and Path(output_file).exists():
            content_file = output_file
        elif container_file and Path(container_file).exists():
            content_file = container_file

        if content_file:
            file_size_bytes = Path(content_file).stat().st_size
            try:
                with open(content_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                logger.info(f"📄 Read content: {len(content)} chars from {content_file}")
            except Exception as e:
                logger.warning(f"⚠️ Could not read content: {e}")

        # ====================================================================
        # DEFENSIVE: lift chat_completions defenses to research endpoint
        # --------------------------------------------------------------------
        # The chat completions endpoint (line ~2240) raises HTTPException 500
        # when parse_claude_message returns None and RateLimitError when
        # detect_quota_exhaustion fires on the response text. Without these,
        # research silently returns status=success with empty content/output
        # whenever the SDK gets rate-limited or stalls -- pipelines then crash
        # 40 minutes later on a meaningless empty response. Same defenses
        # applied here.
        # ====================================================================
        from src.claude_cli import detect_quota_exhaustion as _dqe

        # 1) Parse chunks the same way chat_completions does
        try:
            parsed_assistant_text = claude_cli.parse_claude_message(all_chunks)
        except Exception as _parse_err:
            logger.error(
                f"\u274c Research parse_claude_message raised: {_parse_err}",
                exc_info=True,
            )
            parsed_assistant_text = None

        # 2) Quota exhaustion phrase scan on whatever text we have.
        #    If the SDK silently bailed on rate-limit, the marker is in either
        #    the parsed assistant text or the file content (rare).
        for _candidate, _label in (
            (parsed_assistant_text, "assistant text"),
            (content, "file content"),
        ):
            if _candidate and _dqe(_candidate):
                logger.warning(
                    f"\U0001f6ab Research: quota-exhaustion phrase detected in {_label} "
                    f"-- raising RateLimitError so adaptive limiter is notified"
                )
                raise RateLimitError(
                    f"[Bridge research] Quota exhaustion detected in {_label}"
                )

        # 3) Empty-output guard: if discovery + parse both came up empty, this
        #    is a stalled/quota-killed run masquerading as success. Fail loud
        #    so Nginx / the consumer can see it instead of inheriting None.
        if not discovered_files and not content and not parsed_assistant_text:
            logger.error(
                f"\u274c Research produced no output: "
                f"discovered_files=0, content=None, parsed_text=None, "
                f"chunks={len(all_chunks)}, session_id={session_id}"
            )
            raise HTTPException(
                status_code=500,
                detail={
                    "message": "Research SDK completed but produced no output",
                    "chunks_received": len(all_chunks),
                    "session_id": session_id,
                    "execution_time_seconds": round(execution_time, 2),
                    "hint": (
                        "Likely cause: rate_limit_event with internal retry that "
                        "never recovered, or session-id mismatch in file discovery. "
                        "Check worker logs for rate_limit_event around session start."
                    ),
                },
            )

        # 4) Parsed text fallback: discovery failed and no file content, but the
        #    SDK did emit assistant text -- carry that as the content payload
        #    rather than returning None.
        if not content and parsed_assistant_text:
            empty_chars = len(parsed_assistant_text)
            logger.warning(
                f"\u26a0\ufe0f  Research: file content unavailable but parsed_text exists "
                f"({empty_chars} chars) -- using parsed_text as content fallback"
            )
            content = parsed_assistant_text

        return ResearchResponse(
            status="success",
            query=request_body.query,
            model=request_body.model,
            output_file=output_file,
            container_file=container_file,
            execution_time_seconds=round(execution_time, 2),
            file_size_bytes=file_size_bytes,
            content=content,
            error=None,
            session_id=session_id
        )

    except WorkerUnavailableError:
        # Re-raise to trigger HTTP 503 and Nginx failover to another worker
        # User will NOT see this error - Nginx handles it transparently
        raise

    except Exception as e:
        execution_time = time.time() - start_time
        logger.error(
            f"❌ Research failed: {e}",
            exc_info=True,
            extra={
                "query": request_body.query,
                "model": request_body.model,
                "execution_time": execution_time
            }
        )

        return ResearchResponse(
            status="error",
            query=request_body.query,
            model=request_body.model,
            output_file=None,
            container_file=None,
            execution_time_seconds=round(execution_time, 2),
            file_size_bytes=None,
            error=str(e),
            session_id=session_id
        )


@app.get("/v1/research/{session_id}/content")
async def get_research_content(
    session_id: str,
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """Download research output content by session ID.

    Returns the markdown output file or final_response.json as fallback.
    """
    await verify_api_key(request, credentials)

    wrapper_root = Path(os.environ.get("INSTANCES_DIR", "/app/instances"))

    # Find session directory (pattern: YYYY-MM-DD-HHMM_{session_id})
    matching_dirs = list(wrapper_root.glob(f"*_{session_id}"))
    if not matching_dirs:
        logger.warning(f"Session not found: {session_id}", extra={"session_id": session_id})
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    session_dir = matching_dirs[0]
    claudedocs_dir = session_dir / "claudedocs"

    # Find markdown output
    md_files = list(claudedocs_dir.glob("*.md")) if claudedocs_dir.exists() else []

    if md_files:
        output_file = md_files[0]
        logger.info(f"📄 Returning research output: {output_file.name}", extra={"session_id": session_id})
        return Response(
            content=output_file.read_text(encoding='utf-8'),
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{output_file.name}"'}
        )

    # Fallback: return final_response.json if no .md file
    final_response_file = session_dir / "final_response.json"
    if final_response_file.exists():
        logger.info(f"📄 Returning final_response.json (no .md found)", extra={"session_id": session_id})
        return Response(
            content=final_response_file.read_text(encoding='utf-8'),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="final_response.json"'}
        )

    logger.warning(f"No output file found for session: {session_id}", extra={"session_id": session_id})
    raise HTTPException(status_code=404, detail=f"No output file found for session: {session_id}")


@app.get("/v1/models")
async def list_models(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """List available models.

    Models are defined centrally in model_registry.py.
    Supports fuzzy matching: "sonnet" -> latest Sonnet, "haiku" -> latest Haiku
    """
    # Check FastAPI API key if configured
    await verify_api_key(request, credentials)

    return {
        "object": "list",
        "data": get_models_for_api()
    }


@app.post("/v1/compatibility")
async def check_compatibility(request_body: ChatCompletionRequest):
    """Check OpenAI API compatibility for a request."""
    report = CompatibilityReporter.generate_compatibility_report(request_body)
    return {
        "compatibility_report": report,
        "claude_code_sdk_options": {
            "supported": [
                "model", "system_prompt", "max_turns", "allowed_tools", 
                "disallowed_tools", "permission_mode", "max_thinking_tokens",
                "continue_conversation", "resume", "cwd"
            ],
            "custom_headers": [
                "X-Claude-Max-Turns", "X-Claude-Allowed-Tools", 
                "X-Claude-Disallowed-Tools", "X-Claude-Permission-Mode",
                "X-Claude-Max-Thinking-Tokens"
            ]
        }
    }


@app.get("/v1/usage/status")
async def usage_status(request: Request):
    """
    Get current AI usage status for a tenant.

    Returns token usage, limits, and budget information.
    Requires X-Tenant-API-Key header.
    """
    tenant = get_tenant_from_request(request)
    if not tenant:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "Tenant API key required. Set X-Tenant-API-Key header.",
                    "type": "authentication_required",
                    "code": "missing_tenant_key"
                }
            }
        )

    from src.tenant import check_budget
    budget = await check_budget(tenant)

    return {
        "tenant_id": tenant.tenant_id,
        "tenant_slug": tenant.tenant_slug,
        "billing_mode": budget.billing_mode,
        "current_month": {
            "tokens_used": budget.current_tokens,
            "vision_calls": budget.current_vision_calls,
            "cost_usd": budget.current_cost_usd,
        },
        "limits": {
            "monthly_token_limit": budget.token_limit,
            "monthly_vision_limit": budget.vision_limit,
            "budget_limit_eur": budget.budget_limit_eur,
        },
        "usage_percent": {
            "tokens": round(budget.token_usage_percent, 1),
            "budget": round(budget.budget_usage_percent, 1),
        },
        "allowed": budget.allowed,
        "reason": budget.reason,
    }


@app.get("/health")
@rate_limit_endpoint("health")
async def health_check(request: Request):
    """Health check endpoint with provider health status."""
    worker_instance = os.getenv("INSTANCE_NAME", "unknown")

    result = {
        "status": "healthy",
        "service": "claude-code-openai-wrapper",
        "worker_instance": worker_instance,
    }

    # Include provider fallback health if available
    try:
        from src.providers.fallback import get_all_provider_health
        provider_health = get_all_provider_health()
        if provider_health:
            result["providers"] = provider_health
    except ImportError:
        pass

    return result


@app.post("/health/reset")
async def reset_provider_health_endpoint(request: Request):
    """Reset provider health counters (clears consecutive_failures deadlocks).

    Body (optional): {"provider": "claude-premium"} to reset a specific provider.
    Empty body or {} resets all providers.
    """
    from src.providers.fallback import reset_provider_health, reset_all_provider_health

    try:
        body = await request.json()
    except Exception:
        body = {}

    provider = body.get("provider")
    if provider:
        found = reset_provider_health(provider)
        return {"reset": provider, "found": found}
    else:
        count = reset_all_provider_health()
        return {"reset": "all", "count": count}


@app.get("/debug/tokens")
async def debug_tokens(request: Request):
    """Debug endpoint to check TokenRotator status."""
    from src.auth import token_rotator
    return {
        "total_tokens": len(token_rotator.tokens),
        "current_index": token_rotator.current_index,
        "token_files": [str(f) for f in token_rotator.token_files],
        "token_previews": [t[:25] + "..." for t in token_rotator.tokens] if token_rotator.tokens else [],
        "status": "ok" if len(token_rotator.tokens) > 1 else "warning_single_token"
    }


@app.get("/rate-limits")
async def get_rate_limits(request: Request):
    """
    Get current rate limit status for all workers.
    Useful for monitoring and debugging rate limit issues.
    """
    from datetime import datetime
    worker_instance = os.getenv("INSTANCE_NAME", "unknown")
    rate_limits = rate_limit_tracker.get_all_rate_limits()

    # Check if current worker is rate-limited
    current_worker_limited = rate_limit_tracker.is_rate_limited(worker_instance)
    retry_after = rate_limit_tracker.get_retry_after(worker_instance)

    # Format rate limits for JSON response
    formatted_limits = {}
    for worker_id, reset_time in rate_limits.items():
        formatted_limits[worker_id] = {
            "reset_time": reset_time.isoformat(),
            "retry_after_seconds": max(0, int((reset_time - datetime.now()).total_seconds()))
        }

    return {
        "current_worker": worker_instance,
        "current_worker_rate_limited": current_worker_limited,
        "current_worker_retry_after": retry_after,
        "all_rate_limits": formatted_limits,
        "total_workers_limited": len(rate_limits)
    }


@app.get("/ready")
async def ready_check(request: Request):
    """Readiness endpoint for graceful rebuild. Returns active request count."""
    stats = request_limiter.get_stats()
    return {
        "ready_for_shutdown": stats['active_requests'] == 0,
        "active_requests": stats['active_requests'],
        "service": "claude-code-openai-wrapper",
        "worker": os.getenv("INSTANCE_NAME", "unknown"),
    }


@app.get("/stats")
async def get_stats(request: Request):
    """Get wrapper statistics including request limiting and memory usage."""
    stats = request_limiter.get_stats()
    return {
        "service": "claude-code-openai-wrapper",
        "request_limiting": stats,
        "status": "healthy" if stats['active_requests'] < stats['max_concurrent'] else "busy",
        "can_accept_requests": stats['active_requests'] < stats['max_concurrent'] and stats['memory_usage_percent'] < stats['memory_threshold']
    }


@app.get("/worker-capacity")
async def worker_capacity(request: Request):
    """
    Worker capacity endpoint for smart load balancing.

    Returns this worker's current capacity status:
    - available: Can accept requests
    - rate_limited: Rate-limited, includes reset time
    - busy: At max concurrent requests

    NGINX or a smart router can poll this to route proactively
    instead of waiting for 503 failover.
    """
    worker_id = os.getenv("INSTANCE_NAME", "unknown")
    stats = request_limiter.get_stats()
    is_limited = rate_limit_tracker.is_rate_limited(worker_id)
    retry_after = rate_limit_tracker.get_retry_after(worker_id)

    if is_limited:
        status = "rate_limited"
    elif stats['active_requests'] >= stats['max_concurrent']:
        status = "busy"
    else:
        status = "available"

    return {
        "worker_id": worker_id,
        "status": status,
        "available": status == "available",
        "active_requests": stats['active_requests'],
        "max_concurrent": stats['max_concurrent'],
        "rate_limited": is_limited,
        "retry_after_seconds": retry_after,
        "memory_usage_percent": stats.get('memory_usage_percent', 0),
    }


@app.get("/license-health")
async def license_health_check(request: Request):
    """
    Test this worker's Claude license/token by making a minimal API call.

    Returns:
    - status: "healthy" | "rate_limited" | "error"
    - worker_id: which worker this is
    - token_preview: first 25 chars of token
    - reset_time: when rate limit resets (if rate_limited)
    - test_response: the actual response from Claude (if healthy)

    Usage: Query each worker directly or through load balancer to check all licenses.

    Example aggregation script:
    ```bash
    # Check all workers
    for worker in "worker1:8000" "worker2:8000" "host:8001" "host:8002"; do
      curl -s "http://$worker/license-health" | jq -c '{worker: .worker_id, status: .status}'
    done
    ```
    """
    import time
    from datetime import datetime

    worker_id = os.getenv("INSTANCE_NAME", "unknown")
    from src.auth import token_rotator
    token_preview = token_rotator.tokens[0][:25] + "..." if token_rotator.tokens else "NO_TOKEN"

    # Check if already known to be rate-limited
    if rate_limit_tracker.is_rate_limited(worker_id):
        retry_after = rate_limit_tracker.get_retry_after(worker_id)
        rate_limits = rate_limit_tracker.get_all_rate_limits()
        reset_time = rate_limits.get(worker_id)
        return {
            "status": "rate_limited",
            "worker_id": worker_id,
            "token_preview": token_preview,
            "reset_time": reset_time.isoformat() if reset_time else None,
            "retry_after_seconds": retry_after,
            "message": f"Token rate-limited until {reset_time}"
        }

    # Try a minimal test call
    start_time = time.time()
    try:
        # Create a minimal test prompt
        test_messages = [{"role": "user", "content": "Reply with exactly: OK"}]

        # Use the ClaudeCodeCLI to test
        cli = ClaudeCodeCLI(timeout=30000)  # 30 second timeout
        response_text = ""

        async for message in cli.run_completion(
            prompt="Reply with exactly: OK",
            system_prompt=None,
            model="claude-haiku-4-5-20251001",
            max_turns=1
        ):
            # Check for rate limit in response
            if hasattr(message, 'content') and message.content:
                for block in message.content:
                    if hasattr(block, 'text') and block.text:
                        text_lower = block.text.lower()
                        if "hit your limit" in text_lower or "rate limit" in text_lower:
                            # Mark as rate-limited
                            rate_limit_tracker.mark_rate_limited(worker_id, block.text)
                            return {
                                "status": "rate_limited",
                                "worker_id": worker_id,
                                "token_preview": token_preview,
                                "reset_time": None,
                                "message": block.text[:100]
                            }
                        response_text = block.text

        duration = time.time() - start_time
        return {
            "status": "healthy",
            "worker_id": worker_id,
            "token_preview": token_preview,
            "test_response": response_text[:50] if response_text else "NO_RESPONSE",
            "test_duration_seconds": round(duration, 2),
            "message": "License is active and working"
        }

    except WorkerUnavailableError as e:
        # Token failed, likely rate-limited
        rate_limit_tracker.mark_rate_limited(worker_id, str(e))
        return {
            "status": "rate_limited",
            "worker_id": worker_id,
            "token_preview": token_preview,
            "error": str(e)[:100],
            "message": "Token appears to be rate-limited"
        }

    except Exception as e:
        logger.error(f"License health check failed: {e}")
        return {
            "status": "error",
            "worker_id": worker_id,
            "token_preview": token_preview,
            "error": str(e)[:100],
            "message": "Unexpected error during license check"
        }


@app.post("/v1/debug/request")
@rate_limit_endpoint("debug")
async def debug_request_validation(request: Request):
    """Debug endpoint to test request validation and see what's being sent."""
    try:
        # Get the raw request body
        body = await request.body()
        raw_body = body.decode() if body else ""
        
        # Try to parse as JSON
        parsed_body = None
        json_error = None
        try:
            import json as json_lib
            parsed_body = json_lib.loads(raw_body) if raw_body else {}
        except Exception as e:
            json_error = str(e)
        
        # Try to validate against our model
        validation_result = {"valid": False, "errors": []}
        if parsed_body:
            try:
                chat_request = ChatCompletionRequest(**parsed_body)
                validation_result = {"valid": True, "validated_data": chat_request.model_dump()}
            except ValidationError as e:
                validation_result = {
                    "valid": False,
                    "errors": [
                        {
                            "field": " -> ".join(str(loc) for loc in error.get("loc", [])),
                            "message": error.get("msg", "Unknown error"),
                            "type": error.get("type", "validation_error"),
                            "input": error.get("input")
                        }
                        for error in e.errors()
                    ]
                }
        
        return {
            "debug_info": {
                "headers": dict(request.headers),
                "method": request.method,
                "url": str(request.url),
                "raw_body": raw_body,
                "json_parse_error": json_error,
                "parsed_body": parsed_body,
                "validation_result": validation_result,
                "debug_mode_enabled": DEBUG_MODE or VERBOSE,
                "example_valid_request": {
                    "model": "claude-3-sonnet-20240229",
                    "messages": [
                        {"role": "user", "content": "Hello, world!"}
                    ],
                    "stream": False
                }
            }
        }
        
    except Exception as e:
        return {
            "debug_info": {
                "error": f"Debug endpoint error: {str(e)}",
                "headers": dict(request.headers),
                "method": request.method,
                "url": str(request.url)
            }
        }


@app.get("/v1/providers")
async def list_providers():
    """List available LLM provider tiers and their configurations."""
    from src.providers.registry import list_available_providers
    return {"providers": list_available_providers()}


@app.get("/v1/privacy/status")
async def get_privacy_status():
    """Get privacy service status. Proxies to privacy-pdf-service container."""
    privacy_client = get_privacy_client()
    try:
        client = await privacy_client._get_client()
        response = await client.get("/status")
        response.raise_for_status()
        service_status = response.json()
    except Exception as e:
        logger.warning(f"Privacy service unreachable: {e}")
        service_status = {"enabled": False, "available": False, "error": str(e)}

    return {"privacy": service_status}


@app.post("/v1/privacy/smart-anonymize")
async def smart_anonymize_endpoint(request_body: SmartAnonymizeRequest):
    """Smart pseudonymization. Proxied to privacy-pdf-service container."""
    privacy_client = get_privacy_client()
    try:
        client = await privacy_client._get_client()
        response = await client.post("/smart-anonymize", json={
            "text": request_body.text,
            "language": request_body.language or "de",
            "context_hint": request_body.context_hint,
            "prefix": request_body.prefix,
        }, timeout=120.0)  # Smart-anonymize involves AI call, needs longer timeout
        response.raise_for_status()
        return SmartAnonymizeResponse(**response.json())
    except Exception as e:
        logger.error(f"Smart anonymization failed: {e}", exc_info=True)
        return SmartAnonymizeResponse(
            status="error",
            error=str(e)
        )


# ============================================================================
# PDF Conversion Endpoint — Proxied to privacy-pdf-service (has Docling)
# ============================================================================

@app.post("/v1/convert-pdf", response_model=ConvertPdfResponse)
async def convert_pdf_endpoint(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """Convert PDF to Markdown. Proxied to privacy-pdf-service (has Docling)."""
    await verify_api_key(request, credentials)

    try:
        form = await request.form()
        file = form.get("file")
        if not file:
            return ConvertPdfResponse(
                status="error",
                error="No file uploaded. Send PDF as multipart/form-data with field name 'file'."
            )

        filename = getattr(file, "filename", "upload.pdf") or "upload.pdf"
        pdf_bytes = await file.read()

        privacy_client = get_privacy_client()
        client = await privacy_client._get_client()

        # Forward as multipart to privacy-pdf-service
        response = await client.post(
            "/convert-pdf",
            files={"file": (filename, pdf_bytes, "application/pdf")},
            timeout=300.0,  # PDF conversion can take a while
        )
        response.raise_for_status()
        data = response.json()
        return ConvertPdfResponse(**data)

    except Exception as e:
        logger.error(f"PDF conversion proxy failed: {e}", exc_info=True)
        return ConvertPdfResponse(
            status="error",
            error=f"PDF conversion failed: {str(e)}"
        )


@app.post("/v1/convert-pdf-to-semantic-html")
async def convert_pdf_to_semantic_html_endpoint(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """Convert PDF to semantic HTML via ConvertAPI + AI. Proxied to privacy-pdf-service."""
    await verify_api_key(request, credentials)

    try:
        form = await request.form()
        file = form.get("file")
        if not file:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "error": "No file uploaded. Send PDF as multipart/form-data with field name 'file'."}
            )

        filename = getattr(file, "filename", "upload.pdf") or "upload.pdf"
        pdf_bytes = await file.read()

        privacy_client = get_privacy_client()
        client = await privacy_client._get_client()

        # Forward as multipart to privacy-pdf-service
        response = await client.post(
            "/convert-pdf-to-semantic-html",
            files={"file": (filename, pdf_bytes, "application/pdf")},
            timeout=600.0,  # ConvertAPI + AI conversion can take several minutes
        )
        response.raise_for_status()
        return JSONResponse(content=response.json())

    except Exception as e:
        logger.error(f"PDF-to-semantic-HTML proxy failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": f"PDF-to-semantic-HTML conversion failed: {str(e)}"}
        )


@app.post("/v1/convert-html-to-docx")
async def convert_html_to_docx_endpoint(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """Convert HTML to DOCX via ConvertAPI. Proxied to privacy-pdf-service."""
    await verify_api_key(request, credentials)

    try:
        body = await request.json()
        if not isinstance(body, dict) or not body.get("html"):
            return JSONResponse(
                status_code=400,
                content={"status": "error", "error": "Request body must be JSON with 'html' field."}
            )

        privacy_client = get_privacy_client()
        client = await privacy_client._get_client()

        response = await client.post(
            "/convert-html-to-docx",
            json=body,
            timeout=600.0,
        )
        response.raise_for_status()
        return JSONResponse(content=response.json())

    except Exception as e:
        logger.error(f"HTML-to-DOCX proxy failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": f"HTML-to-DOCX conversion failed: {str(e)}"}
        )


@app.post("/v1/convert-pdf-to-html-direct")
async def convert_pdf_to_html_direct_endpoint(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """Convert PDF to pixel-perfect HTML via ConvertAPI. Proxied to privacy-pdf-service."""
    await verify_api_key(request, credentials)

    try:
        body = await request.json()
        if not isinstance(body, dict) or not body.get("pdf_base64"):
            return JSONResponse(
                status_code=400,
                content={"status": "error", "error": "Request body must be JSON with 'pdf_base64' field."}
            )

        privacy_client = get_privacy_client()
        client = await privacy_client._get_client()

        response = await client.post(
            "/convert-pdf-to-html-direct",
            json=body,
            timeout=600.0,
        )
        response.raise_for_status()
        return JSONResponse(content=response.json())

    except Exception as e:
        logger.error(f"PDF-to-HTML-direct proxy failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": f"PDF-to-HTML-direct conversion failed: {str(e)}"}
        )


# ============================================================================
# Universal Document Conversion — proxied to privacy-pdf-service
# Routes any supported document type (PDF/DOCX/PPTX/XLSX/CSV/HTML/MSG/EML/image)
# to Markdown, optionally with atomic smart-anonymize in one shot.
# ============================================================================


async def _proxy_document_endpoint(
    request: Request, downstream_path: str, timeout: float
) -> JSONResponse:
    """Forward a multipart document upload to the privacy-pdf-service.

    Preserves filename, MIME type and any extra string form fields the caller
    sent (e.g. ``language``, ``privacy_mode``, ``mime_type_hint``).
    """
    try:
        form = await request.form()
        file = form.get("file")
        if not file or not hasattr(file, "read"):
            return JSONResponse(
                status_code=400,
                content={"status": "error", "error": "No file uploaded. Send the document as multipart/form-data with field name 'file'."},
            )

        filename = getattr(file, "filename", "upload.bin") or "upload.bin"
        content_type = getattr(file, "content_type", None) or "application/octet-stream"
        content = await file.read()

        # Pass through any extra string form fields untouched.
        extra_data: Dict[str, str] = {}
        for key, value in form.multi_items():
            if key == "file":
                continue
            if isinstance(value, str):
                extra_data[key] = value

        privacy_client = get_privacy_client()
        client = await privacy_client._get_client()

        response = await client.post(
            downstream_path,
            files={"file": (filename, content, content_type)},
            data=extra_data,
            timeout=timeout,
        )

        # Surface upstream status codes 1:1 so callers see 415/413/etc.
        try:
            payload = response.json()
        except Exception:
            payload = {"status": "error", "error": response.text[:500]}
        return JSONResponse(status_code=response.status_code, content=payload)

    except Exception as e:
        logger.error(f"Document proxy ({downstream_path}) failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": f"Document conversion failed: {str(e)}"},
        )


@app.post("/v1/document/convert")
async def convert_document_endpoint(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Convert any supported document type to Markdown via the privacy service."""
    await verify_api_key(request, credentials)
    return await _proxy_document_endpoint(request, "/document/convert", timeout=600.0)


@app.post("/v1/document/convert-and-anonymize")
async def convert_and_anonymize_document_endpoint(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Atomic convert + smart-anonymize via the privacy service.

    Bridge does not persist the returned mapping — apps store it themselves.
    """
    await verify_api_key(request, credentials)
    return await _proxy_document_endpoint(
        request, "/document/convert-and-anonymize", timeout=900.0
    )


@app.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """Proxy OpenAI Whisper audio transcription. Requires OPENAI_API_KEY in Bridge env."""
    await verify_api_key(request, credentials)

    openai_api_key = os.getenv("OPENAI_API_KEY")
    if not openai_api_key:
        # Config issue — surface as 500 config_error (non-retryable).
        raise BridgeError(config_error(
            detail="OPENAI_API_KEY not configured on Bridge — contact admin"
        ))

    try:
        form = await request.form()
        audio_file = form.get("file")
        if not audio_file:
            raise HTTPException(status_code=400, detail="No file uploaded. Send audio as multipart/form-data with field name 'file'.")

        filename = getattr(audio_file, "filename", "audio.mp3") or "audio.mp3"
        audio_bytes = await audio_file.read()
        content_type = getattr(audio_file, "content_type", "audio/mpeg") or "audio/mpeg"

        # Collect optional parameters
        model = form.get("model", "whisper-1")
        language = form.get("language")
        response_format = form.get("response_format", "json")
        prompt = form.get("prompt")
        temperature = form.get("temperature")

        # Build multipart fields for OpenAI
        files = {"file": (filename, audio_bytes, content_type)}
        data = {"model": model, "response_format": response_format}
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt
        if temperature:
            data["temperature"] = temperature

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {openai_api_key}"},
                files=files,
                data=data,
            )
            response.raise_for_status()
            return JSONResponse(content=response.json(), status_code=response.status_code)

    except httpx.HTTPStatusError as e:
        logger.error(f"OpenAI Whisper API error: {e.response.status_code} {e.response.text}")
        raise HTTPException(status_code=e.response.status_code, detail=f"OpenAI Whisper error: {e.response.text}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Audio transcription proxy failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Audio transcription failed: {str(e)}")


@app.get("/v1/auth/status")
@rate_limit_endpoint("auth")
async def get_auth_status(request: Request):
    """Get Claude Code authentication status and backend availability."""
    from src.auth import auth_manager

    auth_info = get_claude_code_auth_info()
    active_api_key = auth_manager.get_api_key()

    # Check Bedrock availability
    bedrock_valid, bedrock_info = bedrock_credential_manager.validate()

    return {
        "claude_code_auth": auth_info,
        "backends": {
            "anthropic": {
                "available": auth_info["status"]["valid"],
                "method": auth_info["method"]
            },
            "bedrock": {
                "available": bedrock_valid,
                "region": bedrock_info.get("region"),
                "errors": bedrock_info.get("errors", []) if not bedrock_valid else []
            }
        },
        "server_info": {
            "api_key_required": bool(active_api_key),
            "api_key_source": "environment" if os.getenv("API_KEY") else ("runtime" if runtime_api_key else "none"),
            "version": "1.0.0"
        }
    }


@app.get("/v1/sessions/stats")
async def get_session_stats(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """Get session manager statistics."""
    stats = session_manager.get_stats()
    return {
        "session_stats": stats,
        "cleanup_interval_minutes": session_manager.cleanup_interval_minutes,
        "default_ttl_hours": session_manager.default_ttl_hours
    }


@app.get("/v1/sessions")
async def list_sessions(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """List all active sessions."""
    sessions = session_manager.list_sessions()
    return SessionListResponse(sessions=sessions, total=len(sessions))


@app.get("/v1/sessions/{session_id}")
async def get_session(
    session_id: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """Get information about a specific session."""
    session = session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    return session.to_session_info()


@app.delete("/v1/sessions/{session_id}")
async def delete_session(
    session_id: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """Delete a specific session."""
    deleted = session_manager.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")

    return {"message": f"Session {session_id} deleted successfully"}


# ============================================================================
# CLI Session Management Endpoints (for /sc:research tracking and cancellation)
# ============================================================================

@app.get("/v1/cli-sessions")
async def list_cli_sessions(
    status: Optional[str] = None,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """List all CLI sessions (running Claude CLI calls like /sc:research).

    Args:
        status: Optional filter by status (running, completed, cancelled, failed)
    """
    from src.cli_session_manager import cli_session_manager

    logger.debug(f"CLI session list requested (status_filter={status})")

    sessions = cli_session_manager.list_sessions(status_filter=status)
    total = len(sessions)

    # Warn if returning large number of sessions
    if total > 100:
        logger.warning(f"Large CLI session list returned: {total} sessions")

    logger.debug(f"Returned {total} CLI sessions")

    return {
        "cli_sessions": sessions,
        "total": total
    }


@app.get("/v1/cli-sessions/stats")
async def get_cli_session_stats(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """Get CLI session statistics."""
    from src.cli_session_manager import cli_session_manager

    stats = cli_session_manager.get_stats()
    return {"cli_session_stats": stats}


@app.get("/v1/cli-sessions/{cli_session_id}")
async def get_cli_session(
    cli_session_id: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """Get information about a specific CLI session."""
    from src.cli_session_manager import cli_session_manager

    session = cli_session_manager.get_session(cli_session_id)
    if not session:
        raise HTTPException(status_code=404, detail="CLI session not found")

    return session.to_dict()


@app.delete("/v1/cli-sessions/{cli_session_id}")
async def cancel_cli_session(
    cli_session_id: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """Cancel a running CLI session (stops the Claude CLI call)."""
    from src.cli_session_manager import cli_session_manager

    logger.info(f"CLI session cancellation requested: {cli_session_id}")

    cancelled = cli_session_manager.cancel_session(cli_session_id)
    if not cancelled:
        session = cli_session_manager.get_session(cli_session_id)
        if not session:
            logger.warning(f"CLI session not found: {cli_session_id}")
            # Log failed cancellation event
            EventLogger.log_session_event(
                event_subtype="cli_cancel_failed",
                session_id=cli_session_id,
                details={"reason": "not_found"}
            )
            raise HTTPException(status_code=404, detail="CLI session not found")
        else:
            logger.warning(f"Cannot cancel CLI session (status={session.status}): {cli_session_id}")
            # Log failed cancellation event
            EventLogger.log_session_event(
                event_subtype="cli_cancel_failed",
                session_id=cli_session_id,
                details={"reason": "invalid_status", "current_status": session.status}
            )
            raise HTTPException(
                status_code=400,
                detail=f"Cannot cancel session in status: {session.status}"
            )

    logger.info(f"CLI session cancelled successfully: {cli_session_id}")
    # Log successful cancellation event
    EventLogger.log_session_event(
        event_subtype="cli_cancelled",
        session_id=cli_session_id,
        details={"action": "user_requested"}
    )

    return {"message": f"CLI session {cli_session_id} cancelled successfully"}


@app.delete("/v1/cli-sessions")
async def cleanup_old_cli_sessions(
    max_age_hours: int = 24,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """Cleanup old completed/cancelled/failed CLI sessions.

    Args:
        max_age_hours: Remove sessions older than this (default: 24 hours)
    """
    from src.cli_session_manager import cli_session_manager

    logger.info(f"CLI session cleanup requested (max_age_hours={max_age_hours})")

    removed = cli_session_manager.cleanup_old_sessions(max_age_hours=max_age_hours)

    if removed > 0:
        logger.info(f"Cleaned up {removed} old CLI sessions (age > {max_age_hours}h)")
        EventLogger.log_session_event(
            event_subtype="cli_cleanup",
            session_id="system",
            details={"removed_count": removed, "max_age_hours": max_age_hours}
        )
    else:
        logger.info(f"No old CLI sessions to clean up (age > {max_age_hours}h)")

    return {
        "message": f"Cleaned up {removed} old CLI sessions",
        "removed_count": removed
    }


# ============================================================================
# Performance Metrics Endpoint
# ============================================================================

@app.get("/v1/metrics")
async def get_performance_metrics(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """
    Get performance metrics for monitoring.

    Returns:
        Performance summary including request counts, average duration,
        slow requests, and per-endpoint statistics.
    """
    from src.middleware.performance_monitor import metrics

    summary = metrics.get_summary()

    return {
        "metrics": summary,
        "thresholds": {
            "non_tool": {
                "slow_request": "5.0s",
                "very_slow_request": "10.0s"
            },
            "tool_enabled": {
                "slow_request": "30.0s",
                "very_slow_request": "60.0s"
            }
        },
        "note": "Metrics are cumulative since server start. Tool-aware thresholds separate tool vs non-tool requests."
    }


# ============================================================================
# Prompt Performance Metrics (per app + agent)
# ============================================================================

@app.get("/v1/metrics/prompt-performance")
async def get_prompt_performance(
    hours: int = 24,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """
    Get per-prompt-type performance stats (duration, error rate, tokens).

    Grouped by app_id + agent_id so you can see which AI function
    is slow or broken.

    Query params:
        hours: Time window (default 24, 0 = all time)
    """
    from src.middleware.prompt_metrics import get_prompt_metrics

    hours = max(hours, 0)  # 0 = all time
    collector = get_prompt_metrics()
    return collector.get_stats(hours=hours)


@app.get("/v1/metrics/prompt-performance/timeline")
async def get_prompt_timeline(
    app_id: str,
    agent_id: str,
    hours: int = 24,
    bucket_minutes: int = 60,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """
    Get timeline data for a specific app+agent combo (for charts).

    Query params:
        app_id: Application identifier (e.g., "werking-report")
        agent_id: Agent identifier (e.g., "gutachten-generate")
        hours: Time window (default 24)
        bucket_minutes: Bucket size in minutes (default 60)
    """
    from src.middleware.prompt_metrics import get_prompt_metrics

    hours = min(max(hours, 1), 168)
    bucket_minutes = min(max(bucket_minutes, 5), 360)
    collector = get_prompt_metrics()
    return collector.get_timeline(app_id=app_id, agent_id=agent_id, hours=hours, bucket_minutes=bucket_minutes)


@app.get("/v1/metrics/prompt-performance/calls")
async def get_prompt_calls(
    hours: int = 24,
    limit: int = 200,
    app_id: Optional[str] = None,
    user_id: Optional[str] = None,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """
    Get raw individual prompt calls (newest first) for activity feeds.

    Unlike /prompt-performance which returns aggregated stats, this returns
    individual call records with full attribution: app_id, user_id, model,
    tokens, duration, status.

    Query params:
        hours: Time window (default 24, 0 = all time)
        limit: Max entries (default 200, max 1000)
        app_id: Filter by app (optional)
        user_id: Filter by user (optional)
    """
    from src.middleware.prompt_metrics import get_prompt_metrics

    collector = get_prompt_metrics()
    return collector.get_recent_calls(
        hours=max(hours, 0),
        limit=min(limit, 1000),
        app_id=app_id,
        user_id=user_id,
    )


@app.get("/v1/metrics/throughput")
async def get_throughput(
    hours: int = 24,
    bucket_seconds: int = 60,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """
    Per-worker request/token throughput timeline + empirical rate-limit ceiling.

    For each worker returns a time-bucketed series of:
      rpm, in_tpm, out_tpm, err_count, had_429, had_503

    Plus an empirical "ceiling" object per worker that derives a safe
    throttle setting from observed throughput vs error events:
      - first_error_*       : throughput when errors first started
      - max_clean_*         : highest sustained throughput with zero errors
      - recommendation_*    : suggested bridge throttle (safety margin applied)

    Query params:
        hours          : lookback window (default 24, max 168)
        bucket_seconds : bucket size (default 60, min 10, max 600)
    """
    from src.middleware.prompt_metrics import get_prompt_metrics

    hours = min(max(hours, 1), 168)
    bucket_seconds = min(max(bucket_seconds, 10), 600)
    collector = get_prompt_metrics()
    return collector.get_throughput(hours=hours, bucket_seconds=bucket_seconds)


@app.get("/v1/metrics/usage-breakdown")
async def get_usage_breakdown(
    hours: int = 24,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """
    Get token/cost breakdown per app, per user, per model.

    Returns hierarchical data suitable for Sankey diagrams, cost analysis,
    and per-user/per-app dashboards.

    Query params:
        hours: Time window (default 24, 0 = all time)
    """
    from src.middleware.prompt_metrics import get_prompt_metrics

    collector = get_prompt_metrics()
    return collector.get_usage_breakdown(hours=max(hours, 0))


# ============================================================================
# Persistent Request Log (all HTTP requests, stored on disk)
# ============================================================================

@app.get("/v1/metrics/request-log")
async def get_request_log_endpoint(
    hours: int = 24,
    endpoint: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 200,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """
    Get persistent request log from all workers.

    Query params:
        hours: Time window (default 24, 0 = all time)
        endpoint: Filter by endpoint substring (e.g., "/v1/chat")
        status: Filter by "success" or "error"
        limit: Max recent entries (default 200)
    """
    from src.middleware.bridge_metrics_store import get_request_log
    return get_request_log().query(
        hours=max(hours, 0),
        endpoint_filter=endpoint,
        status_filter=status,
        limit=min(limit, 1000),
    )


# ============================================================================
# CC-Usage Snapshots (account limit history)
# ============================================================================

@app.get("/v1/metrics/cc-usage-history")
async def get_cc_usage_history(
    hours: int = 168,
    limit: int = 500,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """
    Get Claude Code account usage snapshot history.

    Query params:
        hours: Time window (default 168 = 7 days, 0 = all time)
        limit: Max snapshots (default 500)
    """
    from src.middleware.bridge_metrics_store import get_cc_usage_store
    return get_cc_usage_store().get_history(
        hours=max(hours, 0),
        limit=min(limit, 2000),
    )


@app.post("/v1/metrics/cc-usage-snapshot")
async def save_cc_usage_snapshot(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """
    Save a CC usage snapshot (called by CUI scraper after each scrape).

    Body: { "accounts": [ { account, plan, currentSession, ... } ] }
    """
    body = await request.json()
    accounts = body.get("accounts", [])
    if not accounts:
        raise HTTPException(status_code=400, detail="No accounts data provided")

    from src.middleware.bridge_metrics_store import get_cc_usage_store
    get_cc_usage_store().record_snapshot(accounts)
    return {"status": "ok", "accounts_saved": len(accounts)}


@app.get("/v1/metrics/queue-forecast")
async def get_queue_forecast(
    window: int = 60,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """
    Short-term load + queue forecast based on rolling in-memory metrics
    AND the latest CC-usage snapshot.

    Query params:
        window: aggregation window in seconds (default 60, max 600)

    Returns per-worker arrival/completion rates, in-flight counts, and
    a `forecast` block with simple drain/saturation estimates.

    NOTE: Rolling metrics live ONLY in this worker process (the one that
    serves this request). The values are best read while the request flows
    through nginx in a round-robin fashion — over a few requests, all four
    workers will report. The CC-usage block is global (shared snapshot store).
    """
    from src.middleware.rolling_metrics import get_rolling_metrics
    from src.middleware.bridge_metrics_store import get_cc_usage_store

    summary = get_rolling_metrics().get_summary(window_seconds=window)

    # Latest CC-usage snapshot (per account / per worker via account map)
    latest_snapshot: Optional[Dict[str, Any]] = None
    try:
        history = get_cc_usage_store().get_history(hours=1, limit=1)
        snapshots = history.get("snapshots") or []
        if snapshots:
            latest_snapshot = snapshots[0]
    except Exception as e:
        logger.warning(f"queue-forecast: cannot read cc-usage-history: {e}")

    account_to_worker = {
        "engelmann": "worker1",
        "office":    "worker2",
        "gmail":     "worker3",
        "werking":   "worker4",
    }
    worker_limits: Dict[str, Dict[str, Any]] = {}
    if latest_snapshot:
        for acc in latest_snapshot.get("accounts", []):
            name = acc.get("account", "")
            worker = account_to_worker.get(name)
            if not worker:
                continue
            weekly = acc.get("weeklyAllModels", {}).get("percent", 0) or 0
            session = acc.get("currentSession", {}).get("percent", 0) or 0
            worker_limits[worker] = {
                "account": name,
                "weekly_percent": weekly,
                "session_percent": session,
                "active": (weekly < 95) and (session < 95),
            }

    # Compute simple forecast
    totals = summary["totals"]
    workers = summary["workers"]
    in_flight_total = totals.get("in_flight", 0)
    completions_per_min = totals.get("completions_per_min", 0)
    arrivals_per_min = totals.get("arrivals_per_min", 0)
    drain_per_s = round(completions_per_min / 60.0, 3)
    arrival_per_s = round(arrivals_per_min / 60.0, 3)

    # ETA empty: in-flight count / drain-rate
    eta_empty_s = (
        round(in_flight_total / drain_per_s, 1)
        if drain_per_s > 0 and in_flight_total > 0 else
        (0 if in_flight_total == 0 else None)
    )

    # Active workers (not rate-limited per latest snapshot)
    active_workers = sum(1 for w in worker_limits.values() if w["active"]) \
        if worker_limits else None

    # Net: positive = queue building up, negative = draining
    net_per_s = round(arrival_per_s - drain_per_s, 3)
    backlog_trend = (
        "growing" if net_per_s > 0.05 else
        "draining" if net_per_s < -0.05 else
        "stable"
    )

    # Adaptive token-budget limiter snapshot (this worker only — each worker
    # auto-tunes independently because each owns its own Anthropic account).
    adaptive_limiter_snapshot: Optional[Dict[str, Any]] = None
    try:
        adaptive_limiter_snapshot = get_adaptive_limiter().snapshot()
    except Exception as _e:
        logger.debug(f"queue-forecast: adaptive limiter snapshot unavailable: {_e}")

    # Risk scoring per worker — token-utilization-based (matches adaptive_limiter reality).
    # For SELF worker we know the exact utilization_pct from adaptive_limiter.
    # For other workers rolling_metrics is per-process (won't see them), so they rarely
    # appear here; if they do, we fall back to a count-based heuristic calibrated to
    # the real token-concurrency era (dozens of concurrent requests are normal).
    self_worker = summary.get("worker_self")
    self_util_pct: Optional[float] = None
    try:
        if adaptive_limiter_snapshot:
            self_util_pct = float(adaptive_limiter_snapshot.get("utilization_pct", 0.0))
    except Exception:
        self_util_pct = None

    def _classify_by_util(util_pct: float) -> str:
        if util_pct >= 90:
            return "saturated"
        if util_pct >= 70:
            return "busy"
        if util_pct > 0:
            return "ok"
        return "idle"

    saturation: Dict[str, str] = {}
    for w_name, w in workers.items():
        in_flight = w.get("in_flight", 0)
        comp_pm = w.get("completions_per_min", 0)
        rl_hits = w.get("rate_limit_hits", 0)
        if rl_hits > 0:
            saturation[w_name] = "rate_limited"
        elif w_name == self_worker and self_util_pct is not None:
            saturation[w_name] = _classify_by_util(self_util_pct)
        elif in_flight >= 25:
            saturation[w_name] = "saturated"
        elif in_flight >= 15:
            saturation[w_name] = "busy"
        elif comp_pm > 0 or in_flight > 0:
            saturation[w_name] = "ok"
        else:
            saturation[w_name] = "idle"

    rate_limit_risk = (
        "high" if totals.get("rate_limit_hits", 0) > 0 or (active_workers is not None and active_workers <= 1) else
        "medium" if (active_workers is not None and active_workers == 2) else
        "low"
    )

    return {
        "window_seconds": summary["window_seconds"],
        "now": summary["now"],
        "worker_self": summary["worker_self"],
        "workers": workers,
        "worker_limits": worker_limits,
        "saturation": saturation,
        "totals": totals,
        "forecast": {
            "in_flight_total": in_flight_total,
            "drain_rate_per_s": drain_per_s,
            "arrival_rate_per_s": arrival_per_s,
            "net_per_s": net_per_s,
            "backlog_trend": backlog_trend,
            "eta_empty_s": eta_empty_s,
            "active_workers": active_workers,
            "rate_limit_risk": rate_limit_risk,
        },
        "adaptive_limiter": adaptive_limiter_snapshot,
        "note": (
            "Rolling-metrics are per-process; only THIS worker's view is shown. "
            "Account-limit data is global (shared snapshot store). "
            "adaptive_limiter is per-worker; collect from all 4 workers to see "
            "the full bridge picture."
        ),
    }


@app.exception_handler(BridgeError)
async def bridge_error_handler(request: Request, exc: BridgeError):
    """Unwrap BridgeError → its carried structured response. The envelope is
    pre-built by helpers in src/middleware/bridge_error.py so apps see a
    consistent `{error: {source, bridge_type, retry_after_s, ...}}` shape.

    Cross-bridge escape hatch: when the BridgeError was raised by the adaptive
    limiter (account_exhausted / throttle / queue_timeout on /v1/chat/completions)
    AND a usable fallback tier exists AND we have the cached request body, we
    try the fallback chain (terminating in `bridge-prod-emergency`) BEFORE
    surfacing the envelope. This closes the gap where dep-raised BridgeErrors
    never reached the route handler's own fallback path.
    """
    # Only the chat-completions path is a candidate for cross-bridge fallback.
    # Other endpoints (research, health, metrics) keep the original envelope.
    try:
        path = request.url.path
        is_chat_completions = path.endswith("/v1/chat/completions")
        cached_body = getattr(request.state, "cached_body_dict", None)
        if not is_chat_completions or not cached_body or cached_body.get("stream"):
            return exc.response

        # Only attempt for retryable bridge-side errors (not, e.g., auth failures).
        err_body = getattr(exc, "response", None)
        err_content = getattr(err_body, "body", None) if err_body else None
        # We pulled the body from the JSONResponse to introspect source/type.
        import json as _json
        try:
            err_obj = _json.loads(err_content) if err_content else {}
        except Exception:
            err_obj = {}
        err_fields = (err_obj.get("error") or {})
        err_source = err_fields.get("source", "")
        err_type = err_fields.get("bridge_type", "")
        # Only these dep-raised categories warrant cross-bridge fallback.
        if not (
            (err_source == "bridge_account" and err_type == "account_exhausted")
            or (err_source == "bridge_internal" and err_type in ("throttle", "queue_timeout"))
        ):
            return exc.response

        # First: try a cross-worker retry on this same bridge before falling
        # over to a different provider tier. The current worker's local
        # admission control rejected the request, but other workers on this
        # bridge may still have capacity. Bounded to 2 attempts via the same
        # X-Bridge-Retry-Count header used by rate_limit_handler.
        self_worker = os.getenv("INSTANCE_NAME", "unknown")
        cross_worker_response = await _cross_worker_retry(request, self_worker)
        if cross_worker_response is not None:
            return cross_worker_response

        from src.providers.fallback import (
            get_fallback_tiers as _fb_tiers,
            record_failure as _fb_fail,
            record_success as _fb_ok,
            FALLBACK_DELAY_SECONDS as _fb_delay,
        )
        import asyncio as _fb_asyncio

        primary_tier = cached_body.get("provider_tier") or "claude-premium"
        _fb_fail(primary_tier, f"dep_bridge_error: {err_type}")
        fallback_chain = _fb_tiers(primary_tier)[1:]  # skip primary
        if not fallback_chain:
            return exc.response  # nothing to try

        # Build ChatCompletionRequest from cached body so call_openai_compatible
        # has the right shape. Any validation error → fall back to envelope.
        try:
            _req = ChatCompletionRequest(**cached_body)
        except Exception as _rebuild_err:
            logger.warning(
                f"Cross-bridge fallback: could not rebuild request model: {_rebuild_err}"
            )
            return exc.response

        for _ft in fallback_chain:
            try:
                logger.warning(
                    f"⚠️ Adaptive limiter raised {err_type} on {primary_tier}. "
                    f"Cross-bridge fallback (dep-level): {_ft}"
                )
                await _fb_asyncio.sleep(_fb_delay)
                _fb_cfg = resolve_backend_config(
                    backend=BackendType.ANTHROPIC,
                    model=_req.model,
                    privacy=_req.privacy or PrivacyMode.AUTO,
                    bedrock_region=_req.bedrock_region,
                    provider_tier=_ft,
                )
                if _fb_cfg.backend != BackendType.OPENAI_COMPATIBLE:
                    continue  # Only OPENAI_COMPATIBLE tiers are fallback-callable here
                from src.providers.openai_compatible import call_openai_compatible
                _fb_resp = await call_openai_compatible(
                    _req,
                    _fb_cfg.provider_base_url,
                    _fb_cfg.provider_api_key,
                    model_override=_fb_cfg.provider_model,
                )
                _fb_ok(_ft)
                logger.info(
                    f"✅ Cross-bridge fallback {_ft} succeeded "
                    f"(dep-level: {err_type})"
                )
                _fb_resp["x_fallback"] = {
                    "used": True,
                    "original_provider": primary_tier,
                    "fallback_provider": _ft,
                    "original_error": f"{err_type} from adaptive limiter",
                    "trigger": "dep_bridge_error",
                }
                return JSONResponse(content=_fb_resp)
            except Exception as _fb_err:
                _fb_fail(_ft, str(_fb_err)[:200])
                logger.warning(f"⚠️ Cross-bridge fallback {_ft} failed: {_fb_err}")
                continue

        logger.error(
            f"❌ All cross-bridge fallbacks exhausted (dep-level {err_type})"
        )
    except Exception as _handler_err:
        # Any error inside the fallback helper itself → fall through to envelope.
        logger.error(f"bridge_error_handler cross-fallback error: {_handler_err}")
    return exc.response


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """
    Format HTTP exceptions as OpenAI-style errors WITH bridge discriminators.

    Registered against the Starlette base class so this ALSO catches 404s
    raised by the router itself (missing route) and 405s (method not allowed),
    not just fastapi.HTTPException instances from our own `raise` statements.

    Existing handlers in this file still `raise HTTPException(...)` for various
    reasons (auth, validation, upstream errors). We don't want callers to
    confuse "Anthropic returned 502" with "bridge had an internal problem", so
    every HTTPException is annotated as `source: bridge_internal` here. Code
    paths that know better (e.g. upstream proxy) should `raise BridgeError(
    upstream_error(...))` instead so the source is correctly tagged.
    """
    detail = exc.detail
    # If detail is already a dict with our envelope shape, pass through.
    if isinstance(detail, dict) and "source" in detail:
        return JSONResponse(status_code=exc.status_code, content={"error": detail})
    # If detail is a dict with arbitrary fields, preserve them as extra.
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("error") or str(detail)
        extra = {k: v for k, v in detail.items() if k not in ("message", "error")}
        return bridge_error(
            source=SOURCE_BRIDGE_INTERNAL,
            error_type=TYPE_INTERNAL,
            message=str(message),
            status_code=exc.status_code,
            retry_after_s=detail.get("retry_after_seconds") or detail.get("retry_after_s"),
            extra=extra,
        )
    # Plain string detail
    return bridge_error(
        source=SOURCE_BRIDGE_INTERNAL,
        error_type=TYPE_INTERNAL,
        message=str(detail),
        status_code=exc.status_code,
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """
    Last-resort catch-all: any unhandled exception in a dependency or handler
    becomes a structured envelope instead of Starlette's raw
    `Internal Server Error` plain-text 500. This is critical — without it,
    apps would see plain text on edge cases (malformed body, pre-validator
    crashes, etc.) and could not programmatically discriminate bridge errors.

    Classification reuses the upstream/network/internal marker logic from
    `classify_exception`, so e.g. a bubbled-up `httpx.ConnectTimeout` still
    ends up tagged `upstream_network` rather than `bridge_internal`.
    """
    logger.exception(
        f"Unhandled exception on {request.method} {request.url.path}: {exc}"
    )
    return classify_exception(exc)


# ============================================================================
# Account Pool State — per-worker limiter snapshot for nginx Lua pool-router
# ============================================================================

@app.get("/v1/metrics/account-pool-state")
async def get_account_pool_state():
    """Exposes this worker's adaptive limiter state for the nginx pool-router."""
    import time as _time
    from src.middleware.adaptive_limiter import (
        SAFETY_MARGIN_PCT,
        SHRINK_TRIGGER_SEC,
        WEEKLY_PREDICTIVE_THROTTLE_ENABLED,
        _WORKER_ACCOUNT_MAP,
        _weekly_budget_multiplier,
    )
    lim = get_adaptive_limiter()
    lim._refresh_account_usage()
    state = lim.state
    inflight = lim._current_inflight_tokens()
    acct = lim._account_usage

    cap = state.cap_tokens
    safety_cap = int(cap * SAFETY_MARGIN_PCT / 100)

    # Real headroom must be the minimum of three independent ceilings:
    #   1) inflight (local Bridge admit budget — sinks as concurrent load rises)
    #   2) Anthropic 5h session window  (session_percent reports its consumption)
    #   3) Anthropic weekly plan window (weekly_percent reports its consumption)
    # The previous formula divided (safety_cap - inflight) by cap and reported
    # SAFETY_MARGIN_PCT (≈85) for every idle worker. That's a constant of the
    # admit-threshold formula, not a capacity signal — so the pool router saw
    # all four idle workers as identical and routed every request to the
    # alphabetically-first account (decision_counter showed 99.6% on worker1).
    # Using the true min lets the router rank workers by which Anthropic
    # account is actually freshest, even when the Bridge itself is idle.
    session_pct = float(acct.get("session_pct", 0.0))
    weekly_pct = float(acct.get("weekly_pct", 0.0))

    inflight_headroom_pct = (
        max(0.0, (safety_cap - inflight) * 100.0 / safety_cap) if safety_cap > 0 else 0.0
    )
    session_remaining_pct = max(0.0, 100.0 - session_pct)
    weekly_remaining_pct = max(0.0, 100.0 - weekly_pct)
    headroom_pct = round(
        min(inflight_headroom_pct, session_remaining_pct, weekly_remaining_pct),
        1,
    )
    headroom = int(safety_cap * headroom_pct / 100.0) if safety_cap > 0 else 0

    now = _time.time()

    # Adaptive-limiter cooldown (token-budget based)
    adaptive_cooldown_s = 0
    if state.last_rate_limit_ts is not None:
        elapsed = now - state.last_rate_limit_ts
        adaptive_cooldown_s = max(0, int(SHRINK_TRIGGER_SEC - elapsed))

    # RateLimitTracker: soft/hard penalty from rate_limit_event stream
    worker_id = lim.worker
    if not isinstance(worker_id, str):
        raise ValueError(f"lim.worker must be str, got {type(worker_id)!r}")

    try:
        tracker_remaining = rate_limit_tracker.get_retry_after(worker_id)  # None or int seconds
        tracker_is_limited = rate_limit_tracker.is_rate_limited(worker_id)
        tracker_is_hard = rate_limit_tracker.is_hard_limited(worker_id)
    except Exception as exc:
        logger.error(f"rate_limit_tracker query failed for worker {worker_id!r}: {exc}")
        raise

    # Capacity lock — deterministic hold based on Anthropic-provided reset_at
    from src.middleware.capacity_lock import get_capacity_lock as _get_cap_lock
    cap_lock = _get_cap_lock()
    cap_lock_remaining_s = cap_lock.remaining_s(worker_id)
    cap_lock_info = cap_lock.get_lock_info(worker_id)
    cap_lock_reason = cap_lock_info["reason"] if cap_lock_info else None

    # Merged cooldown: max of token-budget cooldown, soft/hard penalty, and capacity lock
    soft_penalty_remaining_s = tracker_remaining if tracker_remaining is not None else 0
    cooldown_remaining_s = max(adaptive_cooldown_s, soft_penalty_remaining_s, cap_lock_remaining_s)

    # Merged last_rate_limit_ts: newer of adaptive ts and tracker event start approximation
    # tracker start ≈ (now + remaining) - window, where window = SOFT_PENALTY or MAX_COOLDOWN
    merged_last_rate_limit_ts = state.last_rate_limit_ts
    if tracker_remaining is not None:
        window = (
            rate_limit_tracker.MAX_COOLDOWN_SECONDS
            if tracker_is_hard
            else rate_limit_tracker.SOFT_PENALTY_SECONDS
        )
        tracker_last_ts = now + tracker_remaining - window
        if merged_last_rate_limit_ts is None or tracker_last_ts > merged_last_rate_limit_ts:
            merged_last_rate_limit_ts = tracker_last_ts

    account_name = _WORKER_ACCOUNT_MAP.get(worker_id, worker_id)

    budget_multiplier = _weekly_budget_multiplier(weekly_pct, session_pct)
    effective_cap_tokens = int(safety_cap * budget_multiplier)

    return {
        "ts": int(now),
        "worker": worker_id,
        "account": account_name,
        "session_percent": round(session_pct, 1),
        "weekly_percent": round(weekly_pct, 1),
        "adaptive_cap_tokens": cap,
        "current_in_flight_tokens": inflight,
        "headroom_tokens": headroom,
        "headroom_percent": headroom_pct,
        "predictive_throttle_enabled": WEEKLY_PREDICTIVE_THROTTLE_ENABLED,
        "budget_multiplier": round(budget_multiplier, 3),
        "effective_cap_tokens": effective_cap_tokens,
        "last_rate_limit_ts": merged_last_rate_limit_ts,
        "cooldown_remaining_s": cooldown_remaining_s,
        "available": (
            not cap_lock.is_locked(worker_id)
            and session_pct < 95.0
            and headroom > 0
            and not tracker_is_limited
        ),
        "soft_penalty_remaining_s": soft_penalty_remaining_s,
        "is_hard_limited": tracker_is_hard,
        "capacity_lock_remaining_s": cap_lock_remaining_s,
        "capacity_lock_reason": cap_lock_reason,
    }


def find_available_port(start_port: int = 8000, max_attempts: int = 10) -> int:
    """Find an available port starting from start_port."""
    import socket
    
    for port in range(start_port, start_port + max_attempts):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        try:
            result = sock.connect_ex(('127.0.0.1', port))
            if result != 0:  # Port is available
                return port
        except Exception:
            return port
        finally:
            sock.close()
    
    raise RuntimeError(f"No available ports found in range {start_port}-{start_port + max_attempts - 1}")


def run_server(port: int = None):
    """Run the server - used as Poetry script entry point."""
    import uvicorn
    import socket
    
    # Handle interactive API key protection
    global runtime_api_key
    runtime_api_key = prompt_for_api_protection()
    
    # Priority: CLI arg > ENV var > default
    if port is None:
        port = int(os.getenv("PORT", "8000"))
    preferred_port = port
    
    try:
        # Try the preferred port first
        uvicorn.run(app, host="0.0.0.0", port=preferred_port)
    except OSError as e:
        if "Address already in use" in str(e) or e.errno == 48:
            logger.warning(f"Port {preferred_port} is already in use. Finding alternative port...")
            try:
                available_port = find_available_port(preferred_port + 1)
                logger.info(f"Starting server on alternative port {available_port}")
                print(f"\n🚀 Server starting on http://localhost:{available_port}")
                print(f"📝 Update your client base_url to: http://localhost:{available_port}/v1")
                uvicorn.run(app, host="0.0.0.0", port=available_port)
            except RuntimeError as port_error:
                logger.error(f"Could not find available port: {port_error}")
                print(f"\n❌ Error: {port_error}")
                print("💡 Try setting a specific port with: PORT=9000 poetry run python main.py")
                raise
        else:
            raise


if __name__ == "__main__":
    import sys
    
    # Simple CLI argument parsing for port
    port = None
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
            print(f"Using port from command line: {port}")
        except ValueError:
            print(f"Invalid port number: {sys.argv[1]}. Using default.")
    
    run_server(port)