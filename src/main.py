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
    ResearchResponse
)
from src.claude_cli import ClaudeCodeCLI, WorkerUnavailableError
from src.message_adapter import MessageAdapter
from src.vision_provider import VisionProvider, get_vision_provider
from src.routing.vision_router import check_and_route_vision, prepare_messages_for_vision, has_vision_content
from src.auth import verify_api_key, security, validate_claude_code_auth, get_claude_code_auth_info
from src.parameter_validator import ParameterValidator, CompatibilityReporter
from src.model_registry import (
    get_models_for_api,
    resolve_model,
    ModelResolutionError,
    get_all_model_ids
)
from src.file_discovery import FileDiscoveryService
from src.session_manager import session_manager
from src.privacy import get_privacy_middleware
from src.tenant import (
    TenantMiddleware,
    get_tenant_from_request,
    get_privacy_mode_from_request,
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

# Request limiting for memory protection
from src.request_limiter import get_limiter, RequestLimiterMiddleware

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

    yield
    
    # Cleanup on shutdown
    logger.info("Shutting down session manager...")
    session_manager.shutdown()


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
# TEMPORARILY DISABLED: Python 3.13 + Starlette 0.46 BaseHTTPMiddleware bug
# app.add_middleware(PerformanceMonitorMiddleware)

# Add request limiting middleware (AFTER performance monitoring)
# Protects against memory exhaustion from too many concurrent requests
# BaseHTTPMiddleware but safe (does NOT read request body)
max_concurrent = int(os.getenv("MAX_CONCURRENT_REQUESTS", "3"))
memory_threshold = float(os.getenv("MEMORY_THRESHOLD_PERCENT", "90.0"))
request_limiter = get_limiter(max_concurrent=max_concurrent, memory_threshold=memory_threshold)
# TEMPORARILY DISABLED: Python 3.13 + Starlette 0.46 BaseHTTPMiddleware bug
# app.add_middleware(RequestLimiterMiddleware, limiter=request_limiter)

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
    
    error_response = {
        "error": {
            "message": "Request validation failed - the request body doesn't match the expected format",
            "type": "validation_error", 
            "code": "invalid_request_error",
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
        proxy_next_upstream error timeout http_500 http_502 http_503 http_504;
        proxy_next_upstream_tries 2;
    """
    logger.warning(
        f"🔄 Worker unavailable, returning 503 for Nginx failover",
        extra={
            "path": str(request.url),
            "method": request.method,
            "error": str(exc)
        }
    )

    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "message": "Worker temporarily unavailable, request will be retried on another worker",
                "type": "service_unavailable",
                "code": "503",
                "retry": True
            }
        },
        headers={
            "Retry-After": "0",  # Immediate retry OK
            "X-Worker-Failover": "true"
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
    fastapi_request: Optional[Request] = None
) -> AsyncGenerator[str, None]:
    """Generate SSE formatted streaming response with automatic disconnect detection."""
    cli_session_for_disconnect = None  # Track CLI session for disconnect detection
    streaming_started = asyncio.Event()  # Signal when streaming starts (prevents race condition)

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
        privacy_middleware = get_privacy_middleware()
        anonymization_mapping = {}
        privacy_mode = get_privacy_mode_from_request(fastapi_request) if fastapi_request else "full"

        if privacy_middleware.enabled:
            messages_for_anon = [
                {'role': m.role, 'content': m.content}
                for m in all_messages
            ]
            anon_messages, anonymization_mapping = privacy_middleware.anonymize_messages(
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

        async for chunk in claude_cli.run_completion(
            prompt=prompt,
            system_prompt=system_prompt,
            model=claude_options.get('model'),
            max_turns=claude_options.get('max_turns', 10),
            allowed_tools=claude_options.get('allowed_tools'),
            disallowed_tools=claude_options.get('disallowed_tools'),
            stream=True,
            enable_file_discovery=claude_options.get('enable_file_discovery', False)
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
                            filtered_text, deanon_buffer = privacy_middleware.deanonymize_streaming_chunk(
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
                        filtered_content, deanon_buffer = privacy_middleware.deanonymize_streaming_chunk(
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

        # Flush any remaining de-anonymization buffer at end of stream
        if anonymization_mapping and deanon_buffer:
            flushed_content = privacy_middleware.flush_streaming_buffer(deanon_buffer, anonymization_mapping)
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
        
        # Send final chunk with finish reason
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

        yield "data: [DONE]\n\n"

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


@app.post("/v1/chat/completions")
@rate_limit_endpoint("chat")
async def chat_completions(
    request_body: ChatCompletionRequest,
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """OpenAI-compatible chat completions endpoint."""
    import time
    from fastapi.responses import JSONResponse
    start_time = time.time()

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

        error_detail = {
            "message": "Claude Code authentication failed",
            "errors": auth_info.get('errors', []),
            "method": auth_info.get('method', 'none'),
            "help": "Check /v1/auth/status for detailed authentication information"
        }
        raise HTTPException(
            status_code=503,
            detail=error_detail
        )
    
    try:
        request_id = f"chatcmpl-{os.urandom(8).hex()}"

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
                generate_streaming_response(request_body, request_id, claude_headers, fastapi_request=request),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Worker-Instance": worker_instance,
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
                    return response

            except Exception as e:
                logger.error(f"❌ Vision request failed: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"Vision analysis failed: {str(e)}")

            # Continue with normal SDK flow (no vision content)
            # Process messages with session management
            all_messages, actual_session_id = session_manager.process_messages(
                request_body.messages, request_body.session_id
            )

            logger.info(f"Chat completion: session_id={actual_session_id}, total_messages={len(all_messages)}")

            # Privacy: Anonymize user messages before sending to Claude
            # Uses tenant's privacy mode from request.state (set by TenantMiddleware)
            privacy_middleware = get_privacy_middleware()
            anonymization_mapping = {}
            privacy_mode = get_privacy_mode_from_request(request)

            if privacy_middleware.enabled:
                messages_for_anon = [
                    {'role': m.role, 'content': m.content}
                    for m in all_messages
                ]
                anon_messages, anonymization_mapping = privacy_middleware.anonymize_messages(
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
                enable_file_discovery=claude_options.get('enable_file_discovery', False)
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
                raise HTTPException(status_code=500, detail=f"No response from Claude Code: {error_detail}")
            
            # Filter out tool usage and thinking blocks
            assistant_content = MessageAdapter.filter_content(raw_assistant_content)

            # Privacy: De-anonymize response (restore original PII)
            if anonymization_mapping:
                assistant_content = privacy_middleware.deanonymize_response(
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

            # Create response
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
                )
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

            # Worker instance info for multi-worker deployments
            worker_instance = os.getenv("INSTANCE_NAME", "unknown")

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
        raise
    except WorkerUnavailableError:
        # Re-raise to trigger HTTP 503 and Nginx failover
        raise
    except Exception as e:
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

        logger.error(f"Chat completion error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/research", response_model=ResearchResponse)
async def research(
    request_body: ResearchRequest,
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
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
          -H 'Authorization: Bearer test-key' \\
          -d '{
            "query": "Latest AI developments in 2025",
            "model": "claude-sonnet-4-5-20250929",
            "output_path": "/Users/rafael/research/ai_2025.md"
          }'
        ```
    """
    # Verify API key
    await verify_api_key(request, credentials)

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

    try:
        logger.info(
            f"🔬 Research request received",
            extra={
                "query": request_body.query[:100],
                "model": request_body.model,
                "depth": request_body.depth,
                "strategy": request_body.strategy,
                "max_hops": request_body.max_hops,
                "output_path": request_body.output_path
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
            enable_file_discovery=True
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

        # Get file size if available
        file_size_bytes = None
        if output_file and Path(output_file).exists():
            file_size_bytes = Path(output_file).stat().st_size

        return ResearchResponse(
            status="success",
            query=request_body.query,
            model=request_body.model,
            output_file=output_file,
            container_file=container_file,
            execution_time_seconds=round(execution_time, 2),
            file_size_bytes=file_size_bytes,
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


@app.get("/health")
@rate_limit_endpoint("health")
async def health_check(request: Request):
    """Health check endpoint."""
    worker_instance = os.getenv("INSTANCE_NAME", "unknown")
    return {
        "status": "healthy",
        "service": "claude-code-openai-wrapper",
        "worker_instance": worker_instance
    }


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


@app.get("/v1/privacy/status")
async def get_privacy_status():
    """Get privacy middleware status and configuration."""
    middleware = get_privacy_middleware()

    return {
        "privacy": {
            "enabled": middleware.enabled,
            "available": middleware.is_available() if middleware.enabled else False,
            "language": middleware.language,
            "log_detections": middleware.log_detections,
            "supported_entities": middleware.anonymizer.SUPPORTED_ENTITIES if middleware.enabled else [],
            "info": {
                "description": "DSGVO-compliant PII anonymization using Microsoft Presidio",
                "env_vars": {
                    "PRIVACY_ENABLED": "Enable/disable privacy middleware (default: true)",
                    "PRIVACY_LANGUAGE": "Default language for PII detection (default: de)",
                    "PRIVACY_LOG_DETECTIONS": "Log detected entities (default: false)"
                }
            }
        }
    }


@app.get("/v1/auth/status")
@rate_limit_endpoint("auth")
async def get_auth_status(request: Request):
    """Get Claude Code authentication status."""
    from auth import auth_manager
    
    auth_info = get_claude_code_auth_info()
    active_api_key = auth_manager.get_api_key()
    
    return {
        "claude_code_auth": auth_info,
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
    from middleware.performance_monitor import metrics

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


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Format HTTP exceptions as OpenAI-style errors."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.detail,
                "type": "api_error",
                "code": str(exc.status_code)
            }
        }
    )


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