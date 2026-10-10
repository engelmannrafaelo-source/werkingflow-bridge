"""error_contract — the ONE place that decides whether a bridge error is worth
retrying (BR9, 10.10.2026).

Callers (Energy RETRY, Report, PC1) read only structured fields:

    retryable      bool  — True: the same request can succeed later without any
                           change by the caller. False: it will not, or the
                           bridge does not know that it would.
    retry_after_s  int?  — when the bridge knows how long to wait, else None.

Before BR9 the verdict lived in three places that disagreed: bridge_error()
derived it from the HTTP status, research errors carried it only as a text
marker (main._mark_retryable), and job errors carried nothing at all
(store.mark_error wrote {message, code}). A permanent pin error on the chat
path went out as 503 and therefore as retryable:true.

The rules, in order:

  1. An explicit verdict wins. The code that knows (the pin lookup, the
     research-cloud cap, the job runner) sets it; nobody re-derives it later.
  2. No verdict, but an HTTP status → TRANSIENT_HTTP_STATUSES. This is the
     existing wire contract of bridge_error(); it is unchanged.
  3. Job errors are classified by job_error_fields() below. Unclassified
     exceptions are NOT promised as transient (UNCLASSIFIED_RETRYABLE): a retry
     of a failed chat/research job costs a full run, and "the bridge did not
     recognise this failure" is not "it will pass".

The text marker of _mark_retryable stays in research messages for existing
text classifiers (werking-report isTransientInfraError). It is set by the same
helper that sets the structured field, so the two cannot drift apart.
"""
from typing import Any, Dict, Optional

# HTTP statuses that mean "try again later" when no explicit verdict exists.
# Same set bridge_error() has always used.
TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Job error codes (store.mark_error `code`).
JOB_CODE_EXECUTOR_ERROR = "EXECUTOR_ERROR"
JOB_CODE_NO_EXECUTOR = "NO_EXECUTOR"
JOB_CODE_REQUEUE_EXHAUSTED = "REQUEUE_EXHAUSTED"
JOB_CODE_UPSTREAM_PREFIX = "UPSTREAM_HTTP_"

# A job reaches UPSTREAM_HTTP_424 / _429 only after the runner has already
# waited out its own patience for that cause (registry.DEPENDENCY_PATIENCE,
# CAPACITY_RETRY_*). A client retry on top would repeat the same wait; the
# failure is an outage someone has to see (BR8R §2: 424 = named abort).
JOB_PATIENCE_SPENT_STATUSES = frozenset({424, 429})

UNCLASSIFIED_RETRYABLE = False


def is_retryable_status(status_code: int) -> bool:
    """Rule 2: the verdict for an HTTP status nobody classified explicitly."""
    return status_code in TRANSIENT_HTTP_STATUSES


def fields(retryable: bool, retry_after_s: Optional[float] = None) -> Dict[str, Any]:
    """The two structured fields, normalised (retry_after_s as whole seconds)."""
    after = None
    if retryable and retry_after_s is not None:
        after = max(0, int(retry_after_s))
    return {"retryable": bool(retryable), "retry_after_s": after}


class JobFailure(RuntimeError):
    """An executor failure that carries its own verdict (rule 1), e.g. a
    research answer with status=error and retryable:true, or a self-call that
    lost its connection."""

    def __init__(self, message: str, *, retryable: bool,
                 retry_after_s: Optional[float] = None):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.retry_after_s = retry_after_s


def _upstream_status(code: Optional[str]) -> Optional[int]:
    if not code or not code.startswith(JOB_CODE_UPSTREAM_PREFIX):
        return None
    try:
        return int(code[len(JOB_CODE_UPSTREAM_PREFIX):])
    except ValueError:
        return None


def job_code_fields(code: Optional[str]) -> Dict[str, Any]:
    """Verdict from the job error code alone — for codes that carry it fully,
    and for rows written before BR9 (or by a platform-api that does not store
    the fields yet), where nothing else is known."""
    status = _upstream_status(code)
    if status is not None:
        if status in JOB_PATIENCE_SPENT_STATUSES:
            return fields(False)
        return fields(is_retryable_status(status))
    if code == JOB_CODE_REQUEUE_EXHAUSTED:
        # The worker running it died (deploy, OOM, host) on every attempt;
        # the job itself was never refused.
        return fields(True)
    return fields(UNCLASSIFIED_RETRYABLE)


def job_error_fields(exc: BaseException) -> Dict[str, Any]:
    """Code + verdict for an exception that ends a job (rule 1, then 2/3)."""
    from src.jobs.executors import ExecutorHTTPError

    if isinstance(exc, ExecutorHTTPError):
        code = f"{JOB_CODE_UPSTREAM_PREFIX}{exc.status_code}"
        if exc.status_code in JOB_PATIENCE_SPENT_STATUSES:
            return {"code": code, **fields(False)}
        if exc.retryable is not None:
            return {"code": code, **fields(exc.retryable, exc.retry_after_s)}
        return {"code": code, **fields(is_retryable_status(exc.status_code),
                                       exc.retry_after_s)}
    if isinstance(exc, JobFailure):
        return {"code": JOB_CODE_EXECUTOR_ERROR,
                **fields(exc.retryable, exc.retry_after_s)}
    return {"code": JOB_CODE_EXECUTOR_ERROR, **fields(UNCLASSIFIED_RETRYABLE)}


def job_error_view(error: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The error object GET /v1/jobs/{id} returns: the stored one, with the
    verdict filled in from the code when the row does not carry it."""
    if not isinstance(error, dict):
        return error
    if isinstance(error.get("retryable"), bool):
        return {"retry_after_s": None, **error}
    return {**error, **job_code_fields(error.get("code"))}


def envelope_fields(body: Any) -> Dict[str, Any]:
    """Explicit verdict from a bridge error body ({"error": {...}} or flat),
    {} when it carries none. Used where a self-call's answer becomes a job
    error, so the verdict of the endpoint survives the hop."""
    if not isinstance(body, dict):
        return {}
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    if not isinstance(err.get("retryable"), bool):
        return {}
    after = err.get("retry_after_s")
    if not isinstance(after, (int, float)) or isinstance(after, bool):
        after = None
    return fields(err["retryable"], after)
