"""Credit pool: one account per research run (encrypted_content binding).

Anthropic binds encrypted_content of web_search_tool_result blocks to the
organisation/key that produced them. A run on the promotional pool therefore
pins the account of its first successful call; a later call of the same run
(pause_turn continuation, Lücken return round) must not switch account.
Neutral fixtures, external HTTP mocked (repo convention, no respx).
"""
import json

import httpx
import pytest
from fastapi import HTTPException

from src.providers import credit_pool
from src.providers.credit_pool import PinnedAccountLost
from src.research_cloud.executor import ResearchCloudExecutorError, run_research_cloud
from src.research_cloud.library import LibraryConfig
from src.research_cloud.luecken import Nachkontrolle, PruefAntwort
from src.research_cloud.models import ResearchCloudConfig
from src.research_cloud.perplexity import PerplexityConfig

TOOLS = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
ENC_400 = {"type": "error", "error": {"type": "invalid_request_error",
           "message": "messages.1.content.0: Invalid `encrypted_content` in `search_result` block"}}


def _acc(name):
    return {"id": name, "org_id": "org-" + name, "grant_id": "g-" + name, "api_key": "key-" + name}


class Pool:
    """Fake coordinator + fake Messages API. The API refuses encrypted_content
    produced under another key (the binding the pin protects)."""

    def __init__(self, names, count_status=None):
        self.accounts = [_acc(n) for n in names]
        self.origin = {}
        self.keys_used = []
        self.count_status = count_status or {}  # key -> (status, body) for count_tokens
        self.coord_status = None

    async def coordinator(self, method, path, data=None):
        if path == "/accounts":
            if self.coord_status:
                raise HTTPException(self.coord_status, "Anthropic credit coordinator rejected request")
            return {"accounts": list(self.accounts)}
        return {"state": "reserved"}

    def handler(self, req):
        key = req.headers["x-api-key"]
        body = json.loads(req.content)
        if req.url.path.endswith("count_tokens") and key in self.count_status:
            status, js = self.count_status[key]
            return httpx.Response(status, json=js)
        for m in body["messages"]:
            if isinstance(m["content"], list):
                for b in m["content"]:
                    if b.get("type") == "web_search_tool_result" and self.origin.get(b["encrypted_content"]) != key:
                        return httpx.Response(400, json=ENC_400)
        if req.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 10})
        self.keys_used.append(key)
        tok = f"enc-{len(self.origin)}"
        self.origin[tok] = key
        return httpx.Response(200, json={
            "usage": {"input_tokens": 1, "output_tokens": 1}, "model": "claude-sonnet-5-5",
            "stop_reason": "end_turn",
            "content": [{"type": "web_search_tool_result", "tool_use_id": "s1", "encrypted_content": tok}]})


@pytest.fixture
def pool(monkeypatch):
    def make(names, **kw):
        p = Pool(names, **kw)
        original = httpx.AsyncClient
        monkeypatch.setattr(credit_pool, "coordinator", p.coordinator)
        monkeypatch.setattr(credit_pool.httpx, "AsyncClient",
                            lambda **k: original(transport=httpx.MockTransport(p.handler), **k))
        return p
    return make


def _body(messages):
    return {"model": "claude-sonnet-5-5", "max_tokens": 10, "tools": TOOLS, "messages": messages}


def _follow_up(blocks):
    return _body([{"role": "user", "content": "q"}, {"role": "assistant", "content": blocks},
                  {"role": "user", "content": "Rückrunde"}])


@pytest.mark.asyncio
async def test_first_success_pins_and_follow_up_stays_on_it(pool):
    p = pool(["one", "two"])
    pin = {}
    r, meta = await credit_pool.post_messages(_body([{"role": "user", "content": "q"}]), 10, pin=pin)
    assert r.status_code == 200 and pin == {"account_id": "one"} and meta["credit_pool_account"] == "one"
    # The order changes between the rounds: "two" is listed first now.
    p.accounts = [_acc("two"), _acc("one")]
    r, meta = await credit_pool.post_messages(_follow_up(r.json()["content"]), 10, pin=pin)
    assert r.status_code == 200 and meta["credit_pool_account"] == "one"
    assert p.keys_used == ["key-one", "key-one"]


