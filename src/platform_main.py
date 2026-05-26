"""
Bridge Platform API — DB-backed identity/admin/billing/budget/activity layer.

This FastAPI application mounts ONLY the DB-backed platform routers.
It has NO dependency on the Claude Code SDK, Node.js, or any LLM-routing code.

Routing contract (enforced by nginx path-based split):
  This service handles:
    /v1/auth/*          login, logout, session
    /v1/users*          user management
    /v1/tenants*        tenant management
    /v1/billing/*       billing, Mollie webhooks
    /v1/budget/*        budget management
    /v1/activity/*      activity log
    /v1/audit/*         audit trail
    /v1/feedback        feedback
    /v1/dev-tokens/*    dev tokens
    /v1/stammdaten/*    master data
    /v1/invoices/*      invoice PDF generation
    /v1/system/*        system admin
    /v1/impersonation/* impersonation
    /v1/app-licenses    app license checks
    /v1/db/health       DB connectivity probe
    /v1/sandbox/conversations/*  conversation persistence

  Workers handle:
    /v1/chat/completions, /v1/messages, /v1/research, /v1/document/convert
    /v1/sandbox/wizard-* (LLM-near wizard routes)

See: /root/orchestrator/workspaces/devops/specs/bridge-platform-split/PLAN.md
"""
import json
import os
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

load_dotenv()

from config.logging_config import setup_logging, get_logger  # noqa: E402

setup_logging(
    log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    enable_diagnostic=False,
    log_to_console=True,
    log_to_file=os.getenv("LOG_TO_FILE", "true").lower() in ("true", "1", "yes"),
    filter_sensitive_data=os.getenv("FILTER_SENSITIVE_DATA", "true").lower() in ("true", "1", "yes"),
)
logger = get_logger(__name__)

# DB layer — required (platform-api has no purpose without a DB)
from src.db.client import init_pool, close_pool, is_db_enabled  # noqa: E402

from src.db.admin_routes import router as admin_db_router  # noqa: E402
from src.identity.routes import router as identity_router  # noqa: E402
from src.identity.self_service import router as self_service_router  # noqa: E402
from src.identity.webhook_config import init_webhook_configs  # noqa: E402
from src.identity.webhook_dispatcher import WebhookDispatcher  # noqa: E402
from src.budget.routes import router as budget_router  # noqa: E402
from src.billing.routes import router as billing_router, pending_orders_router, admin_orders_router, project_credits_router  # noqa: E402
from src.activity.routes import router as activity_router  # noqa: E402
from src.feedback.routes import router as feedback_router  # noqa: E402
from src.audit.routes import router as audit_router  # noqa: E402
from src.dev_tokens.routes import router as dev_tokens_router  # noqa: E402
from src.stammdaten.routes import router as stammdaten_router  # noqa: E402
from src.invoices.routes import router as invoices_router  # noqa: E402
from src.metrics.routes import router as metrics_router  # noqa: E402
from src.system.routes import router as system_router  # noqa: E402
from src.impersonation.routes import router as impersonation_router  # noqa: E402
from src.sandbox.routes import router as sandbox_router  # noqa: E402
from src.sandbox.conversation_routes import router as sandbox_conversations_router  # noqa: E402

from src.tenant import TenantMiddleware  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not is_db_enabled():
        # platform-api requires BRIDGE_DB_URL — fail loud on startup.
        raise RuntimeError(
            "BRIDGE_DB_URL is not set. platform-api requires a Postgres connection. "
            "Set BRIDGE_DB_URL in the environment (platform.env)."
        )
    try:
        await init_pool()
        logger.info("✅ Platform DB pool initialised (asyncpg)")
    except Exception as e:
        logger.error(f"❌ Platform DB pool init failed: {e}")
        raise

    # Plan catalog: load from DB into the in-memory PLANS dict. The plans
    # table is the source of truth (migration 020); the PLANS dict in
    # src/budget/plans.py is a runtime cache, populated here and refreshed
    # by POST /v1/billing/plans/reload. Fail-fast on empty catalog — a
    # silently zero-plan Bridge would 404 every customer in the portal.
    from src.budget.plans import reload_plans
    try:
        count = await reload_plans()
        logger.info(f"✅ Plan catalog loaded: {count} active plans")
    except Exception as e:
        logger.error(f"❌ Plan catalog load failed: {e}")
        raise

    # Auth-token webhook pipeline (ADR cross-app/0002 Phase M1).
    # init_webhook_configs() raises RuntimeError when any required
    # BRIDGE_WEBHOOK_URL_<APP> / BRIDGE_WEBHOOK_SECRET_<APP> env is unset —
    # we want that fail-loud at startup, NOT when the first user clicks
    # "Forgot password".
    try:
        configs = init_webhook_configs()
        logger.info(f"✅ Webhook configs loaded: {sorted(configs.keys())}")
    except Exception as e:
        logger.error(f"❌ Webhook config load failed: {e}")
        raise

    # Start the background dispatcher. The pool is the one we just initialised.
    from src.db.client import get_pool as _get_pool
    dispatcher = WebhookDispatcher(_get_pool())
    dispatcher.start()
    app.state.webhook_dispatcher = dispatcher
    logger.info("✅ Webhook dispatcher started")

    yield

    try:
        await dispatcher.stop()
        logger.info("✅ Webhook dispatcher stopped")
    except Exception:
        pass

    try:
        await close_pool()
        logger.info("✅ Platform DB pool closed")
    except Exception:
        pass


app = FastAPI(
    title="Bridge Platform API",
    description="DB-backed identity/admin/billing/budget/activity layer (no LLM routing)",
    version="1.0.0",
    lifespan=lifespan,
)

# Mount all DB-backed platform routers.
app.include_router(admin_db_router)
app.include_router(identity_router)
app.include_router(self_service_router)
app.include_router(budget_router)
app.include_router(billing_router)
app.include_router(pending_orders_router)
app.include_router(admin_orders_router)
app.include_router(project_credits_router)
app.include_router(activity_router)
app.include_router(feedback_router)
app.include_router(audit_router)
app.include_router(dev_tokens_router)
app.include_router(stammdaten_router)
app.include_router(invoices_router)
app.include_router(metrics_router)
app.include_router(system_router)
app.include_router(impersonation_router)
app.include_router(sandbox_router)
app.include_router(sandbox_conversations_router)
logger.info("✅ Platform routes mounted")

# CORS — same policy as workers
cors_origins = json.loads(os.getenv("CORS_ORIGINS", '["*"]'))
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Tenant middleware — required for per-tenant auth/privacy context
app.add_middleware(TenantMiddleware)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    error_details = [
        {
            "field": " -> ".join(str(loc) for loc in err.get("loc", [])),
            "message": err.get("msg", "Unknown validation error"),
            "type": err.get("type", "validation_error"),
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "message": "Request validation failed",
                "type": "validation_error",
                "code": "invalid_request_error",
                "source": "platform_api",
                "retryable": False,
                "details": error_details,
                "timestamp": int(time.time()),
            }
        },
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "platform-api",
        "db_enabled": is_db_enabled(),
    }
