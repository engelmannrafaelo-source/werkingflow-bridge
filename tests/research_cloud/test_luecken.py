"""Lücken-Nachkontrolle (src/research_cloud/luecken.py) and its one return round.

Fixtures are neutral on purpose (Auftrag: no manufacturer, type, standard or
customer names from the case that triggered this). External HTTP is mocked
(repo convention, no respx).
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.research_cloud.executor import ResearchCloudExecutorError, run_research_cloud
from src.research_cloud.library import LibraryConfig
from src.research_cloud.luecken import (
    FLAG,
    LUECKEN_SCHEMA,
    PRUEF_WERKZEUG,
    Luecke,
    LueckenKonfigFehler,
    LueckenPruefFehler,
    Nachkontrolle,
    PruefAntwort,
    WerkzeugAufruf,
    baue_pruefanfrage,
    baue_rueckrunden_auftrag,
    lies_befund,
    messages_api_pruefer,
    nachkontrolle_aktiv,
    offene_luecken,
    pool_pruefer,
    protokoll_aus_cloud_nachrichten,
    protokoll_aus_pool_chunks,
)
from src.research_cloud.perplexity import PerplexityConfig


@pytest.fixture(autouse=True)
def _anonymizer_service_on(monkeypatch):
    monkeypatch.setenv("BRIDGE_ANONYMIZE_ENABLED", "true")


LUECKE_NORM = {"punkt": "Norm ABC 123 (aktuelle Ausgabe)", "art": "norm", "gesucht": False, "gelesen": False}
LUECKE_TYP = {"punkt": "Schallleistung Gerät Typ X-100", "art": "kennwert", "gesucht": False, "gelesen": False}
LUECKE_GESUCHT = {"punkt": "Abmessungen Typ X-100", "art": "kennwert", "gesucht": True, "gelesen": False}
LUECKE_SONST = {"punkt": "Preis auf Anfrage", "art": "sonstiges", "gesucht": False, "gelesen": False}


def _pruefer(*antworten):
    """A fake checker answering in order; an Exception instance is raised."""
    folge = list(antworten)
    aufrufe = []

    async def pruefe(system, anfrage):
        aufrufe.append(anfrage)
        a = folge.pop(0)
        if isinstance(a, Exception):
            raise a
        return PruefAntwort({"luecken": a}, 100, 20, 0.001)

    pruefe.aufrufe = aufrufe
    return pruefe


# --- switch ---------------------------------------------------------------

def test_flag_unset_follows_perplexity(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    assert nachkontrolle_aktiv(True) is True
    assert nachkontrolle_aktiv(False) is False


def test_flag_off_wins(monkeypatch):
    monkeypatch.setenv(FLAG, "false")
    assert nachkontrolle_aktiv(True) is False


def test_flag_on_without_perplexity_is_a_config_error(monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    with pytest.raises(LueckenKonfigFehler):
        nachkontrolle_aktiv(False)
    assert nachkontrolle_aktiv(True) is True


def test_flag_garbage_is_a_config_error(monkeypatch):
    monkeypatch.setenv(FLAG, "vielleicht")
    with pytest.raises(LueckenKonfigFehler):
        nachkontrolle_aktiv(True)


# --- tool log ---------------------------------------------------------------

def test_cloud_log_covers_client_and_server_tools():
    nachrichten = [
        {"role": "user", "content": "frage"},
        {"role": "assistant", "content": [
            {"type": "server_tool_use", "name": "web_search", "input": {"query": "suche eins"}},
            {"type": "tool_use", "name": "perplexity_search", "input": {"frage": "frage zwei"}},
            {"type": "tool_use", "name": "fetch_document", "input": {"url": "https://a.example/d.pdf", "suchbegriff": "X"}},
            {"type": "server_tool_use", "name": "web_fetch", "input": {"url": "https://b.example/"}},
            {"type": "tool_use", "name": "library_get", "input": {"id": "x"}},
            {"type": "text", "text": "..."},
        ]},
    ]
    assert [(a.werkzeug, a.eingabe) for a in protokoll_aus_cloud_nachrichten(nachrichten)] == [
        ("web_search", "suche eins"),
        ("perplexity_search", "frage zwei"),
        ("fetch_document", "https://a.example/d.pdf suchbegriff=X"),
        ("web_fetch", "https://b.example/"),
    ]


def test_pool_log_reads_sdk_blocks_and_dicts():
    chunks = [
        {"content": [
            SimpleNamespace(name="mcp__perplexity__perplexity_search", input={"frage": "f1"}),
            SimpleNamespace(name="Read", input={"file_path": "x"}),
            {"name": "WebSearch", "input": {"query": "q1"}},
            SimpleNamespace(name="mcp__dokument__fetch_document", input={"url": "https://c.example/p.pdf"}),
        ]},
        {"type": "x_claude_metadata"},
        "kein dict",
    ]
    assert [(a.werkzeug, a.eingabe) for a in protokoll_aus_pool_chunks(chunks)] == [
        ("perplexity_search", "f1"),
        ("web_search", "q1"),
        ("fetch_document", "https://c.example/p.pdf"),
    ]


def test_request_carries_log_and_report_and_says_when_empty():
    anfrage = baue_pruefanfrage("Bericht-Text", [])
    assert "keine Such- oder Lese-Aufrufe" in anfrage and "Bericht-Text" in anfrage
    anfrage = baue_pruefanfrage("B", [WerkzeugAufruf("perplexity_search", "frage")])
    assert "1. perplexity_search: frage" in anfrage


# --- answer parsing ---------------------------------------------------------

def test_reads_dict_fenced_and_embedded_json():
    assert len(lies_befund({"luecken": [LUECKE_NORM]}).luecken) == 1
    assert len(lies_befund("```json\n" + json.dumps({"luecken": [LUECKE_NORM]}) + "\n```").luecken) == 1
    assert lies_befund('Hier: {"luecken": []} fertig').luecken == []


@pytest.mark.parametrize("daten", [
    "keine Lücken",                                  # no JSON at all
    '{"luecken": [',                                 # broken JSON
    {"luecken": [{"punkt": "x", "art": "erfunden", "gesucht": False, "gelesen": False}]},
    {"falsch": []},
    ["liste"],
])
def test_answer_outside_schema_is_a_named_error(daten):
    with pytest.raises(LueckenPruefFehler):
        lies_befund(daten)


def test_open_gaps_are_unsearched_norm_product_value():
    befund = lies_befund({"luecken": [LUECKE_NORM, LUECKE_TYP, LUECKE_GESUCHT, LUECKE_SONST]})
    assert [l.punkt for l in offene_luecken(befund)] == [LUECKE_NORM["punkt"], LUECKE_TYP["punkt"]]


def test_return_round_message_lists_points_budgets_and_target():
    text = baue_rueckrunden_auftrag(
        [Luecke(**LUECKE_NORM)], perplexity_werkzeug="P", dokument_werkzeug="D",
        perplexity_rest=3, dokument_rest=7, ziel="ZIELSATZ",
    )
    assert LUECKE_NORM["punkt"] in text and "noch 3 Aufrufe" in text and "noch 7 Aufrufe" in text
    assert "Interpretation" in text and "ZIELSATZ" in text and "einzige Nachkontrolle" in text


# --- decision ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_gaps_no_round():
    nk = Nachkontrolle(pruefer=_pruefer([]))
    assert await nk.entscheide("bericht", [], perplexity_rest=5) is None
    meta = nk.as_meta()
    assert meta["luecken_gemeldet"] == 0 and meta["rueckrunde"] is False
    assert meta["rueckrunde_grund"] == "keine_offenen_luecken"


@pytest.mark.asyncio
async def test_searched_gaps_only_no_round():
    nk = Nachkontrolle(pruefer=_pruefer([LUECKE_GESUCHT, LUECKE_SONST]))
    assert await nk.entscheide("bericht", [], perplexity_rest=5) is None
    assert nk.luecken_gemeldet == 2 and nk.luecken_ohne_suche == 0


@pytest.mark.asyncio
async def test_unsearched_gaps_with_budget_get_the_round():
    nk = Nachkontrolle(pruefer=_pruefer([LUECKE_NORM, LUECKE_GESUCHT]))
    offene = await nk.entscheide("bericht", [], perplexity_rest=1)
    assert [l.punkt for l in offene] == [LUECKE_NORM["punkt"]]
    assert nk.rueckrunde is True and nk.luecken_ohne_suche == 1
    assert nk.pruef_input_tokens == 100 and nk.pruef_kosten_usd == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_no_budget_no_round_and_says_so():
    nk = Nachkontrolle(pruefer=_pruefer([LUECKE_NORM]))
    assert await nk.entscheide("bericht", [], perplexity_rest=0) is None
    assert nk.rueckrunde_grund == "budget_erschoepft" and nk.luecken_ohne_suche == 1


@pytest.mark.asyncio
async def test_checker_failure_is_named_not_no_gaps():
    nk = Nachkontrolle(pruefer=_pruefer(LueckenPruefFehler("HTTP 529")))
    assert await nk.entscheide("bericht", [], perplexity_rest=5) is None
    meta = nk.as_meta()
    assert meta["rueckrunde_grund"] == "pruefung_fehlgeschlagen"
    assert meta["luecken_gemeldet"] is None  # unknown, not 0
    assert "HTTP 529" in meta["luecken_pruef_fehler"][0]


@pytest.mark.asyncio
async def test_empty_report_is_not_checked():
    pruefe = _pruefer()
    nk = Nachkontrolle(pruefer=pruefe)
    assert await nk.entscheide("  ", [], perplexity_rest=5) is None
    assert nk.rueckrunde_grund == "kein_bericht" and pruefe.aufrufe == []


@pytest.mark.asyncio
async def test_only_one_decision_per_run():
    nk = Nachkontrolle(pruefer=_pruefer([LUECKE_NORM], [LUECKE_NORM]))
    await nk.entscheide("bericht", [], perplexity_rest=5)
    with pytest.raises(RuntimeError):
        await nk.entscheide("bericht", [], perplexity_rest=5)


# --- checker implementations ------------------------------------------------

@pytest.mark.asyncio
async def test_messages_api_checker_forces_the_schema_tool():
    gesendet = {}

    def handler(request):
        gesendet.update(json.loads(request.content))
        return httpx.Response(200, json={
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1000, "output_tokens": 100},
            "content": [{"type": "tool_use", "name": PRUEF_WERKZEUG, "input": {"luecken": [LUECKE_NORM]}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        antwort = await messages_api_pruefer("sk-test", c)("SYS", "ANFRAGE")
    assert gesendet["tool_choice"] == {"type": "tool", "name": PRUEF_WERKZEUG}
    assert gesendet["tools"][0]["input_schema"] == LUECKEN_SCHEMA
    assert "haiku" in gesendet["model"]
    assert lies_befund(antwort.daten).luecken[0].art == "norm"
    assert antwort.input_tokens == 1000 and antwort.kosten_usd > 0


@pytest.mark.asyncio
async def test_messages_api_checker_errors_are_named():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(529, text="overloaded"))) as c:
        with pytest.raises(LueckenPruefFehler, match="529"):
            await messages_api_pruefer("sk-test", c)("S", "A")
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"content": [{"type": "text", "text": "x"}], "stop_reason": "end_turn"})
    )) as c:
        with pytest.raises(LueckenPruefFehler, match=PRUEF_WERKZEUG):
            await messages_api_pruefer("sk-test", c)("S", "A")
    with pytest.raises(LueckenPruefFehler, match="Schlüssel"):
        await messages_api_pruefer(None)("S", "A")


@pytest.mark.asyncio
async def test_pool_checker_asks_for_json_and_validates():
    lauf = AsyncMock(return_value=(json.dumps({"luecken": [LUECKE_TYP]}), 500, 50))
    antwort = await pool_pruefer(lauf)("SYS", "ANFRAGE")
    system, prompt = lauf.await_args.args
    assert system == "SYS" and prompt.startswith("ANFRAGE") and '"luecken"' in prompt
    assert lies_befund(antwort.daten).luecken[0].art == "kennwert"
    with pytest.raises(LueckenPruefFehler):
        await pool_pruefer(AsyncMock(return_value=("", 0, 0)))("S", "A")


# --- executor: the one return round (cloud path) ------------------------------

_PPLX_ON = PerplexityConfig(enabled=True, api_key="pplx-test", max_retries=0)

ALT = "# Bericht\n" + "Befund. " * 60 + "\nNorm ABC 123: nicht auffindbar."
NEU = "# Bericht\n" + "Befund. " * 60 + "\nNorm ABC 123: Ausgabe 2020-01 (Quelle: https://n.example/abc, S. 2)."


def _resp(status_code, body=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = body or {}
    r.text = str(body)
    return r


def _anthropic(body, inp=10, out=5):
    return _resp(200, {"model": "claude-sonnet-5", "usage": {"input_tokens": inp, "output_tokens": out}, **body})


def _end(text, **kw):
    return _anthropic({"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}, **kw)


def _tool_use(name, inp, tid="tu1"):
    return _anthropic({"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]})


def _pplx_ok():
    c = MagicMock()
    c.post = AsyncMock(return_value=_resp(200, {
        "status": "completed", "model": "sonar",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "Antwort", "annotations": []}]}],
        "usage": {"cost": {"total_cost": 0.05, "currency": "USD"}},
    }))
    return c


async def _run(antworten, nachkontrolle, *, pplx=None, perplexity_config=_PPLX_ON):
    client = MagicMock()
    client.post = AsyncMock(side_effect=antworten)
    result = await run_research_cloud(
        "frage", "system", api_key="sk-test", client=client,
        library_config=LibraryConfig(), library_index={},
        perplexity_config=perplexity_config, anonymize=AsyncMock(return_value="anonym"),
        perplexity_client=pplx or _pplx_ok(), nachkontrolle=nachkontrolle,
    )
    return result, client


@pytest.mark.asyncio
async def test_cloud_return_round_continues_same_conversation_once():
    # The re-check after the round STILL reports an unsearched gap — it must
    # be measured, and must not start a second round.
    pruefe = _pruefer([LUECKE_NORM], [LUECKE_NORM])
    nk = Nachkontrolle(pruefer=pruefe)
    result, client = await _run(
        [_end(ALT, inp=100, out=50),
         _tool_use("perplexity_search", {"frage": "Norm ABC 123 aktuelle Ausgabe"}),
         _end(NEU, inp=30, out=20)],
        nk,
    )
    assert result.content == NEU
    assert client.post.await_count == 3
    zweite = client.post.call_args_list[1].kwargs["json"]["messages"]
    assert zweite[-2]["role"] == "assistant" and zweite[-2]["content"][0]["text"] == ALT
    auftrag = zweite[-1]["content"][0]["text"]
    assert LUECKE_NORM["punkt"] in auftrag and "VOLLSTÄNDIGEN" in auftrag
    assert result.perplexity_calls == 1 and result.perplexity_cost_usd == 0.05
    # Ledger: the round's tokens are in the run's total, and also shown apart.
    assert result.usage.input_tokens == 140 and result.usage.output_tokens == 75
    meta = nk.as_meta()
    assert meta["rueckrunde"] is True and meta["luecken_ohne_suche"] == 1
    assert meta["rueckrunde_usage"]["input_tokens"] == 40
    assert meta["luecken_nach_rueckrunde"] == 1 and meta["luecken_ohne_suche_nach_rueckrunde"] == 1
    assert len(pruefe.aufrufe) == 2
    # The second check saw the round's search in the log.
    assert "perplexity_search: Norm ABC 123 aktuelle Ausgabe" in pruefe.aufrufe[1]
    assert result.iterations == 3


@pytest.mark.asyncio
async def test_cloud_no_gaps_no_extra_call():
    nk = Nachkontrolle(pruefer=_pruefer([]))
    result, client = await _run([_end(ALT)], nk)
    assert result.content == ALT and client.post.await_count == 1
    assert nk.as_meta()["rueckrunde"] is False


@pytest.mark.asyncio
async def test_cloud_failed_round_keeps_report_before():
    nk = Nachkontrolle(pruefer=_pruefer([LUECKE_NORM]))
    result, _ = await _run([_end(ALT), _resp(500, {"error": "x"})], nk)
    assert result.status == "success" and result.content == ALT
    assert "HTTP 500" in nk.as_meta()["rueckrunde_fehler"]


@pytest.mark.asyncio
async def test_cloud_fragment_answer_keeps_report_before():
    nk = Nachkontrolle(pruefer=_pruefer([LUECKE_NORM], []))
    result, _ = await _run([_end(ALT), _end("Ergänzt: Norm gefunden.")], nk)
    assert result.content == ALT
    assert "keinen vollständigen Bericht" in nk.as_meta()["rueckrunde_fehler"]


@pytest.mark.asyncio
async def test_cloud_checker_failure_leaves_report_and_names_it():
    nk = Nachkontrolle(pruefer=_pruefer(LueckenPruefFehler("kaputt")))
    result, client = await _run([_end(ALT)], nk)
    assert result.content == ALT and client.post.await_count == 1
    assert nk.as_meta()["luecken_pruef_fehler"]


@pytest.mark.asyncio
async def test_cloud_nachkontrolle_without_perplexity_refuses():
    with pytest.raises(ResearchCloudExecutorError, match="Nachkontrolle"):
        await _run([_end(ALT)], Nachkontrolle(pruefer=_pruefer([])), perplexity_config=PerplexityConfig())


@pytest.mark.asyncio
async def test_cloud_without_nachkontrolle_unchanged():
    result, client = await _run([_end(ALT)], None)
    assert result.content == ALT and client.post.await_count == 1 and result.iterations == 1
