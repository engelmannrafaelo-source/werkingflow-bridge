"""Energy-Abo-Modus: Recherche ohne Perplexity (KD-Entscheid N4, 03.11.2026).

Rafael: kein bezahlter Fremdschlüssel in Staging. Im Abo-Modus (Header und
Schalter + Allowlist) läuft /v1/research über die CLI auf dem Abo (Claude-
Websuche) — Perplexity wird nie angeboten und nie aufgerufen, auch wenn
RESEARCH_PERPLEXITY_ENABLED und ein Schlüssel gesetzt sind; der Credit-Pool
und der Cloud-Weg werden nicht berührt. Die Recherche liefert trotzdem ein
Ergebnis. Ohne Netz: die CLI ist ein Fake, Perplexity/Pool/Cloud sind Fallen.
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.unit.test_energy_abo_bildweg import abo, dev_bridge  # noqa: F401
from tests.unit.test_energy_abo_modus import ENERGY, USER_A, _req, pool_trap  # noqa: F401
import src.main as main  # noqa: E402
from src.models import ResearchRequest  # noqa: E402
from src.providers import funding_policy as fp  # noqa: E402

ANTWORT = "Ergebnis der Recherche: X-100 hat 42 kW (Quelle: hersteller.example)."


class FakeResearchCli:
    def __init__(self):
        self.calls = []

    async def run_completion(self, **kw):
        self.calls.append(kw)
        yield {"subtype": "init", "data": {"tools": ["WebSearch", "WebFetch", "Read"]}}
        yield {"type": "assistant", "message_id": "m1", "stop_reason": "tool_use",
               "content": [{"type": "tool_use", "name": "WebSearch", "input": {"query": "X-100 Leistung"}}]}
        yield {"type": "assistant", "message_id": "m2", "stop_reason": "end_turn",
               "content": [{"type": "text", "text": ANTWORT}]}
        yield {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
               "usage": {"input_tokens": 900, "output_tokens": 60,
                         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}


@pytest.fixture
def perplexity_on_trap(monkeypatch):
    """Perplexity eingeschaltet und mit Schlüssel — jeder Zugriff schlägt an."""
    monkeypatch.setenv("RESEARCH_PERPLEXITY_ENABLED", "true")
    monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-platzhalter-nie-benutzen")
    monkeypatch.setenv("BRIDGE_ANONYMIZE_ENABLED", "true")
    trap = MagicMock(side_effect=AssertionError("Perplexity offered/called"))
    atrap = AsyncMock(side_effect=AssertionError("Perplexity called"))
    import src.research_cloud.perplexity as rp
    import src.research_pool_perplexity as rpp
    monkeypatch.setattr(rp, "ask_perplexity", atrap)
    monkeypatch.setattr(rpp, "PoolPerplexityTool", trap)
    monkeypatch.setattr(rpp, "PoolDocumentTool", trap)
    return trap, atrap


@pytest.mark.parametrize("variante", ["header", "switch"])
async def test_abo_research_never_offers_perplexity_and_answers(
        dev_bridge, abo, pool_trap, perplexity_on_trap, monkeypatch, tmp_path, variante):
    trap, atrap = perplexity_on_trap
    headers = dict(ENERGY)
    if variante == "header":
        headers["X-Funding-Source"] = "subscription"
    else:
        monkeypatch.setenv(fp.SUBSCRIPTION_ONLY_ENV, "1")
        monkeypatch.setenv(fp.SUBSCRIPTION_ONLY_USERS_ENV, USER_A)
    req = _req(headers)
    cli = FakeResearchCli()
    cloud = AsyncMock(side_effect=AssertionError("research-cloud path used"))
    body = ResearchRequest(query="Leistung von X-100?", model="claude-sonnet-5-5", depth="quick",
                           antwort_im_text=True)
    with ExitStack() as stack:
        stack.enter_context(patch.object(main, "verify_api_key", AsyncMock(return_value=None)))
        stack.enter_context(patch("src.routing.user_provider_override.enforce_user_provider_override",
                                  AsyncMock(return_value=None)))
        stack.enter_context(patch.object(main, "_admit_research_to_pool", AsyncMock(return_value=None)))
        stack.enter_context(patch.object(main, "_execute_research_cloud_with_pool_fallback", cloud))
        stack.enter_context(patch.object(main.claude_cli, "run_completion", cli.run_completion))
        stack.enter_context(patch("src.activity.ai_call_writer.persist_ai_call_activity", AsyncMock()))
        out = await main.research(body, req, None)
    # (2) die Recherche liefert über das Abo ein Ergebnis
    assert out.status == "success", out.error
    assert ANTWORT in (out.content or "")
    assert out.funding_policy == "subscription_staging_fallback" and fp.pool_umgangen(req)
    # (1) Perplexity nirgends angeboten/aufgerufen, Pool und Cloud nie berührt
    (kw,) = cli.calls
    assert not kw.get("sdk_mcp_servers"), kw.get("sdk_mcp_servers")
    assert "perplexity" not in (kw.get("append_system_prompt") or "").lower()
    assert "perplexity" not in (kw.get("system_prompt") or "").lower()
    assert "WebSearch" not in (kw.get("disallowed_tools") or [])  # Claude-Websuche bleibt
    trap.assert_not_called()
    atrap.assert_not_called()
    cloud.assert_not_called()
    pool_trap.assert_not_called()


def test_cloud_path_also_drops_perplexity_for_energy():
    """Der Cloud-Weg (nur ohne Abo-Modus, Pool-Recherche) schaltet Perplexity
    für werking-energy ebenfalls ab — Quelltext-Beleg der Stelle."""
    import inspect
    src = inspect.getsource(main._execute_research_cloud_impl)
    assert 'if free_only:\n        perplexity_cfg = perplexity_cfg.model_copy(update={"enabled": False, "api_key": None})' in src
