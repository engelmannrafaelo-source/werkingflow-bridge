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

    def __init__(
        self,
        status_code: int,
        message: str,
        retry_after_s: Optional[float] = None,
        rejection: Optional[str] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        # The upstream's own Retry-After, when it sent one. The job runner uses
        # it to schedule a capacity retry (registry._capacity_retry_delay)
        # instead of guessing — the bridge knows when its limit window resets,
        # so throwing that number away and picking our own would be worse than
        # what the caller was told.
        self.retry_after_s = retry_after_s
        # One-line, secret-free account of WHO refused and why (worker,
        # account, reason, and what the LB re-dispatch saw). Set only on
        # capacity refusals; the runner logs it on the defer line.
        self.rejection = rejection


# The worker serves its own FastAPI app here (bypasses the nginx LB + its capacity
# gate — the self-call hits this worker directly). Overridable for tests/other binds.
SELF_BASE_URL = os.getenv("BRIDGE_SELF_URL", "http://localhost:8000")

# ---------------------------------------------------------------------------
# Capacity re-dispatch through this bridge's own LB
# ---------------------------------------------------------------------------
# The self-call above is pinned to THIS worker, so when this worker's account
# is in a (soft) penalty the inner call 429s ("rejecting (NGINX failover)")
# although three sibling accounts are free — and a pinned call has no nginx to
# fail over. Parking the job and waiting for a watchdog re-claim cost 90-130 s
# on a 3-5 s job (measured 29.09.2026, dev bridge, ~30 % of small jobs).
#
# So on a capacity 429 the SAME request is sent once more, this time through
# the bridge's own LB. There proxy_next_upstream + the Lua pool router pick a
# free worker — the failover the sync path always had. Two guarantees:
#
#   * X-Bridge-Hop: 1 — the LB treats the request as already hopped and serves
#     it ONLY from the local tier (nginx.conf `$bridge_hopped`, ADR-0010 loop
#     guard). A dev job therefore never spills onto the prod bridge through
#     this path (and vice versa); it stays in the bridge that holds its row.
#   * Bounded: JOB_CAPACITY_REDISPATCH_ATTEMPTS LB calls per executor run, each
#     of which nginx itself bounds to one pass over the local pool. If that
#     is exhausted too, the original 429 surfaces and the runner parks the job
#     exactly as before (registry._defer_job).
#
# Only the account-consuming paths nginx's chat/research location serves are
# re-dispatched; everything else keeps the plain self-call. The LB service is
# named `nginx` in both compose topologies (dev + prod); a worker-host without
# a local LB can set BRIDGE_JOB_REDISPATCH_URL="" to switch this off — an
# unreachable LB is logged loudly and falls back to the park.
JOB_REDISPATCH_BASE_URL = os.getenv(
    "BRIDGE_JOB_REDISPATCH_URL", "http://nginx:80"
).strip()
JOB_CAPACITY_REDISPATCH_ATTEMPTS = 1
JOB_REDISPATCH_PATHS = frozenset({"/v1/chat/completions", "/v1/research"})
# Diagnostic marker on the re-dispatched request (which worker handed it on).
REDISPATCH_HEADER = "X-Bridge-Job-Redispatch"
CAPACITY_STATUS = 429
# LB statuses that mean "no local worker could take it" (@bridge_full /
# @pool_exhausted_response envelope) — not a verdict on the request itself.
# The job keeps its original 429 and is parked; a deterministic error from a
# worker that DID take it (400, 402, ...) passes through unchanged.
_LB_NO_CAPACITY_STATUSES = frozenset({CAPACITY_STATUS, 503})


def _self_identity() -> str:
    """worker/account of THIS process, for the rejection line (no secrets)."""
    worker = os.getenv("INSTANCE_NAME", "unknown")
    account = (os.getenv("WORKER_ACCOUNT") or "").strip() or "?"
    return f"worker={worker} account={account}"


def _describe_rejection(response, fallback_worker: Optional[str] = None) -> str:
    """Who refused and why, from the bridge's own error envelope. Never the
    body verbatim (could echo user content) — only the named envelope fields."""
    err: Dict[str, Any] = {}
    try:
        data = response.json()
        if isinstance(data, dict):
            candidate = data.get("error", data)
            if isinstance(candidate, dict):
                err = candidate
    except Exception:
        err = {}
    worker = err.get("bridge_worker") or fallback_worker or "?"
    reason = err.get("reason") or err.get("bridge_type") or "?"
    msg = str(err.get("message") or "")[:120]
    return f"worker={worker} reason={reason} status={response.status_code} msg={msg!r}"


async def _post_with_capacity_redispatch(
    client, path: str, body: Dict[str, Any], headers: Dict[str, str]
):
    """POST to this worker (self-call); on a capacity 429 re-dispatch the SAME
    request through the local LB (see the block above). Returns
    ``(response, rejection)`` — ``rejection`` is the one-line account of who
    refused, set whenever the FINAL answer is still a capacity 429."""
    response = await client.post(f"{SELF_BASE_URL}{path}", json=body, headers=headers)
    if response.status_code != CAPACITY_STATUS:
        return response, None

    own = _self_identity()
    first = f"self-call refused ({own}; {_describe_rejection(response)})"
    if path not in JOB_REDISPATCH_PATHS or not JOB_REDISPATCH_BASE_URL:
        why = (
            "path not re-dispatchable"
            if path not in JOB_REDISPATCH_PATHS
            else "BRIDGE_JOB_REDISPATCH_URL empty"
        )
        return response, f"{first}; no LB re-dispatch ({why})"

    hop_headers = {
        **headers,
        # ADR-0010 loop guard: the LB serves a hopped request from the LOCAL
        # tier only, never cross-bridge — dev stays dev, prod stays prod.
        "X-Bridge-Hop": "1",
        REDISPATCH_HEADER: os.getenv("INSTANCE_NAME", "unknown"),
    }
    lb_notes = []
    for attempt in range(1, JOB_CAPACITY_REDISPATCH_ATTEMPTS + 1):
        logger.warning(
            f"🔀 job self-call {path}: {first} — re-dispatching via LB "
            f"{JOB_REDISPATCH_BASE_URL} "
            f"(attempt {attempt}/{JOB_CAPACITY_REDISPATCH_ATTEMPTS})"
        )
        try:
            lb_response = await client.post(
                f"{JOB_REDISPATCH_BASE_URL}{path}", json=body, headers=hop_headers
            )
        except Exception as e:
            # LB unreachable: keep the 429; the runner parks the job.
            logger.error(
                f"❌ job LB re-dispatch {path} unreachable ({type(e).__name__}: {e}) — "
                f"job will be parked instead"
            )
            lb_notes.append(f"LB#{attempt} unreachable: {type(e).__name__}")
            break
        if lb_response.status_code not in _LB_NO_CAPACITY_STATUSES:
            # A sibling worker took it (2xx), or gave a real verdict on the
            # request — either way that is the job's answer now.
            logger.info(
                f"✅ job LB re-dispatch {path} answered HTTP {lb_response.status_code} "
                f"(upstream {lb_response.headers.get('X-Upstream-Server', '?')})"
            )
            return lb_response, None
        lb_notes.append(
            f"LB#{attempt} {_describe_rejection(lb_response, fallback_worker='pool')}"
        )
    # Every local worker refused too. Keep the ORIGINAL self-call 429 (its
    # Retry-After is this worker's own window) so the runner parks the job.
    return response, f"{first}; " + "; ".join(lb_notes)


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
DOC_AGENT_SELF_CALL_TIMEOUT_S = float(
    os.getenv("BRIDGE_DOC_AGENT_JOB_TIMEOUT_S", "1800")
)

# Per-path overrides where the target endpoint's own internal budget exceeds the
# generic default. Timeout-chain invariant: the executor's self-call must sit
# ABOVE the target endpoint's internal budget so the endpoint's own (specific)
# error surfaces before the executor cuts the connection. /v1/privacy/smart-
# anonymize grants the privacy service 1200s (main.py) → 1260s here; nginx
# allows 2500s above both.
PROXY_PATH_TIMEOUTS_S: Dict[str, float] = {
    "/v1/privacy/smart-anonymize": float(
        os.getenv("BRIDGE_ANONYMIZE_JOB_TIMEOUT_S", "1260")
    ),
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
    # ADR-0011: the self-call must keep the job's HOME bridge — nginx trusts
    # this header from the docker-internal subnet (see $bridge_origin_out),
    # so the inner request bills against the same budget domain as the job.
    "bridge_origin": "X-Bridge-Origin",
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


def _retry_after_s(response) -> Optional[float]:
    """Parse a Retry-After header (delta-seconds form) — None when absent or
    not a number. The HTTP-date form is not produced by any bridge path and is
    deliberately not guessed at."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw.strip())
    except (AttributeError, ValueError):
        logger.warning(f"self-call sent an unparseable Retry-After: {raw!r}")
        return None


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
        response, rejection = await _post_with_capacity_redispatch(
            client, "/v1/chat/completions", body, headers
        )

    if response.status_code >= 400:
        # Surface the upstream status + a trimmed body so the job error is actionable.
        detail = response.text[:500]
        raise ExecutorHTTPError(
            response.status_code,
            f"chat self-call failed HTTP {response.status_code}: {detail}",
            retry_after_s=_retry_after_s(response),
            rejection=rejection,
        )

    return attach_ledger_cost(response.json(), response.headers)


def attach_ledger_cost(result: Any, headers: Any) -> Any:
    """Put the ledger's price of this job's call(s) into ``result.usage``.

    The job result used to carry tokens only, so no caller could say what an
    order cost (Befund 23.09.2026, werking-report). The number comes from the
    ledger (X-Bridge-Cost-Eur, set by DeliveryProbeMiddleware from the same
    call_cost_eur that is booked and deducted) — NOT re-priced from ``usage``:
    OpenAI-style prompt_tokens include cache traffic that the ledger prices at
    its own rates, so a second calculation here would be a second, wrong truth.

    Header absent → ``cost_eur: None`` with ``cost_source: "unbekannt"``.
    Explicitly unknown, never 0.0: "no price reached us" is not "free".
    """
    if not isinstance(result, dict):
        return result
    usage = result.get("usage")
    if not isinstance(usage, dict):
        usage = {}
        result["usage"] = usage
    raw = headers.get("x-bridge-cost-eur") if headers is not None else None
    if raw is None:
        usage["cost_eur"] = None
        usage["cost_source"] = "unbekannt"
        return result
    try:
        usage["cost_eur"] = float(raw)
    except (TypeError, ValueError):
        # Never fail the job over this: the call is paid and its result is
        # here. Say "unknown" out loud instead.
        logger.error(
            "job result: X-Bridge-Cost-Eur is not a number (%r) "
            "— cost_eur left unknown",
            raw,
        )
        usage["cost_eur"] = None
        usage["cost_source"] = "unbekannt"
        return result
    usage["cost_source"] = "ledger"
    usage["cost_calls"] = int(headers.get("x-bridge-cost-calls") or 0)
    usage["pricing_version"] = headers.get("x-bridge-pricing-version") or None
    return result


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
        response, rejection = await _post_with_capacity_redispatch(
            client, path, body, headers
        )

    if response.status_code >= 400:
        raise ExecutorHTTPError(
            response.status_code,
            f"self-call {path} failed HTTP {response.status_code}: "
            f"{response.text[:500]}",
            retry_after_s=_retry_after_s(response),
            rejection=rejection,
        )
    try:
        return response.json()
    except Exception as e:
        ctype = response.headers.get("content-type", "?")
        raise RuntimeError(
            f"self-call {path} returned non-JSON (content-type={ctype}): {e}"
        )


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
    result = await _self_post_json(
        "/v1/research", body, attribution, RESEARCH_SELF_CALL_TIMEOUT_S
    )
    if result.get("status") == "error":
        raise RuntimeError(
            "research self-call returned status=error: "
            f"{result.get('error') or 'no error message'}"
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
    return await _self_post_json(
        "/v1/doc-agent", payload, attribution, DOC_AGENT_SELF_CALL_TIMEOUT_S
    )


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
        raise RuntimeError(
            "convert-html-to-pdf payload requires a non-empty 'html' string"
        )
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


async def erkunder_executor(
    payload: dict,
    attribution: Optional[dict],
    report_progress: Callable[[dict], Awaitable[None]],
) -> dict:
    """Transfer this worker's account to the isolated, idempotent coordinator."""
    import asyncio
    from pathlib import Path

    import httpx
    from pydantic import ValidationError

    from src.erkunder.models import Auftrag, Ergebnis
    from src.erkunder.zugang import intern_config
    from src.jobs.registry import DEPENDENCY_UNAVAILABLE_STATUS

    try:
        auftrag = Auftrag.model_validate(payload)
    except ValidationError:
        raise ExecutorHTTPError(400, "ungueltiger Erkunder-Auftrag") from None
    token_path = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN_FILE")
    if not token_path:
        raise RuntimeError("Erkunder: CLAUDE_CODE_OAUTH_TOKEN_FILE fehlt")
    try:
        token = Path(token_path).read_text().strip()
    except OSError:
        raise RuntimeError("Erkunder: Tokendatei nicht lesbar") from None
    if not token:
        raise RuntimeError("Erkunder: Tokendatei leer")
    url, headers = intern_config()

    async def run() -> dict:
        async with httpx.AsyncClient(timeout=60) as client:
            while True:
                # Reattach even if a fast restart happened between two polls.
                response = await client.post(
                    f"{url}/start",
                    headers=headers,
                    json={
                        "worker": os.environ.get("INSTANCE_NAME", "unknown"),
                        "claude_token": token,
                        "auftrag": auftrag.model_dump(mode="json", by_alias=True),
                    },
                )
                if response.status_code in {502, 503, 504}:
                    raise ExecutorHTTPError(
                        DEPENDENCY_UNAVAILABLE_STATUS,
                        "Erkunder: Leitstand nicht bereit",
                    )
                if response.status_code == 409:
                    raise ExecutorHTTPError(429, "Erkunder belegt", retry_after_s=120)
                if response.status_code != 200:
                    raise RuntimeError(f"Erkunder start HTTP {response.status_code}")
                response = await client.get(
                    f"{url}/status/{auftrag.bericht_id}", headers=headers
                )
                if response.status_code in {502, 503, 504}:
                    raise ExecutorHTTPError(
                        DEPENDENCY_UNAVAILABLE_STATUS,
                        "Erkunder: Leitstand nicht bereit",
                    )
                if response.status_code != 200:
                    raise RuntimeError(f"Erkunder status HTTP {response.status_code}")
                state = response.json()
                await report_progress(
                    {key: state[key] for key in ("schritt", "fertig", "gesamt")}
                )
                if state["zustand"] == "fertig":
                    return Ergebnis.model_validate(state["meta"]).model_dump(
                        by_alias=True
                    )
                if state["zustand"] == "abbruch":
                    error = state.get("fehler", {})
                    reason = (
                        error.get("grund", "unbekannt")
                        if isinstance(error, dict)
                        else error
                    )
                    step = state["schritt"]
                    # Never echo an upstream body/token into the generic job log.
                    known = str(reason).split(":", 1)[0]
                    if known not in {
                        "zeit",
                        "speicher",
                        "cli_fehler",
                        "geheimnis_im_ergebnis",
                        "platz_neustart",
                        "konto",
                        "unbekannt",
                    }:
                        known = "unbekannt"
                    safe_step = (
                        step
                        if step
                        in {
                            "daten",
                            "erkunder-1",
                            "erkunder-2",
                            "erkunder-3",
                            "harmonisierung",
                            "pruefung",
                            "harmonisierung-korrektur",
                            "pruefung-korrektur",
                        }
                        else "unbekannt"
                    )
                    raise RuntimeError(f"erkunder abbruch: {safe_step}: {known}")
                if state["zustand"] != "laeuft":
                    raise RuntimeError("Erkunder: ungueltiger Zustand")
                await asyncio.sleep(15)

    try:
        return await asyncio.wait_for(
            run(), float(os.getenv("ERKUNDER_JOB_TIMEOUT_S", "6600"))
        )
    except asyncio.TimeoutError:
        raise RuntimeError("Erkunder: Gesamtfrist abgelaufen") from None
    except httpx.TransportError:
        # The watchdog retries /start with the same report ID and fresh token.
        # B1m then resumes interrupted steps; completed steps remain intact.
        raise ExecutorHTTPError(
            DEPENDENCY_UNAVAILABLE_STATUS, "Erkunder: Leitstand nicht erreichbar"
        ) from None
    except (httpx.HTTPError, ValidationError, ValueError, KeyError):
        raise RuntimeError(
            "Erkunder: Leitstand-Antwort ungueltig oder nicht erreichbar"
        ) from None
