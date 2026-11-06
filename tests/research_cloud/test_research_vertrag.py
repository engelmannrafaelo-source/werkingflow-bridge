"""P6-VERTRAG (06.11.2026): the versioned execution contract of /v1/research —
src/research_vertrag.py, its wiring into both execution paths, the admission
in the handler, the job snapshot and the self-call.

Network lock first (tests/netzsperre.py), then the usual SDK stubs; the
harness pieces come from the existing test modules (no second harness).
"""
import tests.netzsperre as netzsperre  # noqa: E402  — first: the lock is active

import sys
from unittest.mock import MagicMock as _MagicMock

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
    "src.identity.routes",
    "src.db.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

import asyncio  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

import src.main  # noqa: E402
import src.research_laufzeit as rl  # noqa: E402
import src.research_vertrag as rv  # noqa: E402
from src.models import ResearchRequest, ResearchResponse  # noqa: E402
from src.research_cloud.executor import (  # noqa: E402
    ResearchCloudExecutorError,
    ResearchCloudLaufzeitErschoepft,
    run_research_cloud,
)
from src.research_cloud.models import ResearchCloudConfig  # noqa: E402

# Existing harnesses, reused as they are.
from tests.research_cloud.test_execute_research_cloud_impl import (  # noqa: E402
    _cloud_result,
    _make_req as _cloud_req,
)
from tests.research_cloud.test_executor import _response, _usage  # noqa: E402
from tests.research_cloud.test_luecken import (  # noqa: E402
    _PPLX_ON,
    ALT,
    LUECKE_NORM,
    NEU,
    Nachkontrolle,
    _end,
    _pplx_ok,
    _pruefer,
    _tool_use,
)
from tests.research_cloud.test_research_pool_perplexity import (  # noqa: E402,F401
    _make_req as _pool_req,
    persist,
    pplx_on,
    spy_cli,
)
from tests.jobs.test_job_idempotenz_brref2 import (  # noqa: E402,F401
    APP_HEADERS,
    KEY,
    _app,
    _post,
    mem,
)
from tests.jobs.test_executors import _FakeClient, _FakeResp  # noqa: E402
from src.research_cloud.library import LibraryConfig  # noqa: E402

netzsperre.aufheben()  # collection done; re-armed for this module's tests below


@pytest.fixture(scope="module", autouse=True)
def _netzsperre():
    netzsperre.aktivieren()
    yield
    netzsperre.aufheben()


@pytest.fixture(autouse=True)
def _anonymize_gate_env(monkeypatch):
    # perplexity_search never leaves without the gate (research_cloud.perplexity)
    monkeypatch.setenv("BRIDGE_ANONYMIZE_ENABLED", "true")


# ── 1. the module ─────────────────────────────────────────────────────────────

def _body(**kw):
    return SimpleNamespace(vertrag=kw.pop("vertrag", None), max_turns=kw.pop("max_turns", None), **kw)


def test_version_bindet_grenzen_und_capabilities(monkeypatch):
    v0 = rv.version()
    assert v0.startswith(rv.SCHEMA + "+")
    monkeypatch.setenv("RESEARCH_VERTRAG_SUCHEN_MAX", "19")
    v1 = rv.version()
    assert v1 != v0
    monkeypatch.delenv("RESEARCH_VERTRAG_SUCHEN_MAX")
    assert rv.version() == v0
    # Semantics are part of the version, not only the nine numbers.
    monkeypatch.setitem(rv.PROMOTIONAL_CAP, "suchen", 2)
    assert rv.version() != v0


def test_obergrenzen_muessen_in_die_zeitkette_passen(monkeypatch):
    monkeypatch.setenv("RESEARCH_VERTRAG_GESAMTZEIT_S_MAX", str(rl.GESAMTFRIST_S + 1))
    with pytest.raises(ValueError, match="gesamtzeit_s"):
        rv.pruefe_obergrenzen()
    monkeypatch.setenv("RESEARCH_VERTRAG_GESAMTZEIT_S_MAX", "100")
    monkeypatch.setenv("RESEARCH_VERTRAG_TRANSPORTZEIT_S_MAX", "101")
    with pytest.raises(ValueError, match="transportzeit_s"):
        rv.pruefe_obergrenzen()
    monkeypatch.setenv("RESEARCH_VERTRAG_FORTSETZUNGEN_MAX", "0")
    with pytest.raises(ValueError, match="must be > 0"):
        rv.pruefe_obergrenzen()


def test_legacy_ohne_wunsch_bekommt_die_obergrenzen_und_beide_pfade():
    v = rv.vertrag_erstellen(_body(max_turns=30))
    g = rv.obergrenzen()
    assert v.version == v.server_version == rv.version()
    assert v.quelle == "anfrage" and v.verlangt == []
    assert v.grenzen["iterationen"] == 30 and v.begrenzt_durch["iterationen"] == "client"
    assert {k: v.grenzen[k] for k in rv.NAMEN if k != "iterationen"} == {k: g[k] for k in rv.NAMEN if k != "iterationen"}
    assert v.fuer_pfad("pool").pfad == "pool"
    assert v.fuer_pfad("cloud").nicht_durchsetzbar == ["iterationen"]
    assert isinstance(v.grenzen["ausgabe_tokens"], int) and isinstance(v.grenzen["gesamtzeit_s"], float)