@pytest.mark.asyncio
async def test_pinned_account_gone_is_loud_409_without_switch(pool):
    p = pool(["one", "two"])
    pin = {}
    r, _ = await credit_pool.post_messages(_body([{"role": "user", "content": "q"}]), 10, pin=pin)
    p.accounts = [_acc("two")]
    with pytest.raises(PinnedAccountLost) as e:
        await credit_pool.post_messages(_follow_up(r.json()["content"]), 10, pin=pin)
    assert e.value.status_code == 409 and "one" in e.value.detail
    assert p.keys_used == ["key-one"]  # "two" never generated anything


@pytest.mark.asyncio
async def test_without_pin_the_switch_reproduces_the_400(pool):
    """The defect this branch fixes, as before (pin=None path unchanged)."""
    p = pool(["one", "two"])
    r, _ = await credit_pool.post_messages(_body([{"role": "user", "content": "q"}]), 10)
    p.accounts = [_acc("two")]
    r2, _ = await credit_pool.post_messages(_follow_up(r.json()["content"]), 10)
    assert r2.status_code == 400 and "encrypted_content" in r2.text


@pytest.mark.asyncio
async def test_pinned_account_refusing_at_count_is_409(pool):
    """count_tokens 429 on the pinned account: no other account, loud 409."""
    p = pool(["one", "two"])
    pin = {"account_id": "one"}
    p.count_status["key-one"] = (429, {"error": "rate limited"})
    with pytest.raises(PinnedAccountLost):
        await credit_pool.post_messages(_body([{"role": "user", "content": "q"}]), 10, pin=pin)
    assert p.keys_used == []


@pytest.mark.asyncio
async def test_count_400_account_bound_before_pin_moves_on_after_pin_is_loud(pool):
    p = pool(["one", "two"])
    p.count_status["key-one"] = (400, ENC_400)
    pin = {}
    r, meta = await credit_pool.post_messages(_body([{"role": "user", "content": "q"}]), 10, pin=pin)
    assert r.status_code == 200 and meta["credit_pool_account"] == "two" and pin == {"account_id": "two"}
    p.count_status["key-two"] = (400, ENC_400)
    with pytest.raises(PinnedAccountLost) as e:
        await credit_pool.post_messages(_body([{"role": "user", "content": "q"}]), 10, pin=pin)
    assert "count_tokens" in e.value.detail


@pytest.mark.asyncio
async def test_count_400_of_the_request_itself_is_returned_unchanged(pool):
    """A malformed request is the same on every account: no switch, no 409."""
    bad = {"type": "error", "error": {"type": "invalid_request_error", "message": "max_tokens: field required"}}
    p = pool(["one", "two"], count_status={"key-one": (400, bad), "key-two": (400, bad)})
    for pin in ({}, {"account_id": "one"}, None):
        r, meta = await credit_pool.post_messages(_body([{"role": "user", "content": "q"}]), 10, pin=pin)
        assert r.status_code == 400 and meta == {}
    assert p.keys_used == []


@pytest.mark.asyncio
async def test_without_pin_count_400_is_returned_unchanged_like_before(pool):
    """Vision/checker callers (no pin): an account-bound count 400 still comes
    back as it did, no switch to the next account."""
    pool(["one", "two"], count_status={"key-one": (400, ENC_400)})
    r, meta = await credit_pool.post_messages(_body([{"role": "user", "content": "q"}]), 10)
    assert r.status_code == 400 and meta == {}


@pytest.mark.asyncio
async def test_coordinator_409_is_not_a_lost_pin(pool):
    p = pool(["one"])
    p.coord_status = 409
    with pytest.raises(HTTPException) as e:
        await credit_pool.post_messages(_body([{"role": "user", "content": "q"}]), 10, pin={"account_id": "one"})
    assert e.value.status_code == 409 and not isinstance(e.value, PinnedAccountLost)


# --- executor: pin per run, return round --------------------------------------
ALT = "# Bericht\n" + "Befund. " * 60 + "\nNorm ABC 123: nicht auffindbar."
LUECKE = {"punkt": "Norm ABC 123 (aktuelle Ausgabe)", "art": "norm", "gesucht": False, "gelesen": False}


def _pruefer(*antworten):
    folge = list(antworten)

    async def pruefe(system, anfrage):
        return PruefAntwort({"luecken": folge.pop(0)}, 100, 20, 0.001)
    return pruefe


def _answer(stop, text=ALT):
    return httpx.Response(200, json={
        "model": "claude-sonnet-5-5", "stop_reason": stop, "usage": {"input_tokens": 1, "output_tokens": 1},
        "content": [{"type": "web_search_tool_result", "tool_use_id": "s1", "encrypted_content": "enc"},
                    {"type": "text", "text": text}]})


