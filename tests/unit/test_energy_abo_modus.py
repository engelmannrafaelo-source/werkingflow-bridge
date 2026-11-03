"""Energy-Abo-Modus (KD 03.11.2026, Energy-Owner): am Credit-Pool VORBEI.

Im Abo-Modus (Header ``X-Funding-Source: subscription`` oder Schalter
``ENERGY_STAGING_SUBSCRIPTION_ONLY`` + Allowlist ``..._USERS``) berührt die
Bridge den Credit-Pool nie — auch wenn er konfiguriert ist: Chat (Text, Bild),
/v1/document/convert, /v1/research. Ohne Angabe bleibt das heutige Verhalten.
Ohne Netz; jede Pool-Funktion ist eine Falle. Platzhalter-IDs, keine echten.
Vertrag: docs/contracts/energy-abo-bildweg.md.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from tests.unit.test_energy_abo_bildweg import (  # noqa: F401 — fixtures
    _convert, _doc_request, _handler, _img_body, abo, dev_bridge, prod_bridge,
)
import src.main as main  # noqa: E402
from src.models import ChatCompletionRequest, ResearchRequest  # noqa: E402
from src.providers import credit_pool, funding_policy as fp  # noqa: E402

USER_A = "00000000-0000-4000-8000-0000000000aa"   # Platzhalter: „Hetzner-Staging“
USER_B = "00000000-0000-4000-8000-0000000000bb"   # Platzhalter: andere Umgebung


def _req(headers):
    from starlette.requests import Request
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({'type': 'http', 'method': 'POST', 'path': '/v1/chat/completions',
                    'headers': raw, 'client': ("10.0.0.1", 1)})


ENERGY = {"X-App-ID": "werking-energy", "X-User-ID": USER_A}


@pytest.fixture
def pool_trap(monkeypatch):
    """Pool IST konfiguriert; jeder Pool-Zugriff schlägt laut an."""
    monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_URL", "http://pool")
    monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_TOKEN", "t")
    trap = AsyncMock(side_effect=AssertionError("credit pool touched"))
    monkeypatch.setattr(credit_pool, "coordinator", trap)
    monkeypatch.setattr(credit_pool, "post_messages", trap)
    return trap


@pytest.fixture
def switch(monkeypatch):
    monkeypatch.setenv(fp.SUBSCRIPTION_ONLY_ENV, "1")
    monkeypatch.setenv(fp.SUBSCRIPTION_ONLY_USERS_ENV, f" {USER_A.upper()} ,other@example.invalid")


# --- Vertrag ---------------------------------------------------------------

def test_no_indication_is_todays_behaviour(dev_bridge):
    assert fp.subscription_mode_cause(_req(ENERGY)) is None


@pytest.mark.parametrize("value", ["subscription", "ABO", " Subscription "])
def test_header_requests_subscription(dev_bridge, value):
    assert fp.subscription_mode_cause(_req({**ENERGY, "X-Funding-Source": value})) == fp.CAUSE_SUBSCRIPTION_REQUESTED


def test_header_pool_wins_over_switch(dev_bridge, switch):
    assert fp.subscription_mode_cause(_req({**ENERGY, "X-Funding-Source": "pool"})) is None


def test_unknown_header_value_400(dev_bridge):
    with pytest.raises(HTTPException) as e:
        fp.subscription_mode_cause(_req({**ENERGY, "X-Funding-Source": "paid"}))
    assert e.value.status_code == 400


@pytest.mark.parametrize("headers", [
    {"X-App-ID": "werking-report", "X-Funding-Source": "subscription"},
    {**ENERGY, "X-Funding-Source": "subscription"},
])
def test_header_where_not_allowed_409(prod_bridge, headers):
    with pytest.raises(HTTPException) as e:
        fp.subscription_mode_cause(_req(headers))
    assert e.value.status_code == 409 and e.value.detail["error"] == "subscription_funding_not_available"


def test_header_without_staging_switch_409(monkeypatch):
    monkeypatch.delenv(fp.FALLBACK_SWITCH_ENV, raising=False)
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "dev")
    with pytest.raises(HTTPException) as e:
        fp.subscription_mode_cause(_req({**ENERGY, "X-Funding-Source": "subscription"}))
    assert e.value.status_code == 409


def test_switch_needs_allowlisted_user(dev_bridge, switch, monkeypatch):
    assert fp.subscription_mode_cause(_req(ENERGY)) == fp.CAUSE_SUBSCRIPTION_MODE
    assert fp.subscription_mode_cause(_req({**ENERGY, "X-User-ID": USER_B})) is None
    assert fp.subscription_mode_cause(_req({"X-App-ID": "werking-energy"})) is None
    assert fp.subscription_mode_cause(_req({"X-App-ID": "werking-report", "X-User-ID": USER_A})) is None
    monkeypatch.setenv(fp.SUBSCRIPTION_ONLY_USERS_ENV, "")
    assert fp.subscription_mode_cause(_req(ENERGY)) is None  # leere Liste: fail-closed


def test_switch_never_on_prod(prod_bridge, switch):
    assert fp.subscription_mode_cause(_req(ENERGY)) is None


def test_switch_ignores_foreign_origin(dev_bridge, switch):
    """Prod-Anfrage über das Dev-Backup (Herkunft prod): nie aufs Dev-Abo."""
    from src import federation
    with patch.object(federation, "get_request_origin", return_value="prod"):
        assert fp.subscription_mode_cause(_req(ENERGY)) is None
        with pytest.raises(HTTPException) as e:
            fp.subscription_mode_cause(_req({**ENERGY, "X-Funding-Source": "subscription"}))
    assert e.value.status_code == 409


def test_status_expression(dev_bridge, switch, monkeypatch):
    s = fp.pool_umgehung_status()
    assert s["pool_umgangen_mit_header"] and s["pool_umgangen_ohne_header"]
    assert s["wege_ohne_header"] == {"chat": True, "bild": True, "document_convert": True, "research": True}
    assert s["allowlist_eintraege"] == 2 and s["gruende"] == []
    assert fp.pool_umgehung_status(USER_B)["pool_umgangen_ohne_header"] is False
    monkeypatch.setenv(fp.SUBSCRIPTION_ONLY_USERS_ENV, "")
    s = fp.pool_umgehung_status()
    assert s["pool_umgangen_ohne_header"] is False and "fail-closed" in s["gruende"][0]


def test_status_on_prod(prod_bridge, switch):
    s = fp.pool_umgehung_status()
    assert not s["pool_umgangen_mit_header"] and not s["pool_umgangen_ohne_header"] and s["gruende"]


def test_marker_names_bypass(dev_bridge):
    import contextvars
    req = _req({**ENERGY, "X-Funding-Source": "subscription"})
    # Eigener Kontext: der Ledger-Marker darf nicht in andere Tests auslaufen.
    assert contextvars.copy_context().run(fp.enter_subscription_mode, req) and fp.pool_umgangen(req)
    assert fp.response_marker(req) == {
        "funding_policy": "subscription_staging_fallback", "automatic_paid_fallback": False,
        "funding_fallback_cause": "subscription_requested", "funding_source": "subscription",
        "pool_umgangen": True}


# --- Chat: Pool nie berührt --------------------------------------------------

def _text_body():
    return ChatCompletionRequest(model='claude-sonnet-5-5', enable_tools=False,
                                 messages=[{'role': 'user', 'content': 'Hallo'}])


async def test_chat_text_bypasses_pool(dev_bridge, switch, abo, pool_trap):
    admission = AsyncMock(side_effect=HTTPException(429, "abo-limiter stop"))
    req = _req(ENERGY)
    with ExitStack() as stack:
        _handler(stack, admission)
        with pytest.raises(HTTPException) as e:
            await main._chat_completions_finanziert(_text_body(), req, None)
    assert e.value.detail == "abo-limiter stop"     # bis in die Abo-Zulassung
    admission.assert_awaited_once()
    pool_trap.assert_not_called()
    assert fp.pool_umgangen(req)


async def test_chat_image_bypasses_pool(dev_bridge, abo, pool_trap):
    cli, _, _ = abo
    req = _req({**ENERGY, "X-Funding-Source": "subscription"})
    with ExitStack() as stack:
        persist = _handler(stack, AsyncMock(return_value=None))
        out = await main._chat_completions_finanziert(_img_body(), req, None)
    pool_trap.assert_not_called()
    assert len(cli.calls) == 1 and out["x_funding"]["pool_umgangen"] is True
    (row,) = [c.kwargs for c in persist.await_args_list if c.kwargs.get("status") == "success"]
    meta = row["provider_meta"]
    assert meta["funding_policy"] == "subscription_staging_fallback" and meta["pool_umgangen"] is True
    assert meta["funding_source"] == "subscription" and not fp.pool_reservations(meta)
    assert row["provider"] == "anthropic"  # real_cost 0 über die Abo-Regel (resolve_ledger_cost)


async def test_chat_without_indication_still_asks_pool_first(dev_bridge, abo, pool_trap):
    """Ohne Angabe: heutiges Verhalten — der Pool wird zuerst gefragt."""
    with ExitStack() as stack:
        _handler(stack, AsyncMock(return_value=None), cap=AsyncMock(return_value=(False, 0.0, 0.0)))
        with pytest.raises(Exception):
            await main._chat_completions_finanziert(_img_body(), _req(ENERGY), None)
    assert pool_trap.call_count >= 1


async def test_chat_header_on_prod_409_no_pool(prod_bridge, abo, pool_trap):
    with pytest.raises(HTTPException) as e:
        await main._chat_completions_finanziert(_text_body(), _req({**ENERGY, "X-Funding-Source": "abo"}), None)
    assert e.value.status_code == 409
    pool_trap.assert_not_called()


# --- Dokument-Vision ---------------------------------------------------------

async def test_doc_convert_bypasses_pool(dev_bridge, switch, abo, pool_trap, monkeypatch):
    cli, _, _ = abo
    orig = _doc_request

    def doc_req(app_id="werking-energy"):
        r = orig(app_id)
        r.scope["headers"].append((b"x-user-id", USER_A.encode()))
        return r
    monkeypatch.setattr("tests.unit.test_energy_abo_bildweg._doc_request", doc_req)
    out, body, persist, req = await _convert(2)
    assert out.status_code == 200, body
    assert body["vision_funding"] == "subscription_staging_fallback" and len(cli.calls) == 2
    pool_trap.assert_not_called()
    assert fp.pool_umgangen(req)
    (row,) = [c.kwargs for c in persist.await_args_list]
    assert row["provider_meta"]["funding_fallback_cause"] == "subscription_mode"
    assert "credit_pool_reservations" not in row["provider_meta"]


async def test_doc_convert_without_indication_uses_pool(dev_bridge, abo, pool_trap):
    out, body, _, _ = await _convert(1)
    assert pool_trap.call_count >= 1  # heutiger Weg: Pool zuerst


# --- Recherche -----------------------------------------------------------------

async def test_research_bypasses_pool(dev_bridge, switch, abo, pool_trap):
    """Wegwahl im Abo-Modus: CLI-Abo-Weg, Zulassung über den Abo-Limiter,
    kein Cloud-/Pool-Weg."""
    stop = JSONResponse(status_code=429, content={"error": "abo-limiter stop"})
    admit = AsyncMock(return_value=stop)
    cloud = AsyncMock(side_effect=AssertionError("research-cloud (pool) path used"))
    req = _req(ENERGY)
    body = ResearchRequest(query="Testfrage", model="claude-sonnet-5-5")
    with ExitStack() as stack:
        stack.enter_context(patch.object(main, "verify_api_key", AsyncMock(return_value=None)))
        stack.enter_context(patch("src.routing.user_provider_override.enforce_user_provider_override",
                                  AsyncMock(return_value=None)))
        stack.enter_context(patch.object(main, "_admit_research_to_pool", admit))
        stack.enter_context(patch.object(main, "_execute_research_cloud_with_pool_fallback", cloud))
        stack.enter_context(patch("src.activity.ai_call_writer.persist_ai_call_activity", AsyncMock()))
        out = await main.research(body, req, None)
    admit.assert_awaited_once()
    cloud.assert_not_awaited()
    pool_trap.assert_not_called()
    assert fp.pool_umgangen(req) and getattr(out, "status_code", None) == 429


@pytest.mark.parametrize("liste", ["", " ", ",", " , ,, ", "\t,\n"])
def test_empty_or_blank_allowlist_never_forces_subscription(dev_bridge, monkeypatch, liste):
    """Fail-closed: ohne belegte ID erzwingt der Schalter nie das Abo — auch
    nicht für einen Aufruf ohne oder mit leerer X-User-ID."""
    monkeypatch.setenv(fp.SUBSCRIPTION_ONLY_ENV, "1")
    monkeypatch.setenv(fp.SUBSCRIPTION_ONLY_USERS_ENV, liste)
    for headers in (ENERGY, {"X-App-ID": "werking-energy"}, {"X-App-ID": "werking-energy", "X-User-ID": " "}):
        assert fp.subscription_mode_cause(_req(headers)) is None
    s = fp.pool_umgehung_status()
    assert s["pool_umgangen_ohne_header"] is False and s["allowlist_eintraege"] == 0
    assert all(v is False for v in s["wege_ohne_header"].values())


async def test_blank_allowlist_chat_still_pool_first(dev_bridge, monkeypatch, abo, pool_trap):
    monkeypatch.setenv(fp.SUBSCRIPTION_ONLY_ENV, "1")
    monkeypatch.setenv(fp.SUBSCRIPTION_ONLY_USERS_ENV, " , ")
    with ExitStack() as stack:
        _handler(stack, AsyncMock(return_value=None), cap=AsyncMock(return_value=(False, 0.0, 0.0)))
        with pytest.raises(Exception):
            await main._chat_completions_finanziert(_img_body(), _req(ENERGY), None)
    assert pool_trap.call_count >= 1   # heutiges Verhalten, kein erzwungenes Abo