def test_wunsch_senkt_nur_nie_erhoeht():
    v = rv.vertrag_erstellen(_body(vertrag={"ausgabe_tokens": 99999, "gesamtzeit_s": 100, "fortsetzungen": 3}))
    assert v.grenzen["ausgabe_tokens"] == rv.obergrenzen()["ausgabe_tokens"]
    assert v.begrenzt_durch["ausgabe_tokens"] == "server"
    assert v.grenzen["gesamtzeit_s"] == 100.0 and v.begrenzt_durch["gesamtzeit_s"] == "client"
    assert v.grenzen["fortsetzungen"] == 3
    # only what actually lowered counts as a demand
    assert v.verlangt == ["fortsetzungen", "gesamtzeit_s"]


def test_max_turns_und_iterationen_das_kleinere_gilt():
    assert rv.vertrag_erstellen(_body(max_turns=10, vertrag={"iterationen": 5})).grenzen["iterationen"] == 5
    assert rv.vertrag_erstellen(_body(max_turns=3, vertrag={"iterationen": 5})).grenzen["iterationen"] == 3
    assert rv.vertrag_erstellen(_body(max_turns=500)).grenzen["iterationen"] == rv.obergrenzen()["iterationen"]
    assert rv.vertrag_erstellen(_body(max_turns=MagicMock())).grenzen["iterationen"] == rv.obergrenzen()["iterationen"]


def test_unbekanntes_wunschfeld_wird_abgewiesen():
    with pytest.raises(ValueError):
        rv.vertrag_erstellen(_body(vertrag={"ausgabe_token": 10}))
    with pytest.raises(ValueError):
        rv.vertrag_erstellen(_body(vertrag={"gesamtzeit_s": 0}))


def test_pfad_lehnt_wunsch_ab_den_er_nicht_durchsetzen_kann():
    v = rv.vertrag_erstellen(_body(vertrag={"ausgabe_tokens": 1000, "gesamtzeit_s": 600}))
    with pytest.raises(rv.VertragNichtDurchsetzbar) as ei:
        v.fuer_pfad("pool")
    assert ei.value.fehler_code == rv.FEHLER_NICHT_DURCHSETZBAR and ei.value.grenzen == ["ausgabe_tokens"]
    assert "pool" in str(ei.value) and "no other path" in str(ei.value)
    assert v.fuer_pfad("cloud").grenzen["ausgabe_tokens"] == 1000
    w = rv.vertrag_erstellen(_body(vertrag={"iterationen": 5}))
    with pytest.raises(rv.VertragNichtDurchsetzbar):
        w.fuer_pfad("cloud")
    assert w.fuer_pfad("pool").grenzen["iterationen"] == 5
    # a wish at the ceiling demands nothing
    assert rv.vertrag_erstellen(_body(vertrag={"ausgabe_tokens": rv.obergrenzen()["ausgabe_tokens"]})).fuer_pfad("pool")


def test_fremde_version_nur_als_vollstaendig_bestimmter_vertrag(monkeypatch):
    alt = rv.vertrag_erstellen(_body(vertrag={"gesamtzeit_s": 1200})).als_wunsch()
    assert set(alt) == {"version", *rv.NAMEN}
    monkeypatch.setenv("RESEARCH_VERTRAG_SUCHEN_MAX", "25")  # new server policy
    neu = rv.version()
    assert alt["version"] != neu
    v = rv.vertrag_erstellen(_body(vertrag=alt))
    assert v.quelle == "snapshot" and v.version == alt["version"] and v.server_version == neu
    assert v.grenzen == {k: alt[k] for k in rv.NAMEN}
    assert set(v.begrenzt_durch.values()) == {"snapshot"}
    # the old ceiling 20 is below the new 25: under the new policy it is a
    # demand of the snapshot (checked against the path like any other)
    assert v.verlangt == ["suchen", "gesamtzeit_s"]
    # incomplete -> refused
    with pytest.raises(rv.VertragVersionAbweichend, match="not fully specified"):
        rv.vertrag_erstellen(_body(vertrag={"version": alt["version"], "gesamtzeit_s": 10}))
    # above the current ceiling -> refused
    monkeypatch.setenv("RESEARCH_VERTRAG_SUCHEN_MAX", "5")
    with pytest.raises(rv.VertragVersionAbweichend, match="above the current ceiling: suchen"):
        rv.vertrag_erstellen(_body(vertrag=alt))
    # other protocol -> refused
    with pytest.raises(rv.VertragVersionAbweichend, match="protocol"):
        rv.vertrag_erstellen(_body(vertrag={**alt, "version": "research-vertrag/0+abc"}))


def test_senken_nur_nach_unten_mit_grund():
    v = rv.vertrag_erstellen(_body()).fuer_pfad("cloud")
    s = v.senken("suchen", 10, "depth").senken("suchen", 50, "promotional").senken("abrufe", 3, "promotional")
    assert s.grenzen["suchen"] == 10 and s.begrenzt_durch["suchen"] == "depth"
    assert s.grenzen["abrufe"] == 3 and s.begrenzt_durch["abrufe"] == "promotional"
    assert v.grenzen["suchen"] == rv.obergrenzen()["suchen"]  # immutable view


