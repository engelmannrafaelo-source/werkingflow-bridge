"""Caller-owned literature layer (BRUMBAU step 3, BRAUS F19).

The Bridge plans, fetches and formats open-access literature; WHAT to search
for comes from the caller (literatur_planer), what the material is for from
the caller's research_instructions. The legacy Energy planner and header stay
frozen in src/compat/scholarly.py for unmigrated callers only.
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

import src.main
from src import literatur
from src.models import ResearchRequest
from tests.research_cloud.test_execute_research_cloud_impl import _cloud_result, _make_req

PLANER = "Plan search terms for a synthetic question. Answer a JSON array."


# --- request contract ---------------------------------------------------------

def test_planner_needs_the_callers_own_policy():
    with pytest.raises(ValidationError, match="literatur_planer requires research_instructions"):
        ResearchRequest(query="q", literatur_planer=PLANER)
    ok = ResearchRequest(query="q", research_instructions="App rules", literatur_planer=PLANER)
    assert ok.literatur_planer == PLANER


@pytest.mark.parametrize("value", ["", "   ", 5])
def test_planner_must_be_nonblank_text(value):
    with pytest.raises(ValidationError):
        ResearchRequest(query="q", research_instructions="App rules", literatur_planer=value)


def test_agentic_run_rejects_the_planner():
    from tests.test_agentischer_lauf_cxa2 import request_payload
    with pytest.raises(ValidationError, match="auftrag_ungueltig"):
        ResearchRequest(**request_payload(), literatur_planer=PLANER)


def test_legacy_wire_shape_has_no_literatur_field():
    from src.models import ResearchResponse
    assert "literatur" not in ResearchResponse(status="success", query="q", model="m").model_dump()


# --- the neutral mechanism ----------------------------------------------------

DOMAIN_WORDS = ("TRVB", "ÖNORM", "OIB", "Energie", "Grenzwert", "Türöffnungskraft",
                "Druckbelüftung", "Brandschutz", "österreich")


def test_mechanism_carries_no_domain_text():
    source = Path(literatur.__file__).read_text()
    for word in DOMAIN_WORDS:
        assert word.lower() not in literatur.KOPF.lower()
        assert word not in source, word


async def _kontext(monkeypatch, *, planned, bloecke=("### A\n\ntext", 1), anonymize=None):
    sent = {}

    async def self_post(path, body, attribution, timeout):
        sent["body"] = body
        return {"choices": [{"message": {"content": json.dumps(planned)}}]}

    monkeypatch.setattr("src.jobs.executors._self_post_json", self_post)
    fetched = MagicMock(return_value=bloecke)
    monkeypatch.setattr(literatur, "quellen_bloecke", fetched)
    anonymize = anonymize or AsyncMock(side_effect=lambda t: f"anon:{t}")
    block, status = await literatur.literatur_kontext("Question", planer=PLANER, anonymize=anonymize)
    return block, status, sent, fetched, anonymize


async def test_planner_text_is_the_callers_and_terms_pass_the_gate(monkeypatch):
    block, status, sent, fetched, anonymize = await _kontext(monkeypatch, planned=["t1", "t2"])
    assert sent["body"]["messages"][0] == {"role": "system", "content": PLANER}
    assert fetched.call_args.args[0] == ["anon:t1", "anon:t2"]
    assert block.startswith(literatur.KOPF) and block.endswith("### A\n\ntext")
    assert status == {"status": "eingefuegt", "quellen": 1, "suchbegriffe": 2}


async def test_refused_anonymization_sends_nothing(monkeypatch):
    block, status, _, fetched, _ = await _kontext(
        monkeypatch, planned=["t1"], anonymize=AsyncMock(side_effect=RuntimeError("gate")))
    assert block == "" and status == {"status": "uebersprungen", "grund": "anonymisierung_gescheitert"}
    fetched.assert_not_called()


async def test_no_sources_is_said(monkeypatch):
    block, status, *_ = await _kontext(monkeypatch, planned=["t1"], bloecke=("", 0))
    assert block == "" and status == {"status": "uebersprungen", "grund": "keine_quellen"}


# --- BRUMBAU3R P2-1: the caller's planner or nothing ------------------------------

@pytest.mark.parametrize("antwort,ursache", [
    (RuntimeError("bridge down"), "aufruf: RuntimeError"),
    ({"choices": []}, "aufruf: IndexError"),
    ({"choices": [{"message": {"content": "no array"}}]}, "antwort_ohne_json_array"),
    ({"choices": [{"message": {"content": "[not json"}}]}, "antwort_ohne_json_array"),
    ({"choices": [{"message": {"content": None}}]}, "antwort_ohne_json_array"),
])
async def test_failed_planner_is_named_and_nothing_is_searched(
        monkeypatch, antwort, ursache):
    async def self_post(path, body, attribution, timeout):
        if isinstance(antwort, Exception):
            raise antwort
        return antwort

    monkeypatch.setattr("src.jobs.executors._self_post_json", self_post)
    fetched = MagicMock(return_value=("### A", 1))
    monkeypatch.setattr(literatur, "quellen_bloecke", fetched)
    anonymize = AsyncMock(side_effect=lambda t: t)
    block, status = await literatur.literatur_kontext("Question " * 40, planer=PLANER,
                                                      anonymize=anonymize)
    assert block == ""
    assert status == {"status": "uebersprungen", "grund": "planer_gescheitert",
                      "ursache": ursache}
    # No substitute search with the raw question.
    fetched.assert_not_called()
    anonymize.assert_not_called()


async def test_empty_plan_is_keine_suchbegriffe(monkeypatch):
    block, status, _, fetched, _ = await _kontext(monkeypatch, planned=[" ", ""])
    assert block == ""
    assert status == {"status": "uebersprungen", "grund": "keine_suchbegriffe"}
    fetched.assert_not_called()


async def test_blank_question_plans_nothing(monkeypatch):
    called = AsyncMock()
    monkeypatch.setattr("src.jobs.executors._self_post_json", called)
    block, status = await literatur.literatur_kontext(
        "  ", planer=PLANER, anonymize=AsyncMock())
    assert status == {"status": "uebersprungen", "grund": "keine_suchbegriffe"}
    called.assert_not_called()


async def test_terms_over_the_limit_are_counted(monkeypatch):
    planned = [f"t{i}" for i in range(literatur.MAX_SUCHBEGRIFFE + 3)]
    block, status, _, fetched, _ = await _kontext(monkeypatch, planned=planned)
    assert len(fetched.call_args.args[0]) == literatur.MAX_SUCHBEGRIFFE
    assert status == {"status": "eingefuegt", "quellen": 1,
                      "suchbegriffe": literatur.MAX_SUCHBEGRIFFE,
                      "suchbegriffe_verworfen": 3}


async def test_legacy_planner_keeps_its_raw_query_fallback(monkeypatch):
    """The frozen legacy path is unchanged: it still falls back."""
    from src.compat import scholarly as legacy

    async def self_post(path, body, attribution, timeout):
        raise RuntimeError("down")

    monkeypatch.setattr("src.jobs.executors._self_post_json", self_post)
    assert await legacy._plan_queries("Question", None, None) == ["Question"]


# --- BRUMBAU3R P3-1: the remaining reasons -------------------------------------------

async def test_time_budget_is_its_own_reason(monkeypatch):
    import asyncio

    async def self_post(path, body, attribution, timeout):
        await asyncio.sleep(5)

    monkeypatch.setattr("src.jobs.executors._self_post_json", self_post)
    block, status = await literatur.literatur_kontext(
        "Question", planer=PLANER, anonymize=AsyncMock(), overall_timeout=0.05)
    assert block == "" and status == {"status": "uebersprungen", "grund": "zeitbudget"}


async def test_unexpected_error_names_its_type(monkeypatch):
    def kaputt(queries, per_query):
        raise KeyError("x")

    async def self_post(path, body, attribution, timeout):
        return {"choices": [{"message": {"content": "[\"t\"]"}}]}

    monkeypatch.setattr("src.jobs.executors._self_post_json", self_post)
    monkeypatch.setattr(literatur, "quellen_bloecke", kaputt)
    block, status = await literatur.literatur_kontext(
        "Question", planer=PLANER, anonymize=AsyncMock(side_effect=lambda t: t))
    assert block == ""
    assert status == {"status": "uebersprungen", "grund": "fehler: KeyError"}


@pytest.mark.parametrize("env,mode,grund", [
    ({"BRIDGE_SCHOLARLY_ENABLED": "false"}, "academic", "schicht_aus"),
    ({"BRIDGE_SCHOLARLY_ENABLED": "true", "BRIDGE_SCHOLARLY_PROFILE": "full"}, "standard",
     "research_mode_nicht_academic"),
    ({"BRIDGE_SCHOLARLY_ENABLED": "true", "BRIDGE_SCHOLARLY_PROFILE": "light"}, "academic", "profil_light"),
    ({"BRIDGE_SCHOLARLY_ENABLED": "true", "BRIDGE_SCHOLARLY_PROFILE": "full"}, "academic", None),
])
def test_why_the_layer_does_not_run(monkeypatch, env, mode, grund):
    monkeypatch.delenv("BRIDGE_SCHOLARLY_PROFILE", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert literatur.nicht_aktiv_grund(mode) == grund


# --- legacy stays frozen ---------------------------------------------------------

async def test_legacy_planner_and_header_unchanged(monkeypatch):
    from src.compat import scholarly as legacy
    sent = {}

    async def self_post(path, body, attribution, timeout):
        sent["body"] = body
        return {"choices": [{"message": {"content": "[\"q\"]"}}]}

    monkeypatch.setattr("src.jobs.executors._self_post_json", self_post)
    assert await legacy._plan_queries("Question", None, None) == ["q"]
    assert sent["body"]["messages"][0]["content"] == legacy._PLANNER_SYS
    monkeypatch.setattr(legacy, "quellen_bloecke", lambda q, n: ("### A", 1))
    # The whole legacy header, pinned: sha256 of develop 6b924c3's header
    # (src/scholarly.py) followed by the block "### A" (BRUMBAU3R P3-1, M14).
    import hashlib
    block = legacy._retrieve_and_format(["q"], 12)
    assert block.startswith("## VERIFIZIERTE OA-LITERATUR") and block.endswith("### A")
    assert hashlib.sha256(block.encode()).hexdigest() == (
        "323db9b25ac8ec91f0253ea2e9f290769e6c84c1154714de8684e8fa0adcc8b9")
    monkeypatch.setattr(legacy, "quellen_bloecke", lambda q, n: ("", 0))
    assert legacy._retrieve_and_format(["q"], 12) == ""


# --- both research paths -----------------------------------------------------------

def _cloud_patches(gate_texts):
    return (
        patch("src.research_cloud.anonymize_gate.anonymize_query_for_cloud",
              new=AsyncMock(side_effect=gate_texts)),
        patch("src.activity.ai_call_writer.persist_ai_call_activity", new=AsyncMock()),
        patch("src.research_cloud.library.load_library_for_run", new=AsyncMock(return_value=None)),
    )


async def test_cloud_path_injects_the_callers_literature(monkeypatch):
    monkeypatch.setenv("BRIDGE_SCHOLARLY_ENABLED", "true")
    monkeypatch.setenv("BRIDGE_SCHOLARLY_PROFILE", "full")
    req = _make_req(research_instructions="App rules", research_mode="academic", literatur_planer=PLANER)
    kontext = AsyncMock(return_value=(literatur.KOPF + "### A", {"status": "eingefuegt", "quellen": 1}))
    a, b, c = _cloud_patches(["safe query", "safe instruction"])
    with a, b, c, \
         patch("src.research_cloud.executor.run_research_cloud",
               new=AsyncMock(return_value=_cloud_result())) as executor, \
         patch("src.literatur.literatur_kontext", kontext), \
         patch("src.scholarly.build_oa_context", new=AsyncMock(return_value="LEGACY")) as legacy:
        result = await src.main._execute_research_cloud_impl(MagicMock(), req, attribution_ctx={})
    legacy.assert_not_awaited()
    assert kontext.await_args.args[0] == "safe query"
    assert kontext.await_args.kwargs["planer"] == PLANER
    assert executor.await_args.args[0] == "safe query\n\n" + literatur.KOPF + "### A"
    assert result.status == "success"
    assert result.literatur == {"status": "eingefuegt", "quellen": 1}


async def test_cloud_path_reports_an_inactive_layer(monkeypatch):
    monkeypatch.setenv("BRIDGE_SCHOLARLY_ENABLED", "false")
    req = _make_req(research_instructions="App rules", research_mode="academic", literatur_planer=PLANER)
    a, b, c = _cloud_patches(["safe query", "safe instruction"])
    with a, b, c, \
         patch("src.research_cloud.executor.run_research_cloud",
               new=AsyncMock(return_value=_cloud_result())), \
         patch("src.literatur.literatur_kontext", new=AsyncMock()) as kontext:
        result = await src.main._execute_research_cloud_impl(MagicMock(), req, attribution_ctx={})
    kontext.assert_not_awaited()
    assert result.literatur == {"status": "uebersprungen", "grund": "schicht_aus"}


async def test_without_planner_nothing_changes(monkeypatch):
    monkeypatch.setenv("BRIDGE_SCHOLARLY_ENABLED", "true")
    monkeypatch.setenv("BRIDGE_SCHOLARLY_PROFILE", "full")
    req = _make_req(research_instructions="App rules", research_mode="academic")
    a, b, c = _cloud_patches(["safe query", "safe instruction"])
    with a, b, c, \
         patch("src.research_cloud.executor.run_research_cloud",
               new=AsyncMock(return_value=_cloud_result())), \
         patch("src.literatur.literatur_kontext", new=AsyncMock()) as kontext:
        result = await src.main._execute_research_cloud_impl(MagicMock(), req, attribution_ctx={})
    kontext.assert_not_awaited()
    assert "literatur" not in result.model_dump()


async def test_layer_terms_go_through_the_request_privacy_gate(monkeypatch):
    """The helper hands literatur_kontext the research privacy gate with the
    caller's own allowlist."""
    monkeypatch.setenv("BRIDGE_SCHOLARLY_ENABLED", "true")
    monkeypatch.setenv("BRIDGE_SCHOLARLY_PROFILE", "full")
    req = _make_req(research_instructions="App rules", research_mode="academic",
                    literatur_planer=PLANER, privacy_allowlist=["WP1"])
    seen = {}

    async def kontext(query, *, planer, anonymize, **kw):
        seen["term"] = await anonymize("term")
        return "", {"status": "uebersprungen", "grund": "keine_quellen"}

    with patch("src.literatur.literatur_kontext", kontext), \
         patch("src.research_cloud.anonymize_gate.anonymize_query_for_cloud",
               new=AsyncMock(return_value="anon")) as gate:
        block, status = await src.main._literatur_schicht(MagicMock(), req, "q", {})
    assert seen["term"] == "anon"
    assert gate.await_args.kwargs == {"privacy_allowlist": ["WP1"]}
    assert status["grund"] == "keine_quellen"


