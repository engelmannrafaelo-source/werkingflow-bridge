"""Built-in job executors for the generic async-job system.

An executor turns a job's `payload` into a result dict (see registry.py):

    async def executor(payload, attribution, report_progress) -> dict

main.py registers these at startup. Kept here (not in main.py) so they import
without the heavy app module → unit-testable in isolation.

chat_executor — the first REAL durable consumer path. Rather than refactoring the
large, critical /v1/chat/completions handler, the executor calls it INTERNALLY
(self-HTTP to the worker's own port). That reuses the entire existing path —
budget gate, billing/deduction, privacy, rate-limit, retries — with zero changes
to the critical code. Billing therefore happens exactly once (inside that handler);
the job layer adds none. Trade-off: one in-process HTTP hop. A future refinement is
to extract a core chat function and call it directly (removing the hop); until then
this wrapper is the low-risk way to make any chat call a durable job.
"""
import logging
import os
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class ExecutorHTTPError(RuntimeError):
    """A self-call answered with an HTTP error — carries the ORIGINAL status.

    Without it every upstream failure collapses into a generic EXECUTOR_ERROR
    and clients see an opaque job error they treat as retryable (502). A
    deterministic 400 (e.g. Bedrock ValidationException) then gets hammered
    by client retry loops: 240 doomed calls / 4.5h customer wait on
    2026-07-20. The registry persists the status as UPSTREAM_HTTP_<status>
    so clients can restore proper retry semantics."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code

# The worker serves its own FastAPI app here (bypasses the nginx LB + its capacity
# gate — the self-call hits this worker directly). Overridable for tests/other binds.
SELF_BASE_URL = os.getenv("BRIDGE_SELF_URL", "http://localhost:8000")

# Generous: a chat completion can run minutes; the job's heartbeat keeps the row
# alive meanwhile, and the watchdog only requeues a genuinely dead worker.
CHAT_SELF_CALL_TIMEOUT_S = float(os.getenv("BRIDGE_CHAT_JOB_TIMEOUT_S", "600"))

# Research (esp. deep/exhaustive) can run far longer than a chat — many minutes up
# to ~40 min. The heartbeat keeps the job row alive for the whole run.
RESEARCH_SELF_CALL_TIMEOUT_S = float(os.getenv("BRIDGE_RESEARCH_JOB_TIMEOUT_S", "2400"))

# Generic JSON proxy self-call timeout (default for allowlisted paths — short).
PROXY_SELF_CALL_TIMEOUT_S = float(os.getenv("BRIDGE_PROXY_JOB_TIMEOUT_S", "300"))

# Doc-agent navigates a seeded workdir with file tools — multi-turn, can take
# several minutes over many documents.
DOC_AGENT_SELF_CALL_TIMEOUT_S = float(os.getenv("BRIDGE_DOC_AGENT_JOB_TIMEOUT_S", "1800"))

# Per-path overrides where the target endpoint's own internal budget exceeds the
# generic default. Timeout-chain invariant: the executor's self-call must sit
# ABOVE the target endpoint's internal budget so the endpoint's own (specific)
# error surfaces before the executor cuts the connection. /v1/privacy/smart-
# anonymize grants the privacy service 1200s (main.py) → 1260s here; nginx
# allows 2500s above both.
PROXY_PATH_TIMEOUTS_S: Dict[str, float] = {
    "/v1/privacy/smart-anonymize": float(os.getenv("BRIDGE_ANONYMIZE_JOB_TIMEOUT_S", "1260")),
}

# HTML→PDF render self-call timeout — matches the 600s the sync
# /v1/convert-html-to-pdf endpoint already grants the Chromium render.
PDF_SELF_CALL_TIMEOUT_S = float(os.getenv("BRIDGE_PDF_JOB_TIMEOUT_S", "600"))

# Allowlist for the generic 'proxy' executor. ONLY these paths may be invoked as a
# proxy job — never arbitrary paths (no /v1/jobs recursion, no internal routes).
# JSON-in / JSON-out endpoints ONLY; binary/multipart endpoints (document/convert,
# audio/transcriptions) are deliberately out of scope — they return binary / take
# file uploads that do not fit the JSON job-result model, and are short calls that
# gain little from durability. /v1/convert-html-to-pdf is JSON-in/JSON-out
# (base64) but long-running and has its own dedicated kind ('convert-html-to-pdf')
# with a render-appropriate timeout — keep it out of the generic proxy.
PROXY_ALLOWED_PATHS = {
    "/v1/privacy/smart-anonymize",
}

# attribution dict key → outgoing header, so the internal chat call bills/attributes
# to the same app/user/workflow as a direct call would. Live-verified 2026-07-02:
# an 'anonymous:<grund>' marker on the job POST arrives intact at the self-called
# endpoint (the attribution metrics counted it on both hops).
_ATTRIBUTION_HEADERS = {
    "app_id": "X-App-ID",
    "agent_id": "X-Agent-ID",
    "workflow_id": "X-Workflow-ID",
    "session_id": "X-Session-ID",
    "user_id": "X-User-ID",
    "app_env": "X-App-Env",
    "job_id": "X-Job-ID",
}

# Last-resort caller identity for self-calls whose triggering job carried NO
# app_id: without it, the executor's echo of an unattributed job POST books as
# app='unknown' on the TARGET path (e.g. /v1/convert-html-to-pdf) and reads like
# a second, independent leak. This names the true call-site (the job layer)
# WITHOUT masking the leak — the self-call still has no X-User-ID and keeps
# counting as unattributed. Never set when a real app_id exists (X-App-ID wins
# over the X-Client-ID fallback anyway; omitting keeps attributed flows
# byte-identical).
_SELFCALL_CLIENT_ID = "bridge-jobs/selfcall"


async def ping_executor(
    payload: Dict[str, Any],
    attribution: Optional[Dict[str, Any]],
    report_progress: Callable[[Dict[str, Any]], Awaitable[None]],
) -> Dict[str, Any]:
    """Built-in diagnostic executor — proves dispatch→run→poll without the model
    stack. Reachable only when BRIDGE_GENERIC_JOBS_ENABLED=true."""
    await report_progress({"phase": "pong", "percent": 100})
    return {"echo": payload, "attribution": attribution}


def _build_headers(attribution: Optional[Dict[str, Any]]) -> Dict[str, str]:
    from src.auth import auth_manager

    headers = {"Content-Type": "application/json"}
    api_key = auth_manager.get_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if attribution:
        for key, hdr in _ATTRIBUTION_HEADERS.items():
            val = attribution.get(key)
            if val:
                headers[hdr] = str(val)
    if "X-App-ID" not in headers:
        headers["X-Client-ID"] = _SELFCALL_CLIENT_ID
    return headers


async def chat_executor(
    payload: Dict[str, Any],
    attribution: Optional[Dict[str, Any]],
    report_progress: Callable[[Dict[str, Any]], Awaitable[None]],
) -> Dict[str, Any]:
    """Run a chat completion as a durable job by calling the existing
    /v1/chat/completions on this worker (non-streaming). Returns the OpenAI-style
    completion dict. Fail loud: a non-2xx self-call raises → the job is recorded
    as error (and requeued by the watchdog within the attempt cap)."""
    import httpx

    # Jobs persist a whole result, so force non-streaming regardless of caller input.
    body = {**payload, "stream": False}
    headers = _build_headers(attribution)

    await report_progress({"phase": "llm", "model": body.get("model")})

    async with httpx.AsyncClient(timeout=CHAT_SELF_CALL_TIMEOUT_S) as client:
        response = await client.post(
            f"{SELF_BASE_URL}/v1/chat/completions",
            json=body,
            headers=headers,
        )

    if response.status_code >= 400:
        # Surface the upstream status + a trimmed body so the job error is actionable.
        detail = response.text[:500]
        raise ExecutorHTTPError(
            response.status_code,
            f"chat self-call failed HTTP {response.status_code}: {detail}",
        )

    return response.json()


async def _self_post_json(
    path: str,
    body: Dict[str, Any],
    attribution: Optional[Dict[str, Any]],
    timeout_s: float,
) -> Dict[str, Any]:
    """POST a JSON body to one of THIS worker's own endpoints (self-HTTP) and return
    the parsed JSON. Reuses the entire existing path (budget/billing/privacy/rate-
    limit) exactly like a direct call — same pattern as chat_executor. Fail loud on
    non-2xx or a non-JSON body so the job records an actionable error."""
    import httpx

    headers = _build_headers(attribution)
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.post(f"{SELF_BASE_URL}{path}", json=body, headers=headers)

    if response.status_code >= 400:
        raise ExecutorHTTPError(
            response.status_code,
            f"self-call {path} failed HTTP {response.status_code}: {response.text[:500]}",
        )
    try:
        return response.json()
    except Exception as e:
        ctype = response.headers.get("content-type", "?")
        raise RuntimeError(f"self-call {path} returned non-JSON (content-type={ctype}): {e}")


async def research_executor(
    payload: Dict[str, Any],
    attribution: Optional[Dict[str, Any]],
    report_progress: Callable[[Dict[str, Any]], Awaitable[None]],
) -> Dict[str, Any]:
    """Run a /v1/research call as a durable job. Calls the existing endpoint in
    BLOCKING mode (async_mode forced off) so this executor receives the full result;
    durability/requeue comes from the job layer, NOT the legacy file-based research-
    async path. Returns the research result dict. Fail loud on non-2xx.

    /v1/research always answers 200 (ResearchResponse.status carries the
    outcome, never the HTTP status), so a plain non-2xx check here would miss
    every research failure — the job would be marked 'done' with an empty
    result instead of 'error' (same defect convert_html_to_pdf_executor
    already guards against for its own JSON contract). Checking
    result["status"] here is what lets a caller's error message (e.g. a
    research-cloud failure marked retryable, see src.main._mark_retryable)
    actually reach the job's error field instead of being swallowed as a
    false success."""
    # Force blocking: if the caller left async_mode=true we'd get a job-id back
    # instead of the result. The job layer is the durability mechanism here.
    body = {**payload, "async_mode": False}
    await report_progress({"phase": "research", "model": body.get("model")})
    result = await _self_post_json("/v1/research", body, attribution, RESEARCH_SELF_CALL_TIMEOUT_S)
    if result.get("status") == "error":
        raise RuntimeError(
            f"research self-call returned status=error: {result.get('error') or 'no error message'}"
        )
    return result


async def doc_agent_executor(
    payload: Dict[str, Any],
    attribution: Optional[Dict[str, Any]],
    report_progress: Callable[[Dict[str, Any]], Awaitable[None]],
) -> Dict[str, Any]:
    """Run a /v1/doc-agent call (file-tool agent over seeded documents) as a
    durable job. Same self-call pattern as research: the endpoint owns auth,
    budget gate and billing; the job layer owns durability/requeue."""
    await report_progress({"phase": "doc-agent", "model": payload.get("model")})
    return await _self_post_json("/v1/doc-agent", payload, attribution, DOC_AGENT_SELF_CALL_TIMEOUT_S)


async def convert_html_to_pdf_executor(
    payload: Dict[str, Any],
    attribution: Optional[Dict[str, Any]],
    report_progress: Callable[[Dict[str, Any]], Awaitable[None]],
) -> Dict[str, Any]:
    """Run the existing /v1/convert-html-to-pdf (shared Chromium renderer, proxied
    to the privacy-pdf-service) as a durable job. `payload` is the unchanged
    request body of that endpoint ({"html": "..."}). The renderer is JSON-in/
    JSON-out ({status, pdf_base64, size_bytes}), so its response IS the job result
    — no new render logic, billing/activity-tracking happens exactly once inside
    the existing endpoint (same self-call pattern as chat/research). Fail loud on
    non-2xx and on a 2xx body without pdf_base64 (never persist a 'done' job whose
    result cannot be turned into a PDF)."""
    html = payload.get("html")
    if not isinstance(html, str) or not html.strip():
        raise RuntimeError("convert-html-to-pdf payload requires a non-empty 'html' string")
    await report_progress({"phase": "render-pdf"})
    result = await _self_post_json(
        "/v1/convert-html-to-pdf", payload, attribution, PDF_SELF_CALL_TIMEOUT_S
    )
    if result.get("status") != "success" or not result.get("pdf_base64"):
        raise RuntimeError(
            f"convert-html-to-pdf returned no PDF (status={result.get('status')!r}): "
            f"{str(result)[:300]}"
        )
    return result


async def proxy_executor(
    payload: Dict[str, Any],
    attribution: Optional[Dict[str, Any]],
    report_progress: Callable[[Dict[str, Any]], Awaitable[None]],
) -> Dict[str, Any]:
    """Generic durable job for an ALLOWLISTED JSON-in/JSON-out endpoint.

        payload = {"path": "/v1/privacy/smart-anonymize", "body": {...}}

    Rejects (fail loud) any path not in PROXY_ALLOWED_PATHS — no arbitrary self-
    calls (no /v1/jobs recursion, no internal routes). Binary/multipart endpoints
    are unsupported by design (see PROXY_ALLOWED_PATHS note)."""
    path = payload.get("path")
    body = payload.get("body", {})
    if path not in PROXY_ALLOWED_PATHS:
        raise RuntimeError(
            f"proxy path not allowed: {path!r}. Allowed: {sorted(PROXY_ALLOWED_PATHS)}"
        )
    if not isinstance(body, dict):
        raise RuntimeError("proxy 'body' must be a JSON object")
    await report_progress({"phase": "proxy", "path": path})
    timeout_s = PROXY_PATH_TIMEOUTS_S.get(path, PROXY_SELF_CALL_TIMEOUT_S)
    return await _self_post_json(path, body, attribution, timeout_s)