def test_snapshot_fuer_job_und_selbstaufruf():
    snap = rv.snapshot_fuer_job({"query": "q", "max_turns": 7, "vertrag": {"gesamtzeit_s": 500}})
    assert snap["version"] == rv.version() and snap["iterationen"] == 7 and snap["gesamtzeit_s"] == 500.0
    with pytest.raises(ValueError):
        rv.snapshot_fuer_job({"vertrag": "nein"})
    with pytest.raises(ValueError):
        rv.snapshot_fuer_job({"vertrag": {"foo": 1}})
    body = rv.selbstaufruf_rumpf({"query": "q", "vertrag": {"gesamtzeit_s": 500}}, {rv.JOB_VERTRAG_KEY: snap})
    assert body["vertrag"] == snap and body["query"] == "q"
    assert "vertrag" not in rv.selbstaufruf_rumpf({"query": "q"}, {"app_id": "x"})
    assert rv.selbstaufruf_rumpf({"query": "q"}, None) == {"query": "q"}
    # the snapshot reproduces itself exactly under the same server policy
    assert rv.vertrag_erstellen(_body(vertrag=snap)).als_wunsch() == snap


def test_beschreibung_nennt_version_grenzen_und_capabilities():
    b = rv.beschreibung()
    assert b["version"] == rv.version() and b["grenzen"] == rv.obergrenzen()
    assert b["capabilities"]["pfade"]["pool"]["nicht_durchsetzbar"] == rv.NICHT_DURCHSETZBAR_JE_PFAD["pool"]
    assert b["capabilities"]["fortsetzungen_je_runde"] == rv.FORTSETZUNGEN_JE_RUNDE
    assert set(b["capabilities"]["geltung"]) == set(rv.NAMEN)
    json.dumps(b)


def test_research_request_traegt_den_wunsch_und_max_tokens_bleibt_unwirksam():
    r = ResearchRequest(query="q", vertrag={"gesamtzeit_s": 60})
    assert r.vertrag.gesamtzeit_s == 60 and r.max_tokens == 4000
    v = rv.vertrag_erstellen(r)
    assert v.grenzen["ausgabe_tokens"] == rv.obergrenzen()["ausgabe_tokens"]  # not 4000
    with pytest.raises(ValueError):
        ResearchRequest(query="q", vertrag={"max_tokens": 1})
    assert "nicht wirksam" in ResearchRequest.model_fields["max_tokens"].description.lower().replace("not effective", "nicht wirksam")


# ── 2. the deadline helpers ───────────────────────────────────────────────────

def test_fuer_anfrage_hoechstens_senkt_nur():
    request = MagicMock()
    request.headers = {}
    request.state = SimpleNamespace()
    assert rl.Laufzeit.fuer_anfrage(request, hoechstens_s=100).frist_s <= 100
    request.state = SimpleNamespace()
    assert rl.Laufzeit.fuer_anfrage(request, hoechstens_s=99999).frist_s <= rl.GESAMTFRIST_S
    request.headers = {rl.FRIST_HEADER: repr(time.time() + 50.0)}
    request.state = SimpleNamespace()
    assert rl.Laufzeit.fuer_anfrage(request, hoechstens_s=100).frist_s <= 50


@pytest.mark.asyncio
async def test_unter_restfrist_bricht_ab_und_behauptet_kein_ende():
    async def langsam():
        await asyncio.sleep(5)
        return "nie"

    with pytest.raises(rl.RestfristErschoepft, match="may still be running"):
        await rl.unter_restfrist(langsam(), lambda: 0.01, "perplexity_search")
    with pytest.raises(rl.RestfristErschoepft, match="not started"):
        await rl.unter_restfrist(langsam(), lambda: 0.0, "fetch_document")

    async def schnell():
        return "ok"

    assert await rl.unter_restfrist(schnell(), lambda: 10.0, "x") == "ok"
    assert await rl.unter_restfrist(schnell(), None, "x") == "ok"
    assert issubclass(rl.RestfristErschoepft, rl.LaufzeitErschoepft)


# ── 3. the cloud executor under the contract ──────────────────────────────────

def _pause(container="c1"):
    return _response(200, {
        "model": "claude-sonnet-5", "stop_reason": "pause_turn", "container": {"id": container},
        "usage": _usage(), "content": [{"type": "text", "text": "still going"}],
    })


def _done(text="final report"):
    return _response(200, {
        "model": "claude-sonnet-5", "stop_reason": "end_turn", "usage": _usage(),
        "content": [{"type": "text", "text": text}],
    })


class _Uhr:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.mark.asyncio
async def test_jeder_messages_aufruf_wartet_hoechstens_transport_und_restfrist():
    uhr = _Uhr()
    laufzeit = rl.Laufzeit(500.0, uhr=uhr)
    client = MagicMock()
    gesehen = []

    async def post(url, headers=None, json=None, timeout=None):
        gesehen.append(timeout)
        uhr.t += 200.0  # each call takes 200 s
        return _pause() if len(gesehen) < 3 else _done()

    client.post = post
    result = await run_research_cloud(
        "q", "s", config=ResearchCloudConfig(http_timeout_seconds=900.0), api_key="sk-test",
        client=client, laufzeit=laufzeit,
    )
    assert result.status == "success"
    assert gesehen == [500.0, 300.0, 100.0]  # min(900, remaining) every time, one deadline
    assert laufzeit.runden["bericht"] == 600.0