# Pool path: the same helper, its block in the CLI prompt, its status in the answer.
from tests.research_cloud.test_research_pool_perplexity import (  # noqa: E402,F401
    _make_req as _pool_req,
    persist,
    spy_cli,
)


async def test_pool_path_injects_the_callers_literature(spy_cli, persist, monkeypatch):
    monkeypatch.setenv("BRIDGE_SCHOLARLY_ENABLED", "true")
    monkeypatch.setenv("BRIDGE_SCHOLARLY_PROFILE", "full")
    _, captured, _ = spy_cli
    req = _pool_req()
    req.research_instructions = "App rules"
    req.research_mode = "academic"
    req.literatur_planer = PLANER
    status = {"status": "eingefuegt", "quellen": 2, "suchbegriffe": 3}
    with patch("src.literatur.literatur_kontext",
               new=AsyncMock(return_value=(literatur.KOPF + "### Pool", status))) as kontext, \
         patch("src.scholarly.build_oa_context", new=AsyncMock(return_value="LEGACY")) as legacy, \
         patch("src.research_cloud.anonymize_gate.anonymize_query_for_cloud", new=AsyncMock(return_value="x")):
        result = await src.main._execute_research_impl(req, None, request=MagicMock())
    legacy.assert_not_awaited()
    assert kontext.await_args.kwargs["planer"] == PLANER
    assert captured["prompt"].endswith(literatur.KOPF + "### Pool")
    assert result.status == "success", result.error
    assert result.literatur == status