async def _run_pool(monkeypatch, answers, nk):
    monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_URL", "http://pool")
    monkeypatch.setenv("BRIDGE_ANONYMIZE_ENABLED", "true")
    monkeypatch.delenv("RESEARCH_CLOUD_API_KEY", raising=False)
    pins = []
    folge = list(answers)

    async def post(body, timeout, pin=None):
        pins.append(pin)
        a = folge.pop(0)
        if isinstance(a, Exception):
            raise a
        pin.setdefault("account_id", "one")
        return a, {"credit_pool_account": "one"}
    monkeypatch.setattr(credit_pool, "post_messages", post)
    result = await run_research_cloud(
        "frage", "system", config=ResearchCloudConfig(promotional_only=True),
        library_config=LibraryConfig(enabled=False), perplexity_config=PerplexityConfig(enabled=False),
        nachkontrolle=nk, lauf_bezug="job=job_test workflow=wf_test")
    return result, pins


@pytest.mark.asyncio
async def test_pause_turn_and_return_round_share_one_pin(monkeypatch, caplog):
    caplog.set_level("INFO", logger="src.research_cloud.executor")
    nk = Nachkontrolle(pruefer=_pruefer([LUECKE], []))
    result, pins = await _run_pool(
        monkeypatch, [_answer("pause_turn"), _answer("end_turn"), _answer("end_turn", ALT + " ergänzt")], nk)
    assert result.status == "success" and len(pins) == 3
    assert pins[0] is pins[1] is pins[2] and pins[0] == {"account_id": "one"}
    zeilen = [r.getMessage() for r in caplog.records if "research-cloud pool call:" in r.getMessage()]
    assert len(zeilen) == 3
    assert "runde=bericht" in zeilen[0] and "runde=rueckrunde" in zeilen[2]
    assert all("konto=one" in z and "job=job_test" in z for z in zeilen)
    assert not any("key-" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_lost_pin_in_return_round_keeps_report_and_names_it(monkeypatch):
    nk = Nachkontrolle(pruefer=_pruefer([LUECKE]))
    lost = PinnedAccountLost("pinned credit account one unavailable; no switch to another account")
    result, _ = await _run_pool(monkeypatch, [_answer("end_turn"), lost], nk)
    assert result.status == "success" and result.content == ALT
    meta = nk.as_meta()
    assert meta["nachkontrolle_status"] == "gescheitert"
    assert "pinned credit account one" in meta["rueckrunde_fehler"]
    assert nk.status()["grund"] == meta["rueckrunde_fehler"]


@pytest.mark.parametrize("fehler", [
    HTTPException(429, "No eligible Anthropic API credit budget available; paid fallback disabled"),
    HTTPException(409, "Anthropic credit coordinator rejected request"),  # coordinator 409, not a lost pin
    HTTPException(503, "Anthropic credit coordinator unavailable"),
])
@pytest.mark.asyncio
async def test_other_pool_errors_in_return_round_still_stop(monkeypatch, fehler):
    nk = Nachkontrolle(pruefer=_pruefer([LUECKE]))
    with pytest.raises(ResearchCloudExecutorError) as e:
        await _run_pool(monkeypatch, [_answer("end_turn"), fehler], nk)
    assert e.value.status_code == fehler.status_code and not e.value.konto_angeheftet_verloren


@pytest.mark.asyncio
async def test_lost_pin_in_first_round_stops_the_run(monkeypatch):
    nk = Nachkontrolle(pruefer=_pruefer([LUECKE]))
    lost = PinnedAccountLost("pinned credit account one unavailable")
    with pytest.raises(ResearchCloudExecutorError) as e:
        await _run_pool(monkeypatch, [_answer("pause_turn"), lost], nk)
    assert e.value.konto_angeheftet_verloren and e.value.status_code == 409


@pytest.mark.asyncio
async def test_vision_caller_passes_no_pin(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_URL", "http://pool")
    seen = []

    async def post(body, timeout, pin=None):
        seen.append(pin)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "model": "claude-haiku-5-5",
                                         "usage": {"input_tokens": 1, "output_tokens": 1}, "stop_reason": "end_turn"}), {}
    monkeypatch.setattr(credit_pool, "post_messages", post)
    from src.vision_provider import VisionProvider
    try:
        await VisionProvider().analyze([{"role": "user", "content": "test"}], model="claude-haiku-5-5")
    except Exception:
        pass  # only the call shape matters here
    assert seen == [None]