@pytest.mark.asyncio
async def test_frist_erschoepft_vor_fortsetzung_ist_laufzeit_erschoepft_ohne_teilbericht():
    uhr = _Uhr()
    laufzeit = rl.Laufzeit(100.0, uhr=uhr)
    client = MagicMock()

    async def post(url, headers=None, json=None, timeout=None):
        uhr.t += 150.0
        return _pause()

    client.post = post
    with pytest.raises(ResearchCloudLaufzeitErschoepft) as ei:
        await run_research_cloud("q", "s", api_key="sk-test", client=client, laufzeit=laufzeit)
    assert ei.value.fehler_code == rl.LAUFZEIT_ERSCHOEPFT and ei.value.retryable is False
    assert isinstance(ei.value, ResearchCloudExecutorError)


@pytest.mark.asyncio
async def test_transport_timeout_nennt_die_grenze_und_behauptet_kein_ende():
    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ResearchCloudExecutorError, match="transportzeit_s=900 s") as ei:
        await run_research_cloud("q", "s", api_key="sk-test", client=client)
    assert "may still be running" in str(ei.value) and not isinstance(ei.value, ResearchCloudLaufzeitErschoepft)
    # the same timeout at a shortened call (deadline below transport) is the deadline
    with pytest.raises(ResearchCloudLaufzeitErschoepft, match="overall deadline"):
        await run_research_cloud(
            "q", "s", api_key="sk-test", client=client, laufzeit=rl.Laufzeit(30.0, uhr=_Uhr())
        )


@pytest.mark.asyncio
async def test_fortsetzungen_gelten_fuer_den_ganzen_lauf_und_je_runde():
    client = MagicMock()
    client.post = AsyncMock(return_value=_pause())
    with pytest.raises(ResearchCloudExecutorError, match="max_continuations=3"):
        await run_research_cloud(
            "q", "s", config=ResearchCloudConfig(max_continuations=3, max_continuations_je_runde=8),
            api_key="sk-test", client=client,
        )
    assert client.post.await_count == 3
    client.post.reset_mock()
    with pytest.raises(ResearchCloudExecutorError, match="je Runde 2"):
        await run_research_cloud(
            "q", "s", config=ResearchCloudConfig(max_continuations=16, max_continuations_je_runde=2),
            api_key="sk-test", client=client,
        )
    assert client.post.await_count == 2


async def _lauf_mit_nachkontrolle(antworten, nachkontrolle, laufzeit, **config_kw):
    client = MagicMock()
    client.post = AsyncMock(side_effect=antworten)
    result = await run_research_cloud(
        "frage", "system", api_key="sk-test", client=client, config=ResearchCloudConfig(**config_kw),
        library_config=LibraryConfig(), library_index={}, perplexity_config=_PPLX_ON,
        anonymize=AsyncMock(return_value="anonym"), perplexity_client=_pplx_ok(),
        nachkontrolle=nachkontrolle, laufzeit=laufzeit,
    )
    return result, client


@pytest.mark.asyncio
async def test_rueckrunde_bekommt_keine_neue_fortsetzungsreserve():
    """Run-wide budget 2: the report round used both calls, the return round
    has none left — the report from before stays, the reason is named, and
    the budget is NOT reset per round."""
    nk = Nachkontrolle(pruefer=_pruefer([LUECKE_NORM], [LUECKE_NORM]))
    result, client = await _lauf_mit_nachkontrolle(
        [_pause(), _end(ALT, inp=100, out=50), _tool_use("perplexity_search", {"frage": "x"}), _end(NEU)],
        nk, None, max_continuations=2, max_continuations_je_runde=8,
    )
    assert result.content == ALT and client.post.await_count == 2
    assert "max_continuations=2" in nk.rueckrunde_fehler


@pytest.mark.asyncio
async def test_pruefung_und_rueckrunde_nur_mit_restzeit():
    uhr = _Uhr()
    laufzeit = rl.Laufzeit(rl.PRUEFER_MIN_REST_S + 10.0, uhr=uhr)
    nk = Nachkontrolle(pruefer=_pruefer([LUECKE_NORM], [LUECKE_NORM]))

    client = MagicMock()

    async def post(url, headers=None, json=None, timeout=None):
        uhr.t += 20.0
        return _end(ALT)

    client.post = post
    result = await run_research_cloud(
        "frage", "system", api_key="sk-test", client=client, library_config=LibraryConfig(),
        library_index={}, perplexity_config=_PPLX_ON, anonymize=AsyncMock(return_value="anonym"),
        perplexity_client=_pplx_ok(), nachkontrolle=nk, laufzeit=laufzeit,
    )
    assert result.content == ALT
    assert nk.uebersprungen["pruefung"].startswith("laufzeit:")
    assert nk.status()["status"] == "uebersprungen"
    # enough for the check, not for the return round: the gap is reported, no round
    uhr2 = _Uhr()
    lz2 = rl.Laufzeit(rl.PRUEFER_MIN_REST_S + rl.RUECKRUNDE_MIN_REST_S + 30.0, uhr=uhr2)
    nk2 = Nachkontrolle(pruefer=_pruefer([LUECKE_NORM], [LUECKE_NORM]))

    async def post2(url, headers=None, json=None, timeout=None):
        uhr2.t += 40.0
        return _end(ALT)

    client2 = MagicMock()
    client2.post = post2
    result2 = await run_research_cloud(
        "frage", "system", api_key="sk-test", client=client2, library_config=LibraryConfig(),
        library_index={}, perplexity_config=_PPLX_ON, anonymize=AsyncMock(return_value="anonym"),
        perplexity_client=_pplx_ok(), nachkontrolle=nk2, laufzeit=lz2,
    )
    assert result2.content == ALT
    assert nk2.uebersprungen["rueckrunde"].startswith("laufzeit:") and nk2.luecken_gemeldet == 1


