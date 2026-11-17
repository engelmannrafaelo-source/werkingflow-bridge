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
import math
from typing import Any, Dict, Optional

# HTTP statuses that mean "try again later" when no explicit verdict exists.
# Same set bridge_error() has always used.
TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Job error codes (store.mark_error `code`).
JOB_CODE_EXECUTOR_ERROR = "EXECUTOR_ERROR"
JOB_CODE_NO_EXECUTOR = "NO_EXECUTOR"
JOB_CODE_REQUEUE_EXHAUSTED = "REQUEUE_EXHAUSTED"
JOB_CODE_UPSTREAM_PREFIX = "UPSTREAM_HTTP_"
# A parked job still waiting at the retention backstop (store.prune_jobs, BR11).
JOB_CODE_MAX_AGE_EXCEEDED = "JOB_MAX_AGE_EXCEEDED"
# A running job was stopped on its owner's cancel, but the platform-api
# predates FK25B and cannot record 'cancelled' (registry, FK25B2). A deploy
# mismatch, not a gap: a retry meets the same platform-api.
JOB_CODE_STORE_INCOMPATIBLE = "JOB_STORE_INCOMPATIBLE"

# A job reaches UPSTREAM_HTTP_424 / _429 only after the runner has already
# waited out its own patience for that cause (registry.DEPENDENCY_PATIENCE,
# CAPACITY_RETRY_*). A client retry on top would repeat the same wait; the
# failure is an outage someone has to see (BR8R §2: 424 = named abort).
JOB_PATIENCE_SPENT_STATUSES = frozenset({424, 429})

UNCLASSIFIED_RETRYABLE = False

# What a job error says about the run besides message, code and verdict
# (JOBENDE, 11.10.2026). A research answer cut at the output limit came back
# as status=error with fehler_code/antwort_ende/session_id; the job error kept
# only the text, so a /v1/jobs caller read antwort_ende=None. Stored flat in
# the error row next to message/code, only when set.
JOB_ERROR_DETAIL_KEYS = ("fehler_code", "antwort_ende", "session_id")

# What a job error keeps of a provider REFUSAL (E3-1, 17.11.2026). A chat job
# whose caller did not announce the refusal mark gets the endpoint's 422
# modell_abbruch; its error object carried x_bridge_finish (category, request
# id, per-iteration tokens), but the job error kept only fehler_code, so a
# /v1/jobs caller could neither name the category nor book the tokens. Stored
# flat in the error row, next to `usage` (summed from the iterations). Only
# diagnosis the direct caller sees anyway — no prompt, no answer text.
JOB_ERROR_ABLEHNUNG_KEYS = ("x_bridge_finish", "category", "provider_request_id")


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
                 retry_after_s: Optional[float] = None,
                 usage_felder: Optional[Dict[str, Any]] = None,
                 details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.retry_after_s = retry_after_s
        # usage / usage_fehlt of the failed self-call (research, RUSAGE): a
        # failed run was paid for, and the job error is all its caller gets.
        # None = the failure carries no usage information at all.
        self.usage_felder = usage_felder
        # JOB_ERROR_DETAIL_KEYS of the failed answer; job_error_details reads it.
        self.details = job_error_details(details)


def job_usage_fields(exc: BaseException) -> Dict[str, Any]:
    """usage / usage_fehlt for the job error row, {} when the failure has none."""
    felder = getattr(exc, "usage_felder", None)
    return dict(felder) if isinstance(felder, dict) else {}


def job_ablehnung_fields(exc: BaseException) -> Dict[str, Any]:
    """JOB_ERROR_ABLEHNUNG_KEYS of a refused self-call, {} for any other failure."""
    felder = getattr(exc, "ablehnung", None)
    if not isinstance(felder, dict):
        return {}
    return {k: felder[k] for k in JOB_ERROR_ABLEHNUNG_KEYS if felder.get(k) is not None}