# --- BRUMBAU3R P2-2: the status comes with error answers too ------------------------

LIT_STATUS = {"status": "eingefuegt", "quellen": 1, "suchbegriffe": 1}


def _lit_env(monkeypatch):
    monkeypatch.setenv("BRIDGE_SCHOLARLY_ENABLED", "true")
    monkeypatch.setenv("BRIDGE_SCHOLARLY_PROFILE", "full")
    return patch("src.literatur.literatur_kontext",
                 new=AsyncMock(return_value=(literatur.KOPF + "### A",
                                             dict(LIT_STATUS))))


async def test_cloud_unusable_library_keeps_the_literature_status(monkeypatch):
    from src.research_cloud.library import LibraryUnavailableError

    req = _make_req(research_instructions="App rules", research_mode="academic",
                    literatur_planer=PLANER)
    with _lit_env(monkeypatch), \
         patch("src.research_cloud.anonymize_gate.anonymize_query_for_cloud",
               new=AsyncMock(side_effect=["safe query", "safe instruction"])), \
         patch("src.research_cloud.library.load_library_for_run",
               new=AsyncMock(side_effect=LibraryUnavailableError("kaputt"))), \
         patch("src.research_cloud.executor.run_research_cloud",
               new=AsyncMock()) as executor:
        result = await src.main._execute_research_cloud_impl(
            MagicMock(), req, attribution_ctx={})
    executor.assert_not_awaited()
    assert result.status == "error" and "Bibliothek" in result.error
    assert result.literatur == LIT_STATUS
    assert json.loads(result.model_dump_json())["literatur"] == LIT_STATUS