@pytest.mark.asyncio
async def test_werkzeugaufruf_wird_an_der_restfrist_abgebrochen():
    uhr = _Uhr()
    laufzeit = rl.Laufzeit(50.0, uhr=uhr)
    nk = None
    pplx = MagicMock()

    async def haengt(*a, **kw):
        await asyncio.sleep(5)

    pplx.post = haengt
    with patch("src.research_cloud.executor.unter_restfrist") as ur:
        async def _ur(arbeit, rest, was):
            arbeit.close()
            raise rl.RestfristErschoepft(f"{was} abandoned at the run's overall deadline — the remote call may still be running")
        ur.side_effect = _ur
        client = MagicMock()
        client.post = AsyncMock(side_effect=[_tool_use("perplexity_search", {"frage": "x"}), _end(ALT)])
        with pytest.raises(ResearchCloudLaufzeitErschoepft, match="perplexity_search abandoned"):
            await run_research_cloud(
                "frage", "system", api_key="sk-test", client=client, library_config=LibraryConfig(),
                library_index={}, perplexity_config=_PPLX_ON, anonymize=AsyncMock(return_value="anonym"),
                perplexity_client=pplx, nachkontrolle=nk, laufzeit=laufzeit,
            )


# ── 4. the cloud wrapper wires the contract ───────────────────────────────────

def _cloud_patches(executor_mock):
    return (
        patch("src.research_cloud.anonymize_gate.anonymize_query_for_cloud", new=AsyncMock(return_value="ANON_ q")),
        patch("src.research_cloud.executor.run_research_cloud", executor_mock),
        patch("src.activity.ai_call_writer.persist_ai_call_activity", new=AsyncMock()),
        patch("src.scholarly.scholarly_enabled", return_value=False),
    )


@pytest.mark.asyncio
async def test_cloud_config_kommt_aus_dem_vertrag_nicht_aus_max_tokens():
    req = _cloud_req(max_tokens=4000, max_turns=30, depth="standard")
    req.vertrag = rv.ResearchVertragWunsch(ausgabe_tokens=5000, gesamtzeit_s=1000, perplexity_suchen=2)
    request = MagicMock()
    request.state = SimpleNamespace()
    executor_mock = AsyncMock(return_value=_cloud_result())
    p = _cloud_patches(executor_mock)
    with p[0], p[1], p[2], p[3]:
        result = await src.main._execute_research_cloud_impl(request, req, attribution_ctx={})
    kwargs = executor_mock.await_args.kwargs
    cfg = kwargs["config"]
    assert cfg.max_tokens == 5000 and cfg.max_continuations == 16 and cfg.max_continuations_je_runde == 8
    assert cfg.http_timeout_seconds == 900.0 and cfg.perplexity_max_uses == 2 and cfg.fetch_document_max_uses == 10
    assert cfg.web_search_max_uses == 10 and cfg.web_fetch_max_uses == 6  # depth standard
    assert kwargs["laufzeit"].frist_s <= 1000.0
    v = request.state.research_vertrag
    assert v.pfad == "cloud" and v.grenzen["suchen"] == 10 and v.begrenzt_durch["suchen"] == "depth"
    assert v.grenzen["ausgabe_tokens"] == 5000 and v.begrenzt_durch["ausgabe_tokens"] == "client"
    assert result.status == "success" and result.laufzeit["frist_s"] <= 1000.0


@pytest.mark.asyncio
async def test_cloud_promotional_cap_ist_im_vertrag_sichtbar():
    req = _cloud_req(depth="deep")
    req.vertrag = None
    request = MagicMock()
    request.state = SimpleNamespace()
    executor_mock = AsyncMock(return_value=_cloud_result())
    p = _cloud_patches(executor_mock)
    with p[0], p[1], p[2], p[3], \
         patch("src.providers.funding_policy.promotional_only", return_value=True), \
         patch("src.providers.funding_policy.require_pool", return_value=None), \
         patch("src.providers.credit_pool.start_booking_log", return_value=[]):
        await src.main._execute_research_cloud_impl(request, req, attribution_ctx={})
    cfg = executor_mock.await_args.kwargs["config"]
    v = request.state.research_vertrag
    assert cfg.web_search_max_uses == 3 == v.grenzen["suchen"] and v.begrenzt_durch["suchen"] == "promotional"
    assert cfg.web_fetch_max_uses == 3 == v.grenzen["abrufe"]