def ablehnung_aus_antwort(body: Any) -> Optional[Dict[str, Any]]:
    """{"ablehnung": {...}, "usage_felder": {...}} from a 422 modell_abbruch body
    that carries x_bridge_finish (vision_provider.ablehnung_ohne_marke); None
    for every other answer. Values are type-checked, never guessed:
      * x_bridge_finish only as a mapping, category / provider_request_id only
        as text;
      * usage = sum over x_bridge_finish.iterations when EVERY iteration has
        input_tokens and output_tokens as integers >= 0 and its cache fields
        absent/null or integers >= 0; otherwise usage_fehlt (a refusal was
        usually paid — "no tokens known" must not read as 0, a negative count
        is unreadable, not a credit). Shape and meaning as result.usage
        (vision_provider._usage_mit_cache): prompt_tokens = whole input
        (uncached + cache), the three parts in their own keys (Zweitpruefung
        E3-1 R1 A1).
    cost_eur = x_bridge_finish.cost_total_eur only when the refusal states
    cost_status "belegt" (the provider bills this category) with a number
    (R1 A2). That amount is the bridge's own price-list calculation from the
    tokens, NOT a provider debit: cost_source "bridge_berechnet", cost_status
    "berechnet" (Hauptkoordination 17.11.2026). Otherwise cost_eur null,
    cost_source and cost_status "unbekannt" — the caller's regular reserve,
    never an estimate here. Whether the provider bills stays in
    x_bridge_finish (cost_status, iterations[].billed), unchanged."""
    if not isinstance(body, dict):
        return None
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    finish = err.get("x_bridge_finish")
    if err.get("fehler_code") != "modell_abbruch" or not isinstance(finish, dict):
        return None
    ablehnung: Dict[str, Any] = {"x_bridge_finish": finish}
    for key in ("category", "provider_request_id"):
        wert = err.get(key, finish.get(key))
        if isinstance(wert, str) and wert:
            ablehnung[key] = wert
    def _zahl(wert: Any) -> bool:
        return type(wert) is int and wert >= 0

    iterationen = finish.get("iterations")
    ganz = (isinstance(iterationen, list) and iterationen
            and all(isinstance(it, dict)
                    and all(_zahl(it.get(k)) for k in ("input_tokens", "output_tokens"))
                    and all(it.get(k) is None or _zahl(it.get(k))
                            for k in ("cache_read_input_tokens", "cache_creation_input_tokens"))
                    for it in iterationen))
    if not ganz:
        return {"ablehnung": ablehnung, "usage_felder": {"usage_fehlt": "ablehnung_ohne_tokenangabe"}}

    def _summe(key: str) -> int:
        return sum(it.get(key) or 0 for it in iterationen)
    ungecacht = _summe("input_tokens")
    lesen = _summe("cache_read_input_tokens")
    anlage = _summe("cache_creation_input_tokens")
    usage: Dict[str, Any] = {
        "prompt_tokens": ungecacht + lesen + anlage,
        "completion_tokens": _summe("output_tokens"),
        "uncached_input_tokens": ungecacht,
        "cache_read_tokens": lesen,
        "cache_creation_tokens": anlage,
        "cost_eur": None,
        "cost_source": "unbekannt",
        "cost_status": "unbekannt",
        "quelle": "x_bridge_finish.iterations",
    }
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    gesamt = finish.get("cost_total_eur")
    if (finish.get("cost_status") == "belegt" and isinstance(gesamt, (int, float))
            and not isinstance(gesamt, bool) and math.isfinite(gesamt) and gesamt >= 0):
        usage.update(cost_eur=gesamt, cost_source="bridge_berechnet", cost_status="berechnet")
    return {"ablehnung": ablehnung, "usage_felder": {"usage": usage}}


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
        # The worker running it died on every attempt, and the watchdog has
        # already restarted it itself (max_attempts). Same reasoning as
        # JOB_PATIENCE_SPENT_STATUSES: a client retry would be attempt n+1 of
        # what the bridge already retried. And a job that kills its worker
        # every time (OOM, a crash the input triggers) is a poison job, not a
        # gap; each new submit may run, and be paid for, until it kills the
        # worker again. A worker death from a deploy is bridged by the
        # watchdog's own requeue; it does not repeat on every attempt.
        # (BR9b, BR8R2 SOLL; was True in BR9.)
        return fields(False)
    if code == JOB_CODE_STORE_INCOMPATIBLE:
        return fields(False)
    if code == JOB_CODE_MAX_AGE_EXCEEDED:
        # It already waited a day for capacity or a dependency; a client retry
        # would start the same wait again. An outage to look at, not a gap.
        return fields(False)
    return fields(UNCLASSIFIED_RETRYABLE)


def job_error_details(source: Any) -> Dict[str, str]:
    """JOB_ERROR_DETAIL_KEYS that are set, from any error answer or exception.

    An answer (mapping) carries them flat ({"fehler_code": ...}) or inside the
    bridge error envelope ({"error": {...}}); flat wins, the envelope fills
    what is missing. An exception carries them as its own attributes (e.g.
    fehler_code / antwort_ende on a research truncation) or in its `details`,
    which is read like an answer. {} when none is set; values as text."""
    if source is None:
        return {}
    found: Dict[str, Any] = {}
    if isinstance(source, dict):
        layers = [source]
        if isinstance(source.get("error"), dict):
            layers.append(source["error"])
    else:
        layers = [{key: getattr(source, key, None) for key in JOB_ERROR_DETAIL_KEYS}]
        own = getattr(source, "details", None)
        if isinstance(own, dict):
            layers.append(job_error_details(own))
    for layer in layers:
        for key in JOB_ERROR_DETAIL_KEYS:
            if found.get(key) in (None, "") and layer.get(key) not in (None, ""):
                found[key] = layer[key]
    return {key: str(value) for key, value in found.items()}


def job_error_fields(exc: BaseException) -> Dict[str, Any]:
    """Code + verdict for an exception that ends a job (rule 1, then 2/3),
    plus `details` when the exception carries any JOB_ERROR_DETAIL_KEYS."""
    details = job_error_details(exc)
    extra = {"details": details} if details else {}
    return {**_job_error_verdict(exc), **extra}


def _job_error_verdict(exc: BaseException) -> Dict[str, Any]:
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