async def test_cloud_model_stop_keeps_the_literature_status(monkeypatch):
    from src.lauf_abschluss import abschluss_aus_stop_reason
    from src.research_cloud.executor import ResearchCloudModelAbbruch
    from src.research_cloud.models import ResearchCloudUsage

    req = _make_req(research_instructions="App rules", research_mode="academic",
                    literatur_planer=PLANER)
    abbruch = ResearchCloudModelAbbruch(
        "stopped", usage=ResearchCloudUsage(),
        abschluss=abschluss_aus_stop_reason("refusal", category=None))
    a, b, c = _cloud_patches(["safe query", "safe instruction"])
    with a, b, c, _lit_env(monkeypatch), \
         patch("src.research_cloud.executor.run_research_cloud",
               new=AsyncMock(side_effect=abbruch)):
        result = await src.main._execute_research_cloud_impl(
            MagicMock(), req, attribution_ctx={})
    assert result.status == "error"
    assert result.literatur == LIT_STATUS


async def test_pool_unusable_library_keeps_the_literature_status(
        spy_cli, persist, monkeypatch):
    from src.research_cloud.library import LibraryUnavailableError

    spy, _, _ = spy_cli
    req = _pool_req()
    req.research_instructions = "App rules"
    req.research_mode = "academic"
    req.literatur_planer = PLANER
    with _lit_env(monkeypatch), \
         patch("src.research_cloud.anonymize_gate.anonymize_query_for_cloud",
               new=AsyncMock(return_value="x")), \
         patch("src.research_cloud.library.load_library_for_run",
               new=AsyncMock(side_effect=LibraryUnavailableError("index laedt nicht"))):
        result = await src.main._execute_research_impl(req, None, request=MagicMock())
    spy.assert_not_called()
    assert result.status == "error" and "Bibliothek" in result.error
    assert result.literatur == LIT_STATUS


async def test_pool_unusable_perplexity_keeps_the_literature_status(
        spy_cli, persist, monkeypatch):
    spy, _, _ = spy_cli
    monkeypatch.setenv("RESEARCH_PERPLEXITY_ENABLED", "true")
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    req = _pool_req()
    req.research_instructions = "App rules"
    req.research_mode = "academic"
    req.literatur_planer = PLANER
    with _lit_env(monkeypatch), \
         patch("src.research_cloud.anonymize_gate.anonymize_query_for_cloud",
               new=AsyncMock(return_value="x")):
        result = await src.main._execute_research_impl(req, None, request=MagicMock())
    spy.assert_not_called()
    assert result.status == "error" and "PERPLEXITY_API_KEY" in result.error
    assert result.literatur == LIT_STATUS


def test_answer_before_the_layer_has_no_literature_field():
    """Only an answer after the layer carries the field: an empty holder (the
    run ended before the layer, or no literatur_planer) adds nothing."""
    response = src.main.ResearchResponse(status="error", query="q", model="m")
    assert "literatur" not in src.main._mit_literatur(response, {}).model_dump()
    assert "literatur" not in src.main._mit_literatur(response, {"status": None}).model_dump()