@pytest.mark.asyncio
async def test_cloud_lehnt_nicht_durchsetzbaren_wunsch_ab_ohne_lauf():
    req = _cloud_req()
    req.vertrag = rv.ResearchVertragWunsch(iterationen=3)
    request = MagicMock()
    request.state = SimpleNamespace()
    executor_mock = AsyncMock(return_value=_cloud_result())
    p = _cloud_patches(executor_mock)
    with p[0], p[1], p[2], p[3]:
        result = await src.main._execute_research_cloud_impl(request, req, attribution_ctx={})
    assert result.status == "error" and result.fehler_code == rv.FEHLER_NICHT_DURCHSETZBAR
    executor_mock.assert_not_awaited()


# ── 5. the pool path wires the contract ───────────────────────────────────────

@pytest.mark.asyncio
async def test_pool_turns_frist_und_werkzeugdeckel_aus_dem_vertrag(spy_cli, persist, pplx_on, monkeypatch):  # noqa: F811
    _, captured, _ = spy_cli
    # The SDK stub (sys.modules MagicMock) loses these two attributes once an
    # earlier module's monkeypatch undo deleted them — the pre-existing cause
    # of the red pool tests in test_research_pool_perplexity; pinned here.
    sdk = sys.modules["claude_code_sdk"]
    monkeypatch.setattr(sdk, "create_sdk_mcp_server", MagicMock(return_value={"type": "sdk"}), raising=False)
    monkeypatch.setattr(sdk, "tool", MagicMock(return_value=lambda fn: fn), raising=False)
    req = _pool_req()  # max_turns=10
    req.vertrag = rv.ResearchVertragWunsch(iterationen=4, perplexity_suchen=2, dokument_abrufe=3, gesamtzeit_s=700)
    req.luecken_nachkontrolle = False  # the spy must see the MAIN run's CLI call, not the checker's
    request = MagicMock()
    request.state = SimpleNamespace()
    werkzeuge = {}

    import src.research_pool_perplexity as rpp
    _P, _D = rpp.PoolPerplexityTool, rpp.PoolDocumentTool

    class P(_P):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            werkzeuge["pplx"] = self

    class D(_D):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            werkzeuge["doc"] = self

    with patch.object(rpp, "PoolPerplexityTool", P), patch.object(rpp, "PoolDocumentTool", D):
        result = await src.main._execute_research_impl(req, None, request=request)
    assert result.status == "success"
    assert captured["max_turns"] == 4
    assert captured["timeout_s"] <= 700.0
    assert werkzeuge["pplx"].max_uses == 2 and werkzeuge["doc"].counters.max_uses == 3
    assert callable(werkzeuge["pplx"].frist) and callable(werkzeuge["doc"].frist)
    assert result.laufzeit["frist_s"] <= 700.0
    v = request.state.research_vertrag
    assert v.pfad == "pool" and v.nicht_durchsetzbar == rv.NICHT_DURCHSETZBAR_JE_PFAD["pool"]


@pytest.mark.asyncio
async def test_pool_legacy_max_turns_wirkt_wie_bisher(spy_cli, persist):  # noqa: F811
    _, captured, _ = spy_cli
    result = await src.main._execute_research_impl(_pool_req(), None, request=MagicMock())
    assert result.status == "success" and captured["max_turns"] == 10
    assert captured["timeout_s"] <= rl.GESAMTFRIST_S


@pytest.mark.asyncio
async def test_pool_werkzeug_bricht_an_der_restfrist_ab():
    import src.research_pool_perplexity as rpp
    from src.research_cloud.perplexity import PerplexityConfig

    tool = rpp.PoolPerplexityTool(
        PerplexityConfig(enabled=True, api_key="k", max_retries=0), AsyncMock(return_value="anon"),
        MagicMock(), 5, frist=lambda: 0.01,
    )
    async def _haengt(*a, **k):
        await asyncio.sleep(5)

    with patch("src.research_pool_perplexity.ask_perplexity", new=_haengt):
        with pytest.raises(rpp.PoolToolError, match="may still be running"):
            await tool.handle({"frage": "x"})
    doc = rpp.PoolDocumentTool(3, frist=lambda: 0.0)
    with pytest.raises(rpp.PoolToolError, match="not started"):
        await doc.handle({"url": "https://x"})


# ── 6. admission in the handler ───────────────────────────────────────────────

def _frequest():
    from fastapi import Request as FRequest
    return FRequest({
        "type": "http", "method": "POST", "path": "/v1/research",
        "headers": [(b"x-user-id", b"user-1"), (b"x-app-id", b"werking-report"), (b"authorization", b"Bearer k")],
        "query_string": b"",
    })


