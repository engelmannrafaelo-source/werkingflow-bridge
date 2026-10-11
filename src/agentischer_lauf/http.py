"""Additive dispatch on the existing Research and content routes.

No CLI fallback: deployments must explicitly bind a bounded adapter AND an
admission function enforcing their account/provider/privacy/billing policy.
Until then the requested capability is rejected before any model call.
"""

from functools import wraps
from fastapi import HTTPException, Response

from .adapter import RunError

_runtime = None
_admit = None


def configure(runtime, admit):
    """Server composition seam; admit(request, body) returns verified tenant scope.

    Admission must enforce provider/model permission, privacy, capacity, budget
    and accounting, including review model, and must not rewrite the app input.
    This is deliberately not selectable by a request body or environment import.
    """
    global _runtime, _admit
    if not callable(admit):
        raise TypeError("explicit policy admission required")
    _runtime, _admit = runtime, admit


def failure(exc):
    status = 409 if exc.code == "lauf_konflikt" else 429 if exc.code == "lauf_belegt" else 422
    return HTTPException(
        status, {"code": exc.code, "retryable": exc.retryable, "retry_after_s": exc.retry_after_s}
    )


def require_runtime():
    if _runtime is None or _admit is None:
        raise failure(RunError("faehigkeit_nicht_verfuegbar"))
    return _runtime


async def authorized(request, credentials):
    from src.auth import verify_api_key
    from src.main import enforce_attribution, extract_attribution_context

    from .bedrock import ATTRIBUTION

    await verify_api_key(request, credentials)
    enforce_attribution(request)
    # The ledger booking of each bounded model call carries this caller.
    ATTRIBUTION.set(extract_attribution_context(request))


def research_dispatch(legacy):
    @wraps(legacy)
    async def dispatch(request_body, request, credentials=None, *args, **kwargs):
        if request_body.agentischer_lauf is None:
            return await legacy(request_body, request, credentials, *args, **kwargs)
        await authorized(request, credentials)
        runtime = require_runtime()
        try:
            runtime.preflight(request_body)
            tenant = await _admit(request, request_body)
            if not isinstance(tenant, str) or not tenant:
                raise HTTPException(403, "verified tenant scope required")
            return await runtime.execute(request_body, tenant)
        except RunError as exc:
            raise failure(exc) from None

    return dispatch


def content_dispatch(legacy):
    @wraps(legacy)
    async def dispatch(session_id, request, credentials=None, **kwargs):
        artifact = request.query_params.get("lauf_artifact")
        if not isinstance(artifact, str) or not artifact:
            return await legacy(
                session_id=session_id, request=request, credentials=credentials, **kwargs
            )
        await authorized(request, credentials)
        runtime = require_runtime()
        tenant = await _admit(request, None)
        if not isinstance(tenant, str) or not tenant:
            raise HTTPException(403, "verified tenant scope required")
        try:
            data = runtime.storage.artifact(
                tenant, session_id, request.query_params["lauf_artifact"]
            )
        except RunError:
            raise HTTPException(404, "artifact not found") from None
        return Response(data, media_type="application/octet-stream")

    return dispatch


def job_dispatch(legacy):
    @wraps(legacy)
    async def dispatch(body, request, credentials=None):
        if body.kind != "research" or "agentischer_lauf" not in body.payload:
            return await legacy(body, request, credentials)
        from pydantic import ValidationError
        from src.models import ResearchRequest

        await authorized(request, credentials)
        try:
            parsed = ResearchRequest.model_validate(body.payload)
        except ValidationError:
            raise failure(RunError("auftrag_ungueltig")) from None
        runtime = require_runtime()
        try:
            runtime.preflight(parsed)
            tenant = await _admit(request, parsed)
            if not isinstance(tenant, str) or not tenant:
                raise HTTPException(403, "verified tenant scope required")
            # Reserve before any work is scheduled. A refused submission remains
            # visibly accepted but unstarted; a later attempt keeps its deadline.
            runtime.reserve(parsed, tenant)
        except RunError as exc:
            raise failure(exc) from None
        return await legacy(body, request, credentials)

    return dispatch
