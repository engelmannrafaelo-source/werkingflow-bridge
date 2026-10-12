"""BRREFUSAL (FK32 b, 12.10.2026): a model refusal is its own, final error.

Befund FK32 (dev, CLI 2.1.285): the safety classifier stopped turn 1 of a
~455k reduction call; the CLI's same-model retry ("continuing once with that
noted") re-sent the full context and was refused again; the run ended in the
CLI's API-error frame ("…'s safeguards flagged this message") without an HTTP
status. CliApiError made that a retryable 429 with Retry-After 30, and Energy
sent the same context up to 24 times as if the pool were full.

Measured shapes (messages.jsonl of 171b5947, 27415153, d308d235, 29d39a14,
2cbb3ec6 on dev): assistant turn → system "informational" → user meta turn →
assistant turn → system subtype "model_refusal_no_fallback" with
api_refusal_category; the withheld API-error frame follows. The CLI bundle
(2.1.285 and 2.1.296) documents model_refusal_no_fallback as "the structured
counterpart to detecting stop_reason 'refusal' on the assistant error frame"
and gates the same-model retry on CLAUDE_CODE_DISABLE_REFUSAL_RETRY.

Here: the signal is a field (never the sentence), the answer is 422 with
code/fehler_code modell_abbruch and retryable false on every path, the audit
names it, the CLI retry is off — and a real capacity error stays a retryable
429.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock as _MagicMock

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

import src.claude_cli as claude_cli  # noqa: E402
import src.main as main  # noqa: E402
from src.claude_cli import ClaudeCodeCLI  # noqa: E402
from src.middleware.bridge_error import (  # noqa: E402
    CliApiError,
    CliModellAbbruch,
    classify_exception,
)
from src.sdk_parser import resilient_parse_message  # noqa: E402
from tests import sdk_strom as sdk  # noqa: E402

WORKER = "worker-brrefusal"
FLAGGED = (
    "Claude Sonnet 5.5's safeguards flagged this message "
    "(https://www.anthropic.com/legal/aup). Try rephrasing the request."
)
CATEGORY = "reasoning_extraction"


# ── stream building blocks (the measured shapes) ────────────────────────────


def _turn(text: str, *, stop_reason=None, msg_id="msg_1", category=None):
    m = sdk.AssistantMessage(content=[sdk.TextBlock(text=text)], model="claude-sonnet-5-5")
    m._bridge_message_id = msg_id
    m._bridge_message_usage = {"input_tokens": 2, "cache_read_input_tokens": 455000,
                               "output_tokens": 900}
    if stop_reason is not None:
        m._bridge_stop_reason = stop_reason
    if category is not None:
        m._bridge_stop_details = {"type": "refusal", "category": category}
    return m


def _informational():
    return sdk.SystemMessage(subtype="informational", data={
        "type": "system", "subtype": "informational", "level": "notice",
        "content": "Sonnet 5.5's safeguards stopped the response above · "
                   "continuing once with that noted"})


class UserMessage:  # the SDK class NAME is what is_user_turn reads
    def __init__(self, text):
        self.content = [sdk.TextBlock(text=text)]


def _meta_turn():
    return UserMessage("Your response above was stopped by a safety classifier — this is "
                       "not a tool or API error. Do not produce that content again.")


def _no_fallback(category=CATEGORY):
    return sdk.SystemMessage(subtype="model_refusal_no_fallback", data={
        "type": "system", "subtype": "model_refusal_no_fallback",
        "original_model": "claude-sonnet-5-5", "request_id": "req_x",
        "api_refusal_category": category,
        "api_refusal_explanation": "This request was blocked …"})


def _error_frame(*, refusal=True, text=FLAGGED):
    m = sdk.AssistantMessage(content=[sdk.TextBlock(text=text)], model="<synthetic>")
    m._bridge_is_api_error_message = True
    m._bridge_cli_error = "unknown"
    if refusal:
        m._bridge_stop_reason = "refusal"
        m._bridge_stop_details = {"type": "refusal", "category": CATEGORY}
    return m


def _error_result(status=None, text=FLAGGED):
    r = sdk.result("success", is_error=True, num_turns=2,
                   usage={"input_tokens": 6, "cache_read_input_tokens": 911320,
                          "output_tokens": 3958})
    r.result = text
    if status is not None:
        r._bridge_api_error_status = status
    r._bridge_terminal_reason = "api_error"
    return r


def _ok_result():
    return sdk.result("success", num_turns=1, usage={"input_tokens": 2, "output_tokens": 5})


# FK32 hard case: turn 1 refused, the CLI's retry refused again → error frame.
def HART_FK32():
    return (sdk.init(), _turn("<teil nr=\"1\">…", stop_reason="refusal", category=CATEGORY),
            _informational(), _meta_turn(),
            _turn("Ich schreibe das nicht erneut", msg_id="msg_2"),
            _no_fallback(), _error_frame(), _error_result())


# CLI retry off: the refusal is reported at once, as frame + system message.
def HART_OHNE_RETRY():
    return (sdk.init(), _no_fallback(), _error_frame(), _error_result())


# Real capacity: an API error frame without refusal, 529 / no status.
def KAPAZITAET_529():
    return (sdk.init(), _error_frame(refusal=False, text="API Error: 529 overloaded"),
            _error_result(529, "API Error: 529 overloaded"))


def KAPAZITAET_OHNE_STATUS():
    return (sdk.init(), _error_frame(refusal=False, text="API Error: Connection lost"),
            _error_result(None, "API Error: Connection lost"))


class _Optionen:
    """A fresh options object per run: with the SDK mocked, ClaudeCodeOptions()
    would return one shared MagicMock, and options.env would leak between
    tests."""

    def __init__(self, **kw):
        self.env = None
        self.__dict__.update(kw)

    def __getattr__(self, name):
        return None


@pytest.fixture(autouse=True)
def umgebung(monkeypatch):
    monkeypatch.setattr(claude_cli, "ClaudeCodeOptions", _Optionen)
    monkeypatch.setenv("INSTANCE_NAME", WORKER)
    monkeypatch.delenv("BRIDGE_CLI_REFUSAL_RETRY", raising=False)
    monkeypatch.setattr(
        claude_cli, "MessageParseError", type("MessageParseError", (Exception,), {})
    )


# ── 1. detection: fields, never the sentence ────────────────────────────────


def test_system_meldung_ist_das_signal():
    sig = claude_cli.cli_refusal_signal(_no_fallback())
    assert sig == {"stop_reason": "refusal", "category": CATEGORY,
                   "quelle": "model_refusal_no_fallback"}
    # dict form after sdk_message_to_dict, and the raw stream-json form
    assert claude_cli.cli_refusal_signal(sdk.chunk(_no_fallback()))["category"] == CATEGORY
    assert claude_cli.cli_refusal_signal(
        {"type": "system", "subtype": "model_refusal_no_fallback",
         "api_refusal_category": None})["quelle"] == "model_refusal_no_fallback"


def test_satz_ohne_feld_ist_kein_signal():
    """The words alone — in a model answer, in the informational notice, in an
    API error frame without stop_reason — are never a refusal."""
    assert claude_cli.cli_refusal_signal(_informational()) is None
    assert claude_cli.cli_refusal_signal(sdk.init()) is None
    assert claude_cli.cli_refusal_signal(_turn(FLAGGED)) is None
    turn = claude_cli.detect_cli_api_error(_error_frame(refusal=False))
    assert claude_cli.cli_refusal_signal_of_turn(turn) is None


def test_fehlerframe_mit_stop_reason_refusal_ist_signal():
    turn = claude_cli.detect_cli_api_error(_error_frame())
    assert turn["stop_reason"] == "refusal" and turn["category"] == CATEGORY
    assert claude_cli.cli_refusal_signal_of_turn(turn)["quelle"] == "api_error_turn"


def test_parser_traegt_stop_reason_des_fehlerframes():
    roh = {"type": "assistant", "error": "unknown", "is_api_error_message": True,
           "message": {"content": [{"type": "text", "text": FLAGGED}], "model": "<synthetic>",
                       "stop_reason": "refusal",
                       "stop_details": {"type": "refusal", "category": CATEGORY}},
           "session_id": "s", "uuid": "u"}

    def sdk_parse(data):
        return sdk.AssistantMessage(content=[sdk.TextBlock(text=FLAGGED)], model="<synthetic>")

    turn = claude_cli.detect_cli_api_error(resilient_parse_message(roh, sdk_parse))
    assert claude_cli.cli_refusal_signal_of_turn(turn)["category"] == CATEGORY


# ── 2. the verdict on the wire ──────────────────────────────────────────────


def test_umschlag_422_modell_abbruch_ohne_retry_after():
    e = CliModellAbbruch(None, "unknown", FLAGGED, WORKER,
                         {"stop_reason": "refusal", "category": CATEGORY,
                          "quelle": "model_refusal_no_fallback"})
    resp = classify_exception(e)
    err = json.loads(resp.body)["error"]
    assert resp.status_code == 422
    assert "retry-after" not in {k.lower() for k in resp.headers}
    assert err["code"] == "modell_abbruch" and err["reason"] == "modell_abbruch"
    assert err["fehler_code"] == "modell_abbruch"
    assert err["retryable"] is False and err["retry_after_s"] is None
    assert err["stop_reason"] == "refusal" and err["category"] == CATEGORY
    assert err["refusal_quelle"] == "model_refusal_no_fallback"
    assert "safeguards flagged" in err["message"]  # the CLI text stays readable
    assert isinstance(e, CliApiError) and e.retryable is False and e.retry_after_s is None


@pytest.mark.parametrize("status", [None, 429, 529, 500])
def test_kapazitaet_bleibt_429_retryable(status):
    resp = classify_exception(CliApiError(status, "unknown", "API Error", WORKER))
    err = json.loads(resp.body)["error"]
    assert resp.status_code == 429 and err["retryable"] is True
    assert err["retry_after_s"] == 30 and resp.headers["Retry-After"] == "30"
    assert err["reason"] == "claude_upstream_error"


# ── 3. the real run_completion ──────────────────────────────────────────────


def _make_cli(tmp_path: Path) -> ClaudeCodeCLI:
    cli = object.__new__(ClaudeCodeCLI)
    cli.timeout = 30
    cli.cwd = tmp_path
    cli.claude_env_vars = {}
    cli.cache_dir = tmp_path
    cli.max_cache_size_mb = 10
    cli.file_discovery = MagicMock()
    return cli


def _query(*messages, seen_options=None):
    async def query(prompt, options):
        if seen_options is not None:
            seen_options.append(options)
        for m in messages:
            yield m

    return query


async def _laufen(monkeypatch, tmp_path, messages, **kw):
    seen = []
    monkeypatch.setattr(claude_cli, "query", _query(*messages, seen_options=seen))
    got = []
    async for c in _make_cli(tmp_path).run_completion(prompt="Hallo", model="m", **kw):
        got.append(c)
    return got, seen


@pytest.mark.parametrize("form", [HART_FK32, HART_OHNE_RETRY])
async def test_hartes_refusal_wird_modell_abbruch(monkeypatch, tmp_path, form):
    with pytest.raises(CliModellAbbruch) as ei:
        await _laufen(monkeypatch, tmp_path, form())
    e = ei.value
    assert e.retryable is False and e.fehler_code == "modell_abbruch"
    assert e.refusal["category"] == CATEGORY
    assert e.usage["cache_read_tokens"] == 911320  # the withheld result's usage


async def test_nur_systemmeldung_ohne_stop_am_chunk_wird_nicht_angenommen(monkeypatch, tmp_path):
    """Retry off and the stop fields lost on the chunk: no meta turn either,
    so only model_refusal_no_fallback says it — the run must not pass."""
    with pytest.raises(CliModellAbbruch) as ei:
        await _laufen(monkeypatch, tmp_path,
                      (sdk.init(), _turn("<teil nr=\"1\">…"), _no_fallback(), _ok_result()))
    assert ei.value.refusal["quelle"] == "model_refusal_no_fallback"
    assert ei.value.usage is not None


async def test_weiches_refusal_bleibt_content_filter(monkeypatch, tmp_path):
    """The stop on the chunk, no API error: unchanged BRREF path (chat answers
    200 content_filter, research/doc-agent modell_abbruch)."""
    got, _ = await _laufen(monkeypatch, tmp_path, (
        sdk.init(), _turn("Teil", stop_reason="refusal", category=CATEGORY),
        _no_fallback(), _ok_result()))
    ab = claude_cli.lauf_abschluss(got)
    assert ab.finish_reason == "content_filter" and ab.category == CATEGORY


@pytest.mark.parametrize("form,status", [(KAPAZITAET_529, 529), (KAPAZITAET_OHNE_STATUS, None)])
async def test_kapazitaetsfehler_bleibt_cli_api_error(monkeypatch, tmp_path, form, status):
    with pytest.raises(CliApiError) as ei:
        await _laufen(monkeypatch, tmp_path, form())
    assert not isinstance(ei.value, CliModellAbbruch)
    assert ei.value.status == status and ei.value.retryable is True


async def test_sdk_absturz_nach_refusal_bleibt_refusal(monkeypatch, tmp_path):
    async def query(prompt, options):
        yield sdk.init()
        yield _no_fallback()
        raise RuntimeError("Command failed with exit code 1")

    monkeypatch.setattr(claude_cli, "query", query)
    with pytest.raises(CliModellAbbruch):
        async for _ in _make_cli(tmp_path).run_completion(prompt="Hallo", model="m"):
            pass


# ── 4. the follow-up turn: CLI retry off, other turns untouched ─────────────


async def test_cli_retry_nach_refusal_ist_aus(monkeypatch, tmp_path):
    got, seen = await _laufen(monkeypatch, tmp_path, (sdk.init(), _turn("ok"), _ok_result()),
                              max_thinking_tokens=1024)
    env = seen[0].env
    assert env["CLAUDE_CODE_DISABLE_REFUSAL_RETRY"] == "1"
    assert env["MAX_THINKING_TOKENS"] == "1024"  # other env of the run kept


async def test_schalter_erlaubt_den_retry_wieder(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIDGE_CLI_REFUSAL_RETRY", "true")
    _, seen = await _laufen(monkeypatch, tmp_path, (sdk.init(), _turn("ok"), _ok_result()))
    assert "CLAUDE_CODE_DISABLE_REFUSAL_RETRY" not in (seen[0].env or {})


async def test_normaler_mehrzug_lauf_unberuehrt(monkeypatch, tmp_path):
    """Tool turns and the max_output_tokens continuation run as before: the
    switch is a refusal-only CLI gate, and nothing in the stream changes."""
    tool = sdk.AssistantMessage(content=[{"type": "tool_use", "id": "t1", "name": "Read",
                                          "input": {}}], model="m")
    tool._bridge_message_id = "msg_t"
    fortsetzung = UserMessage(claude_cli.CLI_CONTINUATION_SENTENCE + " — no apology")
    cut = _turn("erster Teil", stop_reason="max_tokens", msg_id="msg_a")
    rest = _turn("zweiter Teil", stop_reason="end_turn", msg_id="msg_b")
    got, _ = await _laufen(monkeypatch, tmp_path,
                           (sdk.init(), tool, UserMessage("tool result"), cut,
                            fortsetzung, rest, _ok_result()))
    ab = claude_cli.lauf_abschluss(got)
    # zuege counts model calls that reported usage (the tool turn here has none)
    assert not ab.modell_abbruch and [z["message_id"] for z in ab.zuege] == ["msg_a", "msg_b"]
    assert ab.finish_reason == "length"  # continued answer, as BRTRUNC reports it


# ── 5. every path ───────────────────────────────────────────────────────────


def _echter_cli(monkeypatch, tmp_path, *messages):
    cli = _make_cli(tmp_path)
    monkeypatch.setattr(claude_cli, "query", _query(*messages))
    monkeypatch.setattr(main.claude_cli, "run_completion", cli.run_completion)


def _chat(monkeypatch, tmp_path, stream: bool, messages):
    from starlette.testclient import TestClient

    from src.middleware.adaptive_limiter import adaptive_limit_dependency

    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("CLAUDE_SKIP_AUTH", "1")
    _echter_cli(monkeypatch, tmp_path, *messages)

    async def _kein_limiter():
        return None

    main.app.dependency_overrides[adaptive_limit_dependency] = _kein_limiter
    try:
        with (
            patch("src.main.validate_claude_code_auth", return_value=(True, {"method": "test"})),
            patch("src.main.verify_api_key", new_callable=AsyncMock),
            patch("src.main.enforce_pool_admission", new_callable=AsyncMock),
            patch("src.main._cross_worker_retry", new=AsyncMock(return_value=None)),
            patch("src.providers.fallback.get_fallback_tiers", side_effect=lambda t, **kw: [t]),
        ):
            return TestClient(main.app, raise_server_exceptions=False).post(
                "/v1/chat/completions",
                headers={"X-App-ID": "werking-energy"},
                json={"model": "claude-sonnet-4-5", "stream": stream,
                      "messages": [{"role": "user", "content": "ping"}]},
            )
    finally:
        main.app.dependency_overrides.clear()
        if main.limiter is not None:
            main.limiter.reset()


def _ledger(monkeypatch):
    buch = AsyncMock(return_value="row")
    monkeypatch.setattr("src.activity.ai_call_writer.persist_ai_call_activity", buch)
    return buch


def _gebucht_mit(buch, code):
    return [c.kwargs for c in buch.await_args_list if c.kwargs.get("error_code") == code]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("form", [HART_FK32, HART_OHNE_RETRY])
def test_chat_422_modell_abbruch(monkeypatch, tmp_path, stream, form):
    buch = _ledger(monkeypatch)
    resp = _chat(monkeypatch, tmp_path, stream, form())
    assert "retry-after" not in {k.lower() for k in resp.headers}
    if stream and form is HART_FK32:
        # turn 1's text was streamed before the refusal: the 200 is out, the
        # refusal arrives as event: error (CACHEFIX stream contract)
        assert resp.status_code == 200
        errs = [b for b in resp.text.split("\n\n") if b.startswith("event: error")]
        assert len(errs) == 1 and "data: [DONE]" not in resp.text
        err = json.loads(errs[0].split("data: ", 1)[1])["error"]
    else:
        assert resp.status_code == 422, resp.text[:300]
        err = resp.json()["error"]
    assert err["code"] == "modell_abbruch" and err["retryable"] is False
    assert err["retry_after_s"] is None and err["category"] == CATEGORY
    rows = _gebucht_mit(buch, "modell_abbruch")
    assert len(rows) == 1, buch.await_args_list
    row = rows[0]
    assert row["status"] == "error" and row["provider_meta"]["category"] == CATEGORY
    if not (stream and form is HART_FK32):
        # own audit row (before this: none at all); mid-stream the stream's
        # usage block books it (char estimate, as for every broken stream)
        assert row["input_tokens"] == 0
        assert row["provider_meta"]["usage_vorenthalten"]["cache_read_tokens"] == 911320


@pytest.mark.parametrize("stream", [False, True])
def test_chat_kapazitaet_bleibt_429(monkeypatch, tmp_path, stream):
    buch = _ledger(monkeypatch)
    resp = _chat(monkeypatch, tmp_path, stream, KAPAZITAET_529())
    assert resp.status_code == 429, resp.text[:300]
    err = resp.json()["error"]
    assert err["retryable"] is True and err["retry_after_s"] == 30
    assert resp.headers["Retry-After"] == "30"
    assert not _gebucht_mit(buch, "modell_abbruch")


async def test_chat_stream_nach_erstem_chunk_meldet_modell_abbruch(monkeypatch, tmp_path):
    from src.models import ChatCompletionRequest, Message

    buch = _ledger(monkeypatch)
    _echter_cli(monkeypatch, tmp_path, sdk.init(), _turn("Teil 1"), *HART_OHNE_RETRY()[1:])
    req = ChatCompletionRequest(model="claude-sonnet-5", stream=True,
                                messages=[Message(role="user", content="hi")])
    fake_request = MagicMock()
    monkeypatch.setattr(main, "get_tenant_from_request", lambda r: None)
    out = [c async for c in main.generate_streaming_response(
        req, "req-brrefusal", fastapi_request=fake_request)]
    errs = [c for c in out if c.startswith("event: error\n")]
    assert len(errs) == 1 and "data: [DONE]\n\n" not in out
    err = json.loads(errs[0].split("data: ", 1)[1])["error"]
    assert err["code"] == "modell_abbruch" and err["retryable"] is False
    assert _gebucht_mit(buch, "modell_abbruch")


def _research_req():
    req = MagicMock()
    for k, v in dict(query="Kennwerte von X-100?", model="claude-sonnet-4-5", depth="quick",
                     strategy="planning", max_turns=10, max_hops=None,
                     confidence_threshold=0.7, parallel_searches=5, source_filter=None,
                     output_path=None, async_mode=False, backend=None, privacy=None,
                     bedrock_region=None, research_mode=None, antwort_im_text=True).items():
        setattr(req, k, v)
    return req


@pytest.mark.parametrize("form", [HART_FK32, HART_OHNE_RETRY])
async def test_research_modell_abbruch(monkeypatch, tmp_path, form):
    buch = _ledger(monkeypatch)
    _echter_cli(monkeypatch, tmp_path, *form())
    res = await main._execute_research_impl(_research_req(), None, request=MagicMock())
    assert res.status == "error" and res.fehler_code == "modell_abbruch"
    assert res.retryable is False and res.retry_after_s is None
    assert _gebucht_mit(buch, "modell_abbruch")


async def test_research_kapazitaet_bleibt_retryable(monkeypatch, tmp_path):
    buch = _ledger(monkeypatch)
    _echter_cli(monkeypatch, tmp_path, *KAPAZITAET_529())
    res = await main._execute_research_impl(_research_req(), None, request=MagicMock())
    assert res.status == "error" and res.fehler_code is None
    assert res.retryable is True and res.retry_after_s == 30
    assert _gebucht_mit(buch, "research_error") and not _gebucht_mit(buch, "modell_abbruch")


@pytest.mark.parametrize("form", [
    HART_FK32, HART_OHNE_RETRY,
    # the soft stop (BRREF) now carries the code too
    lambda: (sdk.init(), _turn("Teil", stop_reason="refusal", category=CATEGORY), _ok_result()),
])
async def test_doc_agent_modell_abbruch(monkeypatch, tmp_path, form):
    from src.models import DocAgentFile, DocAgentRequest

    buch = _ledger(monkeypatch)
    _echter_cli(monkeypatch, tmp_path, *form())
    req = DocAgentRequest(question="Was steht drin?",
                          files=[DocAgentFile(name="a.txt", content="x")])
    res = await main._execute_doc_agent_impl(req)
    assert res.status == "error" and res.fehler_code == "modell_abbruch"
    assert res.retryable is False and res.retry_after_s is None
    assert _gebucht_mit(buch, "modell_abbruch")


async def test_doc_agent_kapazitaet_bleibt_retryable(monkeypatch, tmp_path):
    from src.models import DocAgentFile, DocAgentRequest

    _ledger(monkeypatch)
    _echter_cli(monkeypatch, tmp_path, *KAPAZITAET_529())
    req = DocAgentRequest(question="Was steht drin?",
                          files=[DocAgentFile(name="a.txt", content="x")])
    res = await main._execute_doc_agent_impl(req)
    assert res.status == "error" and res.fehler_code is None
    assert res.retryable is True and res.retry_after_s == 30


# ── 6. jobs: the error row carries code, verdict and the reason field ───────


def _abbruch_umschlag():
    return classify_exception(CliModellAbbruch(
        None, "unknown", FLAGGED, WORKER,
        {"stop_reason": "refusal", "category": CATEGORY, "quelle": "api_error_turn"}))


async def _job_lauf(kind, answer):
    from tests.jobs.test_error_verdict_br9 import _run, _Seam

    seam = _Seam()
    executor = None
    if kind == "doc-agent":
        from src.jobs import executors
        executor = executors.doc_agent_executor
    await _run(kind, seam, answer=answer, executor=executor)
    return seam


async def test_chat_job_endet_endgueltig_mit_grund():
    resp = _abbruch_umschlag()
    seam = await _job_lauf("chat", httpx.Response(resp.status_code, content=resp.body,
                                                  headers={"content-type": "application/json"}))
    assert not seam.defer_job.called  # 422 is never parked like a 429
    assert seam.verdict() == ("UPSTREAM_HTTP_422", False, None)
    assert seam.mark_error.await_args.kwargs["details"] == {"fehler_code": "modell_abbruch"}


async def test_chat_job_kapazitaet_wird_zurueckgestellt():
    resp = classify_exception(CliApiError(529, "unknown", "API Error: 529", WORKER))
    seam = await _job_lauf("chat", httpx.Response(resp.status_code, content=resp.body,
                                                  headers={"content-type": "application/json",
                                                           **dict(resp.headers)}))
    assert seam.defer_job.called and not seam.mark_error.called  # unchanged: parked, retried


@pytest.mark.parametrize("kind", ["research", "doc-agent"])
async def test_research_und_doc_agent_job_tragen_grund(kind):
    answer = httpx.Response(200, json={"status": "error", "query": "q", "model": "m",
                                       "question": "q", "error": "refused",
                                       "fehler_code": "modell_abbruch",
                                       "retryable": False, "retry_after_s": None})
    seam = await _job_lauf(kind, answer)
    assert seam.verdict() == ("EXECUTOR_ERROR", False, None)
    assert seam.mark_error.await_args.kwargs["details"] == {"fehler_code": "modell_abbruch"}