def _handler_patches(impl, admit, budget):
    return (
        patch("src.main.verify_api_key", AsyncMock()),
        patch("src.main.enforce_attribution", MagicMock()),
        patch("src.main.resolve_model", return_value=("claude-sonnet-5", "ok")),
        patch("src.pricing.is_priced", return_value=True),
        patch("src.routing.user_provider_override.enforce_user_provider_override", AsyncMock(return_value=None)),
        patch("src.routing.app_provider_policy.resolve_app_provider_policy", return_value=(None, None)),
        patch("src.research_cloud.routing.resolve_research_cloud_routing", AsyncMock(return_value=False)),
        patch("src.main.resolve_backend_config", return_value=MagicMock(backend=src.main.BackendType.ANTHROPIC, env_vars=None)),
        patch("src.routing.user_provider_override.assert_bedrock_is_pinned", MagicMock()),
        patch("src.routing.user_provider_override.assert_bedrock_attribution_complete", MagicMock()),
        patch("src.main._admit_research_to_pool", admit),
        patch("src.budget.gate.enforce_budget", budget),
        patch("src.main._execute_research_impl", impl),
    )


async def _call(body: ResearchRequest):
    impl = AsyncMock(return_value=ResearchResponse(status="success", query=body.query, model="claude-sonnet-5", content="r"))
    admit = AsyncMock(return_value=None)
    budget = AsyncMock()
    ps = _handler_patches(impl, admit, budget)
    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in ps:
            stack.enter_context(p)
        out = await src.main.research(body, _frequest(), credentials=MagicMock())
    return out, impl, admit, budget


@pytest.mark.asyncio
async def test_handler_lehnt_nicht_durchsetzbaren_wunsch_vor_pool_admission_ab():
    out, impl, admit, budget = await _call(ResearchRequest(query="q", vertrag={"ausgabe_tokens": 100}))
    assert out.status_code == 422
    body = json.loads(bytes(out.body))
    assert body["status"] == "error" and body["fehler_code"] == rv.FEHLER_NICHT_DURCHSETZBAR
    assert body["retryable"] is False and body["vertrag"]["version"] == rv.version()
    admit.assert_not_awaited(); budget.assert_not_awaited(); impl.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_lehnt_fremde_version_vor_allem_ab():
    out, impl, admit, budget = await _call(ResearchRequest(query="q", vertrag={"version": "research-vertrag/1+000000000000", "gesamtzeit_s": 5}))
    assert out.status_code == 422
    body = json.loads(bytes(out.body))
    assert body["fehler_code"] == rv.FEHLER_VERSION_ABWEICHEND and body["vertrag"]["version"] == rv.version()
    admit.assert_not_awaited(); budget.assert_not_awaited(); impl.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_legacy_laeuft_und_antwort_traegt_den_vertrag():
    out, impl, admit, budget = await _call(ResearchRequest(query="q", max_turns=12))
    assert isinstance(out, ResearchResponse) and out.status == "success"
    admit.assert_awaited_once(); impl.assert_awaited_once()
    assert out.vertrag["pfad"] == "pool" and out.vertrag["version"] == rv.version()
    assert out.vertrag["grenzen"]["iterationen"] == 12 and out.vertrag["begrenzt_durch"]["iterationen"] == "client"
    assert out.vertrag["nicht_durchsetzbar"] == rv.NICHT_DURCHSETZBAR_JE_PFAD["pool"]


@pytest.mark.asyncio
async def test_handler_erlaubter_wunsch_laeuft_auf_dem_pool():
    out, impl, admit, budget = await _call(ResearchRequest(query="q", vertrag={"gesamtzeit_s": 300, "perplexity_suchen": 1}))
    assert isinstance(out, ResearchResponse) and out.status == "success"
    assert out.vertrag["grenzen"]["gesamtzeit_s"] == 300.0 and out.vertrag["begrenzt_durch"]["gesamtzeit_s"] == "client"


@pytest.mark.asyncio
async def test_vertrag_ist_vor_admission_lesbar():
    with patch("src.main.verify_api_key", AsyncMock()):
        b = await src.main.research_vertrag_lesen(_frequest(), credentials=MagicMock())
    assert b["version"] == rv.version() and "capabilities" in b


# ── 7. durable jobs: snapshot, idempotency, self-call ─────────────────────────

@pytest.fixture
def research_job(mem, monkeypatch):  # noqa: F811
    from src.jobs import registry, routes, store_client
    monkeypatch.setitem(registry._EXECUTORS, "research", AsyncMock())
    gesehen = {"create": [], "dispatch": []}
    orig_idem, orig_plain = store_client.create_job_idempotent, store_client.create_job

    async def create_idem(job_id, kind, payload, attribution, owner, key):
        gesehen["create"].append((payload, attribution))
        return await orig_idem(job_id, kind, payload, attribution, owner, key)

    async def create_plain(job_id, kind, payload, attribution):
        gesehen["create"].append((payload, attribution))
        return await orig_plain(job_id, kind, payload, attribution)

    monkeypatch.setattr(store_client, "create_job_idempotent", create_idem)
    monkeypatch.setattr(store_client, "create_job", create_plain)

    def fake_run(job_id, kind, payload, attribution):
        gesehen["dispatch"].append((payload, attribution))

        async def _c():
            pass
        return _c()

    monkeypatch.setattr(routes, "run_generic_job", fake_run)
    return gesehen


RBODY = {"kind": "research", "payload": {"query": "q", "max_turns": 9}}


async def test_job_snapshot_liegt_in_der_attribution_nicht_im_rumpf(research_job):
    r = await _post(body=RBODY, key=KEY)
    assert r.status_code == 201
    payload, attribution = research_job["create"][0]
    assert payload == RBODY["payload"]  # the client's body, untouched
    snap = attribution[rv.JOB_VERTRAG_KEY]
    assert snap["version"] == rv.version() and snap["iterationen"] == 9
    assert research_job["dispatch"][0][1][rv.JOB_VERTRAG_KEY] == snap


async def test_job_replay_findet_den_job_auch_nach_neuer_serverpolicy(research_job, monkeypatch):
    r1 = await _post(body=RBODY, key=KEY)
    v_alt = research_job["create"][0][1][rv.JOB_VERTRAG_KEY]["version"]
    monkeypatch.setenv("RESEARCH_VERTRAG_SUCHEN_MAX", "7")  # new policy, new version
    assert rv.version() != v_alt
    r2 = await _post(body=RBODY, key=KEY)
    assert r2.status_code == 200 and r2.headers["Idempotent-Replayed"] == "true"
    assert r2.json()["job_id"] == r1.json()["job_id"]
    assert len(research_job["create"]) == 1  # no second row, no second snapshot
    # a NEW job binds the contract in force now
    r3 = await _post(body=RBODY, key=KEY + ":2")
    assert r3.status_code == 201
    assert research_job["create"][1][1][rv.JOB_VERTRAG_KEY]["version"] == rv.version() != v_alt


async def test_job_mit_rumpf_snapshot_wird_ersetzt_und_fremde_version_abgelehnt(research_job):
    body = {"kind": "research", "payload": {"query": "q"}, "attribution": {rv.JOB_VERTRAG_KEY: {"version": "fake"}}}
    r = await _post(body=body, key=KEY)
    assert r.status_code == 201
    assert research_job["create"][0][1][rv.JOB_VERTRAG_KEY]["version"] == rv.version()
    bad = {"kind": "research", "payload": {"query": "q", "vertrag": {"version": "research-vertrag/1+ffffffffffff", "gesamtzeit_s": 5}}}
    r = await _post(body=bad, key=KEY + ":b")
    assert r.status_code == 422 and r.json()["detail"]["reason"] == rv.FEHLER_VERSION_ABWEICHEND
    r = await _post(body={"kind": "research", "payload": {"query": "q", "vertrag": {"nope": 1}}})
    assert r.status_code == 400 and r.json()["detail"]["reason"] == rv.FEHLER_VERTRAG_UNGUELTIG
    assert len(research_job["create"]) == 1


async def test_job_ohne_schluessel_bekommt_den_snapshot_auch(research_job):
    r = await _post(body=RBODY)
    assert r.status_code == 200
    assert research_job["create"][0][1][rv.JOB_VERTRAG_KEY]["version"] == rv.version()


async def test_selbstaufruf_sendet_den_snapshot_als_wunsch():
    from src.jobs.executors import research_executor
    snap = rv.snapshot_fuer_job({"query": "q", "vertrag": {"gesamtzeit_s": 400}})
    resp = _FakeResp(200, {"status": "success", "content": "# R", "query": "q", "model": "m"})
    fake = _FakeClient(resp)
    with patch("httpx.AsyncClient", return_value=fake), patch("src.auth.auth_manager.get_api_key", return_value="k"):
        await research_executor({"query": "q", "vertrag": {"gesamtzeit_s": 400}}, {"app_id": "a", rv.JOB_VERTRAG_KEY: snap}, AsyncMock())
        fake2 = _FakeClient(resp)
        with patch("httpx.AsyncClient", return_value=fake2):
            await research_executor({"query": "q"}, {"app_id": "a"}, AsyncMock())
    assert fake.calls[0]["json"]["vertrag"] == snap and fake.calls[0]["json"]["async_mode"] is False
    assert "vertrag" not in fake2.calls[0]["json"]  # legacy job: no wish


# ── 8. deploy smoke: the contract route is probed, read-only ──────────────────

def test_smoke_probe_prueft_die_form_des_vertrags(monkeypatch):
    from tests.unit.test_smoke_jobs_retention import _Resp, coverage, smoke

    assert "/v1/research/vertrag" in coverage.discover_routes()
    assert "/v1/research/vertrag" in coverage.covered_endpoints()
    p = next(p for p in smoke.PROBES if p.name == "research_vertrag")
    assert p.endpoint == "/v1/research/vertrag" and {"hetzner", "server2"} <= p.profiles
    assert "X-User-ID" in p.repro

    seen = {}

    def fake_get(url, headers, timeout):
        seen["url"] = url
        return _Resp(200, seen["body"])

    monkeypatch.setattr(smoke.requests, "get", fake_get)
    ctx = smoke.Ctx(base_url="http://bridge.invalid", api_key="k")
    seen["body"] = rv.beschreibung()
    assert p.fn(ctx).ok and seen["url"].endswith("/v1/research/vertrag")
    seen["body"] = {**rv.beschreibung(), "version": "research-vertrag/0+x"}
    assert not p.fn(ctx).ok
    seen["body"] = {**rv.beschreibung(), "grenzen": {"a": 1}}
    assert not p.fn(ctx).ok
