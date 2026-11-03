"""Abo-Bildweg (KD 03.11.2026): Energy-Bilder über das Claude-Abo im Staging-Rückfall.

Ohne Netz: die CLI ist ein Fake-Runner (``subscription_vision.set_cli_runner``),
Pool, Limiter und Reserve sind gepatcht, Anthropic/Pool-HTTP ist verboten.
Vertrag: docs/contracts/energy-abo-bildweg.md.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import stat
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

for name in ['claude_code_sdk', 'claude_code_sdk._errors', 'claude_code_sdk._internal',
             'claude_code_sdk._internal.client', 'src.identity.routes', 'src.db.client']:
    if name not in sys.modules:
        sys.modules[name] = MagicMock()

import src.main as main  # noqa: E402
from src.models import ChatCompletionRequest  # noqa: E402
from src.providers import credit_pool, funding_policy as fp, subscription_vision as sv  # noqa: E402
from src.vision_provider import VisionProvider  # noqa: E402

SWITCH = fp.FALLBACK_SWITCH_ENV
PAID_KEY = "sk-ant-platzhalter-bezahlt-nie-benutzen"


def _bild(fmt: str = "PNG") -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (6, 4), (200, 10, 10)).save(buf, format=fmt)
    return buf.getvalue()


def _b64(fmt: str = "PNG") -> str:
    return base64.b64encode(_bild(fmt)).decode()


def _block(data: str, media: str = "image/png") -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}


class FakeCli:
    """Ersetzt ClaudeCodeCLI.run_completion. Liest jede genannte Bilddatei
    (wie das Read-Werkzeug) und antwortet; Varianten über ``mode``."""

    def __init__(self, mode: str = "ok", text: str = "Rotes Rechteck, Text TESTBILD 4711", delay: float = 0.0):
        self.mode, self.text, self.delay = mode, text, delay
        self.calls: list[dict] = []
        self.seen_files: list[dict] = []
        self.active = 0
        self.max_active = 0
        self.symlink_target = None

    async def run_completion(self, **kw):
        self.calls.append(kw)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            pfade = [z[2:] for z in kw["prompt"].splitlines() if z.startswith("- /")]
            for p in pfade:
                st = os.stat(p)
                self.seen_files.append({"path": p, "mode": stat.S_IMODE(st.st_mode),
                                        "dir_mode": stat.S_IMODE(os.stat(os.path.dirname(p)).st_mode),
                                        "bytes": Path(p).read_bytes()})
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.symlink_target is not None:
                # Ein fremder Prozess legt einen Symlink ins private Verzeichnis.
                os.symlink(self.symlink_target, os.path.join(os.path.dirname(pfade[0]), "boese-link"))
            if self.mode != "no_init":
                tools = ["Read", "Bash"] if self.mode == "init_more_tools" else ["Read"]
                yield {"subtype": "init", "data": {"tools": tools, "permissionMode": "bypassPermissions"}}
            if self.mode == "raise":
                raise RuntimeError("cli boom")
            lesen = pfade
            if self.mode == "skip_read":
                lesen = pfade[:-1]
            tools = [{"type": "tool_use", "name": "Read", "input": {"file_path": p}} for p in lesen]
            if self.mode == "bash":
                tools.append({"type": "tool_use", "name": "Bash", "input": {"command": "cat /etc/passwd"}})
            if self.mode == "foreign_read":
                tools.append({"type": "tool_use", "name": "Read", "input": {"file_path": "/root/.claude/.credentials.json"}})
            if self.mode == "traversal":
                tools.append({"type": "tool_use", "name": "Read",
                              "input": {"file_path": pfade[0] + "/../../etc/passwd"}})
            if tools:
                yield {"type": "assistant", "message_id": "m1", "stop_reason": "tool_use", "content": tools}
            antwort = {"type": "assistant", "message_id": "m2", "stop_reason": "end_turn",
                       "content": [{"type": "text", "text": self.text}]}
            if self.mode == "length":
                antwort["stop_reason"] = "max_tokens"
            if self.mode == "refusal":
                antwort["stop_reason"] = "refusal"
            yield antwort
            if self.mode == "no_result":
                return
            result = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
                      "usage": {"input_tokens": 1500, "output_tokens": 40,
                                "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3}}
            if self.mode == "max_turns":
                result.update(subtype="error_max_turns", is_error=True)
            yield result
        finally:
            self.active -= 1


@pytest.fixture
def dev_bridge(monkeypatch):
    monkeypatch.setenv(SWITCH, "1")
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "dev")


@pytest.fixture
def prod_bridge(monkeypatch):
    monkeypatch.setenv(SWITCH, "1")  # versehentlich gesetzt: Prod bleibt trotzdem strikt
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "prod")


@pytest.fixture
def abo(monkeypatch):
    """Fake-CLI + offene Abo-Grenzen; jeder bezahlte/Pool-Weg schlägt laut an."""
    cli = FakeCli()
    sv.set_cli_runner(cli)
    sv._reset_for_tests()
    gates = {"reserve": MagicMock(return_value=None),
             "limiter": AsyncMock(return_value=(True, None, {}, 0.0)),
             "rate": MagicMock(return_value=False)}
    limiter = MagicMock()
    limiter.acquire_with_wait = gates["limiter"]
    monkeypatch.setattr("src.middleware.account_reserve.require_account_reserve", gates["reserve"])
    monkeypatch.setattr("src.middleware.adaptive_limiter.get_adaptive_limiter", lambda: limiter)
    import src.claude_cli as cc
    monkeypatch.setattr(cc.rate_limit_tracker, "should_reject_new_request", gates["rate"])
    monkeypatch.setenv("ANTHROPIC_VISION_API_KEY", PAID_KEY)  # liegt live im Worker; darf nie greifen
    paid = AsyncMock(side_effect=AssertionError("paid/pool route used"))
    monkeypatch.setattr(credit_pool, "post_messages", paid)
    import src.vision_provider as vp

    class _NoHttp:
        def __init__(self, *a, **k):
            raise AssertionError("direct Anthropic HTTP used")
    monkeypatch.setattr(vp.httpx, "AsyncClient", _NoHttp)
    monkeypatch.setattr(vp, "_vision_provider", None)
    yield cli, gates, paid
    sv.set_cli_runner(main.claude_cli)
    sv._reset_for_tests()


async def _analyze(images, text="Beschreibe das Bild", **kw):
    msgs = [{"role": "user", "content": [*images, {"type": "text", "text": text}]}]
    return await sv.analyze_via_subscription(msgs, system_prompt=kw.pop("system", None),
                                             model="claude-sonnet-5-5", **kw)


# --- Eingangsprüfung ------------------------------------------------------

@pytest.mark.parametrize("fmt,mime,ext", [("PNG", "image/png", "png"), ("JPEG", "image/jpeg", "jpg"),
                                          ("GIF", "image/gif", "gif"), ("WEBP", "image/webp", "webp")])
def test_format_from_signature_not_from_caller(fmt, mime, ext):
    (raw, got_mime, got_ext), = sv.bilder_pruefen([_block(_b64(fmt), media="image/evil;../../x")])
    assert (got_mime, got_ext) == (mime, ext) and raw == _bild(fmt)


@pytest.mark.parametrize("images,status,code", [
    ([], 400, "abo_vision_no_image"),
    ([{"type": "image", "source": {"type": "url", "url": "http://169.254.169.254/x"}}], 400,
     "abo_vision_image_not_base64"),
    ([_block("@@@nicht base64@@@")], 400, "abo_vision_image_invalid_base64"),
    ([_block(base64.b64encode(b"#!/bin/sh\nrm -rf /").decode())], 415, "abo_vision_image_format"),
    ([_block(_b64())] * (sv.ABO_VISION_MAX_IMAGES + 1), 413, "abo_vision_too_many_images"),
])
def test_input_refusals_loud(images, status, code):
    with pytest.raises(HTTPException) as e:
        sv.bilder_pruefen(images)
    assert e.value.status_code == status and e.value.detail["error"] == code
    assert e.value.detail["automatic_paid_fallback"] is False


def test_size_limits(monkeypatch):
    monkeypatch.setattr(sv, "ABO_VISION_MAX_IMAGE_B64", 10)
    with pytest.raises(HTTPException) as e:
        sv.bilder_pruefen([_block(_b64())])
    assert e.value.status_code == 413 and e.value.detail["error"] == "abo_vision_image_too_large"
    monkeypatch.setattr(sv, "ABO_VISION_MAX_IMAGE_B64", 5 * 2**20)
    monkeypatch.setattr(sv, "ABO_VISION_MAX_TOTAL_B64", len(_b64()) + 1)
    with pytest.raises(HTTPException) as e:
        sv.bilder_pruefen([_block(_b64()), _block(_b64())])
    assert e.value.detail["error"] == "abo_vision_images_too_large"


# --- Der CLI-Lauf -------------------------------------------------------------

async def test_happy_path_isolated_file_only_read_no_session(dev_bridge, abo, caplog):
    cli, gates, paid = abo
    data = _b64()
    caplog.set_level(logging.DEBUG)
    resp = await _analyze([_block(data)], system="App-Auftrag: sachlich")
    (kw,) = cli.calls
    assert kw["allowed_tools"] == ["Read"] and kw["cli_tools"] == ["Read"]  # --tools Read
    for neu in ("Monitor", "TaskOutput", "TaskStop", "CronCreate", "RemoteTrigger", "EnterWorktree",
                "PushNotification"):
        assert neu in kw["disallowed_tools"]
    for verboten in ("Bash", "Write", "Edit", "WebFetch", "WebSearch", "Task", "Glob", "Grep", "mcp__*"):
        assert verboten in kw["disallowed_tools"]
    assert kw["session_id"] is None and kw["continue_session"] is False
    assert kw["enable_file_discovery"] is False and kw["max_turns"] == 1 + sv.ABO_VISION_EXTRA_TURNS
    assert kw["system_prompt"].startswith("App-Auftrag: sachlich") and "keine Anweisung" in kw["system_prompt"]
    (seen,) = cli.seen_files
    assert Path(seen["path"]).name == "bild-1.png" and "bridge-abo-bild-" in seen["path"]
    assert seen["mode"] == 0o600 and seen["dir_mode"] == 0o700 and seen["bytes"] == _bild()
    assert not os.path.exists(seen["path"]) and not os.path.exists(os.path.dirname(seen["path"]))
    assert resp.content == "Rotes Rechteck, Text TESTBILD 4711" and resp.stop_reason == "end_turn"
    assert resp.credit_meta is None and resp.api_key_lane == sv.LANE_SUBSCRIPTION
    # voller Eingang: 1500 ungecacht + 3 cache_creation + 7 cache_read
    assert resp.usage == {"prompt_tokens": 1510, "completion_tokens": 40, "total_tokens": 1550}
    assert resp.ledger_meta["uncached_input_tokens"] == 1500
    meta = resp.ledger_meta
    assert meta["funding_policy"] == "subscription_staging_fallback" and meta["automatic_paid_fallback"] is False
    assert meta["usage_source"] == "subscription_cli" and meta["subscription_vision"] is True
    assert not fp.pool_reservations(meta)
    gates["reserve"].assert_called_once()
    gates["limiter"].assert_awaited_once()
    paid.assert_not_called()
    assert data[:40] not in caplog.text  # kein Bildinhalt im Log


async def test_caller_text_never_in_path(dev_bridge, abo):
    cli, _, _ = abo
    await _analyze([_block(_b64())], text="../../etc/passwd bild-1.png; rm -rf /")
    (seen,) = cli.seen_files
    assert ".." not in seen["path"] and "passwd" not in seen["path"]


async def test_prod_bridge_never_runs_cli(prod_bridge, abo):
    cli, gates, _ = abo
    with pytest.raises(HTTPException) as e:
        await _analyze([_block(_b64())])
    assert e.value.status_code == 409 and cli.calls == [] and not gates["limiter"].await_count


async def test_reserve_blocks_before_cli(dev_bridge, abo):
    cli, gates, _ = abo
    gates["reserve"].side_effect = HTTPException(503, "10 % Nutzerreserve")
    with pytest.raises(HTTPException) as e:
        await _analyze([_block(_b64())])
    assert "Nutzerreserve" in str(e.value.detail) and cli.calls == []


async def test_limiter_and_rate_limit_block_before_cli(dev_bridge, abo):
    cli, gates, _ = abo
    gates["limiter"].return_value = (False, "cap full", {}, 60.0)
    with pytest.raises(HTTPException) as e:
        await _analyze([_block(_b64())])
    assert e.value.status_code == 429 and e.value.detail["error"] == "abo_vision_limiter"
    gates["limiter"].return_value = (True, None, {}, 0.0)
    gates["rate"].return_value = True
    with pytest.raises(HTTPException) as e:
        await _analyze([_block(_b64())])
    assert e.value.detail["error"] == "abo_vision_worker_rate_limited" and cli.calls == []


@pytest.mark.parametrize("mode,code", [("bash", "abo_vision_unexpected_tool_use"),
                                       ("foreign_read", "abo_vision_unexpected_tool_use"),
                                       ("traversal", "abo_vision_unexpected_tool_use"),
                                       ("skip_read", "abo_vision_image_not_read")])
async def test_prompt_injection_answer_withheld(dev_bridge, abo, mode, code):
    cli, _, _ = abo
    cli.mode = mode
    with pytest.raises(HTTPException) as e:
        await _analyze([_block(_b64()), _block(_b64("JPEG"))])
    assert e.value.status_code == 422 and e.value.detail["error"] == code
    assert e.value.detail["retryable"] is False
    assert all(not os.path.exists(s["path"]) for s in cli.seen_files)


async def test_cli_error_still_deletes_files(dev_bridge, abo):
    cli, _, _ = abo
    cli.mode = "raise"
    with pytest.raises(RuntimeError):
        await _analyze([_block(_b64())])
    assert cli.seen_files and not os.path.exists(os.path.dirname(cli.seen_files[0]["path"]))


async def test_output_limit_visible_not_silent(dev_bridge, abo, monkeypatch):
    cli, _, _ = abo
    cli.mode = "length"
    resp = await _analyze([_block(_b64())])
    assert resp.stop_reason == "max_tokens"  # Chat: finish_reason length, Dokument: Vermerk
    from src.vision_provider import finish_reason_for
    assert finish_reason_for(resp.stop_reason) == "length"
    cli.mode = "ok"
    monkeypatch.setattr(sv, "ABO_VISION_MAX_OUTPUT_CHARS", 10)
    with pytest.raises(HTTPException) as e:
        await _analyze([_block(_b64())])
    assert e.value.detail["error"] == "abo_vision_output_over_limit"


@pytest.mark.parametrize("mode,status,code", [("max_turns", 422, "abo_vision_run_unfinished"),
                                              ("no_result", 422, "abo_vision_run_unfinished")])
async def test_unfinished_run_is_error(dev_bridge, abo, mode, status, code):
    cli, _, _ = abo
    cli.mode = mode
    with pytest.raises(HTTPException) as e:
        await _analyze([_block(_b64())])
    assert e.value.status_code == status and e.value.detail["error"] == code


async def test_refusal_reported_as_content_filter(dev_bridge, abo):
    cli, _, _ = abo
    cli.mode = "refusal"
    resp = await _analyze([_block(_b64())])
    from src.vision_provider import finish_reason_for
    assert finish_reason_for(resp.stop_reason) == "content_filter"


async def test_concurrency_cap_per_worker(dev_bridge, abo):
    cli, _, _ = abo
    cli.delay = 0.05
    await asyncio.gather(*(_analyze([_block(_b64())]) for _ in range(7)))
    assert len(cli.calls) == 7 and cli.max_active == sv.ABO_VISION_MAX_PARALLEL


async def test_slot_wait_timeout_429(dev_bridge, abo, monkeypatch):
    cli, _, _ = abo
    cli.delay = 0.3
    monkeypatch.setattr(sv, "ABO_VISION_SLOT_WAIT_S", 0.05)
    results = await asyncio.gather(*(_analyze([_block(_b64())]) for _ in range(3)), return_exceptions=True)
    busy = [r for r in results if isinstance(r, HTTPException)]
    assert len(busy) == 1 and busy[0].status_code == 429 and busy[0].detail["error"] == "abo_vision_busy"


async def test_thinking_output_config_recorded_as_ignored(dev_bridge, abo):
    resp = await _analyze([_block(_b64())], output_config={"effort": "high"}, max_tokens=8192)
    assert resp.ledger_meta["ignored_params"] == ["output_config"]
    assert resp.ledger_meta["requested_max_tokens"] == 8192


# --- VisionProvider / Router -------------------------------------------------

def _openai_msgs():
    return [{"role": "user", "content": [{"type": "text", "text": "Was zeigt das?"},
                                         {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_b64()}"}}]}]


async def test_provider_explicit_flag_never_paid(dev_bridge, abo, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_URL", "http://pool")  # Pool an: trotzdem kein Nachversuch
    cli, _, paid = abo
    resp = await VisionProvider().analyze(_openai_msgs(), model="claude-sonnet-5-5", subscription_fallback=True)
    assert resp.api_key_lane == sv.LANE_SUBSCRIPTION and len(cli.calls) == 1
    paid.assert_not_called()


async def test_provider_reads_context_marker_only_after_activation(dev_bridge, abo):
    from starlette.requests import Request
    cli, _, paid = abo

    async def ohne():
        with pytest.raises(AssertionError, match="direct Anthropic HTTP used"):
            await VisionProvider().analyze(_openai_msgs(), model="m")

    async def mit():
        req = Request({"type": "http", "headers": [(b"x-app-id", b"werking-energy")]})
        fp.activate_subscription_fallback(req, fp.CAUSE_POOL_MISSING)
        return await VisionProvider().analyze(_openai_msgs(), model="m")
    await asyncio.create_task(ohne())
    resp = await asyncio.create_task(mit())
    assert resp.ledger_meta["funding_fallback_cause"] == "credit_pool_missing" and len(cli.calls) == 1


async def test_context_marker_ignored_on_prod(prod_bridge, abo):
    from src.providers.funding_policy import _ledger_marker
    cli, _, _ = abo

    async def lauf():
        _ledger_marker.set({"funding_policy": fp.SUBSCRIPTION_STAGING_FALLBACK})
        assert fp.subscription_fallback_in_context() is False
    await asyncio.create_task(lauf())


async def test_router_refuses_gemini_in_fallback(dev_bridge, abo):
    from src.routing.vision_router import VISION_TARGET_GEMINI, route_to_vision
    gemini = AsyncMock(side_effect=AssertionError("gemini used"))
    with patch("src.providers.gemini_vision.get_gemini_vision_provider", gemini):
        with pytest.raises(HTTPException) as e:
            await route_to_vision(_openai_msgs(), model="m", target=VISION_TARGET_GEMINI, subscription_fallback=True)
    assert e.value.status_code == 409 and "no paid fallback" in e.value.detail


async def test_router_books_subscription_lane(dev_bridge, abo):
    from src.routing.vision_router import route_to_vision
    res = await route_to_vision(_openai_msgs(), model="m", subscription_fallback=True)
    assert res.api_key_lane == "vision_subscription" and res.api_key_lane != "vision_prepaid"
    assert res.extra_meta["funding_policy"] == "subscription_staging_fallback"


# --- Chat-Pfad (echter Handler) --------------------------------------------

def _req(headers=None):
    from starlette.requests import Request
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {"X-App-ID": "werking-energy"}).items()]
    return Request({'type': 'http', 'method': 'POST', 'path': '/v1/chat/completions',
                    'headers': raw, 'client': ("10.0.0.1", 1)})


def _img_body(**extra):
    return ChatCompletionRequest(**{'model': 'claude-sonnet-5-5', 'enable_tools': False, 'messages': [
        {'role': 'user', 'content': [{'type': 'text', 'text': 'Was zeigt das Bild?'},
                                     {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{_b64()}'}}]}],
        **extra})


def _handler(stack, admission, cap=None):
    stack.enter_context(patch.object(main, "verify_api_key", AsyncMock(return_value=None)))
    stack.enter_context(patch.object(main, "validate_claude_code_auth", return_value=(True, {})))
    stack.enter_context(patch("src.budget.gate.enforce_budget", AsyncMock(return_value=None)))
    stack.enter_context(patch("src.routing.user_provider_override.enforce_user_provider_override",
                              AsyncMock(return_value=None)))
    stack.enter_context(patch.object(main, "enforce_pool_admission", admission))
    stack.enter_context(patch.object(main, "prepaid_vision_over_cap",
                                     cap or AsyncMock(side_effect=AssertionError("prepaid cap read"))))
    persist = AsyncMock()
    stack.enter_context(patch("src.activity.ai_call_writer.persist_ai_call_activity", persist))
    return persist


async def test_chat_image_pool_missing_runs_abo_through_admission(dev_bridge, abo, monkeypatch):
    monkeypatch.setenv("BRIDGE_VISION_DEFAULT_PROVIDER", "gemini")  # Dev-Standard: gilt im Rückfall nicht
    cli, _, paid = abo
    admission = AsyncMock(return_value=None)
    with ExitStack() as stack:
        persist = _handler(stack, admission)
        with patch.object(credit_pool, "enabled", return_value=False):
            out = await main._chat_completions_finanziert(_img_body(), _req(), None)
    admission.assert_awaited_once()
    assert len(cli.calls) == 1
    assert isinstance(out, dict)
    assert out["choices"][0]["message"]["content"].startswith("Rotes Rechteck")
    assert out["choices"][0]["finish_reason"] == "stop"
    assert out["x_funding"] == {"funding_policy": "subscription_staging_fallback",
                                "automatic_paid_fallback": False,
                                "funding_fallback_cause": "credit_pool_missing",
                                "funding_source": "subscription", "pool_umgangen": False}
    rows = [c.kwargs for c in persist.await_args_list if c.kwargs.get("status") == "success"]
    (row,) = rows
    assert row["provider"] == "anthropic"
    assert row["provider_meta"]["api_key_lane"] == "vision_subscription"
    assert row["provider_meta"]["usage_source"] == "subscription_cli"
    assert row["provider_meta"]["funding_policy"] == "subscription_staging_fallback"
    paid.assert_not_called()


async def test_chat_image_pool_unavailable_reruns_on_abo(dev_bridge, abo, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_URL", "http://pool")
    monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_TOKEN", "t")
    cli, _, paid = abo
    paid.side_effect = credit_pool.PoolUnavailable(429, "No eligible Anthropic API credit budget available")
    admission = AsyncMock(return_value=None)
    req = _req()
    cap = AsyncMock(return_value=(False, 0.0, 0.0))
    with ExitStack() as stack:
        _handler(stack, admission, cap=cap)
        await main._chat_completions_finanziert(_img_body(), req, None)
    cap.assert_awaited_once()           # nur der Pool-Versuch liest die Prepaid-Kappe
    paid.assert_awaited_once()          # Pool zuerst
    admission.assert_awaited_once()     # nur der Abo-Lauf geht durch die Abo-Zulassung
    assert len(cli.calls) == 1 and fp.fallback_cause(req) == fp.CAUSE_POOL_UNAVAILABLE


async def test_chat_image_on_prod_stays_strict(prod_bridge, abo):
    cli, _, _ = abo
    with ExitStack() as stack:
        _handler(stack, AsyncMock(return_value=None))
        with patch.object(credit_pool, "enabled", return_value=False):
            with pytest.raises(HTTPException) as e:
                await main._chat_completions_finanziert(_img_body(), _req(), None)
    assert e.value.status_code == 503 and cli.calls == []


async def test_chat_image_other_app_unchanged(dev_bridge, abo):
    """werking-report hat keinen Rückfall: kein Abo-Bildweg."""
    cli, _, _ = abo
    with ExitStack() as stack:
        _handler(stack, AsyncMock(return_value=None))
        with patch.object(credit_pool, "enabled", return_value=False):
            try:
                out = await main._chat_completions_finanziert(_img_body(), _req({"X-App-ID": "werking-report"}), None)
            except Exception as exc:  # noqa: BLE001
                out = exc
    status = getattr(out, "status_code", None)
    assert isinstance(out, Exception) or (status is not None and status >= 400)
    assert cli.calls == []


# --- Dokument-Vision (/v1/document/convert) ----------------------------------

class _FakeTrackCall:
    async def __aenter__(self):
        return 0

    async def __aexit__(self, *exc):
        return False


def _doc_request(app_id="werking-energy"):
    from starlette.requests import Request
    upload = MagicMock()
    upload.filename, upload.content_type = "a.pdf", "application/pdf"
    upload.read = AsyncMock(return_value=b"%PDF-1.4 fake")
    form = MagicMock()
    form.get = MagicMock(side_effect=lambda k, d=None: upload if k == "file" else d)
    form.multi_items = MagicMock(return_value=[("file", upload), ("describe_images", "true"),
                                               ("describe_prompt", "ENERGY-BESCHREIBUNG")])

    async def receive():
        await asyncio.sleep(3600)

    req = Request({"type": "http", "headers": [(b"x-app-id", app_id.encode())]}, receive)
    req.form = AsyncMock(return_value=form)
    return req


async def _convert(n_images: int, app_id="werking-energy"):
    upstream = {"success": True, "format": "pdf", "markdown": "# Doc", "images":
                {f"fig{i:02d}.png": _b64() for i in range(n_images)}, "image_count": n_images}
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value=upstream)
    pc = MagicMock()
    pc.post = AsyncMock(return_value=resp)
    pc.track_call = MagicMock(return_value=_FakeTrackCall())
    persist = AsyncMock()
    req = _doc_request(app_id)

    async def run():
        with (patch("src.main.get_privacy_client", return_value=pc),
              patch("src.main._record_document_call_metrics"),
              patch("src.main._maybe_repair_pdf_actualtext", AsyncMock(side_effect=lambda c, *a: c)),
              patch("src.activity.ai_call_writer.persist_ai_call_activity", persist),
              patch("src.main.verify_api_key", AsyncMock())):
            return await main.convert_document_endpoint(req, None)
    out = await asyncio.create_task(run())
    return out, json.loads(out.body), persist, req


async def test_doc_pool_missing_described_by_abo_and_booked(dev_bridge, abo, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_CREDIT_POOL_URL", raising=False)
    cli, _, paid = abo
    out, body, persist, req = await _convert(3)
    assert out.status_code == 200, body
    assert body["vision_funding"] == "subscription_staging_fallback"
    assert set(body["image_descriptions"]) == {"fig00.png", "fig01.png", "fig02.png"}
    assert "## Bildbeschreibungen" in body["markdown"] and len(cli.calls) == 3
    assert all("ENERGY-BESCHREIBUNG" in c["system_prompt"] for c in cli.calls)
    (row,) = [c.kwargs for c in persist.await_args_list]
    meta = row["provider_meta"]
    assert row["provider"] == "anthropic" and meta["funding_policy"] == "subscription_staging_fallback"
    assert meta["funding_fallback_cause"] == "credit_pool_missing" and meta["vision_calls"] == 3
    assert "credit_pool_reservations" not in meta and meta["automatic_paid_fallback"] is False
    assert (row["input_tokens"], row["output_tokens"]) == (4530, 120)  # voller Eingang inkl. Cache-Felder (Befund N1F P3)
    paid.assert_not_called()


async def test_doc_pool_unavailable_first_call_reruns_on_abo(dev_bridge, abo, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_URL", "http://pool")
    cli, _, paid = abo
    paid.side_effect = credit_pool.PoolUnavailable(429, "No eligible Anthropic API credit budget available")
    out, body, persist, req = await _convert(2)
    assert out.status_code == 200, body
    assert body["vision_funding"] == "subscription_staging_fallback" and len(cli.calls) == 2
    assert fp.fallback_cause(req) == fp.CAUSE_POOL_UNAVAILABLE
    (row,) = [c.kwargs for c in persist.await_args_list]
    assert row["provider_meta"]["funding_fallback_cause"] == "credit_pool_unavailable"


async def test_doc_abo_error_is_loud_status_1to1(dev_bridge, abo, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_CREDIT_POOL_URL", raising=False)
    cli, gates, _ = abo
    gates["limiter"].return_value = (False, "cap full", {}, 60.0)
    out, body, persist, _ = await _convert(2)
    assert out.status_code == 429 and "markdown" not in body
    assert cli.calls == []


async def test_doc_prod_bridge_stays_409(prod_bridge, abo, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_CREDIT_POOL_URL", raising=False)
    cli, _, _ = abo
    out, body, _, _ = await _convert(1)
    assert out.status_code == 409 and body["error"].startswith("promotional_only")
    assert cli.calls == []


def test_doc_estimate_uses_abo_parallelism():
    from src import docconvert_worker_vision as dw
    assert dw._runden(8, dw._abo_parallel()) == 8 // sv.ABO_VISION_MAX_PARALLEL
    assert dw._runden(8) == 2


@pytest.mark.parametrize("mode", ["init_more_tools", "no_init"])
async def test_tools_not_restricted_answer_withheld(dev_bridge, abo, mode):
    """Zweite Grenze: nennt die init-Meldung mehr als Read (oder fehlt sie),
    wird verworfen — auch wenn die Antwort sonst sauber wäre."""
    cli, _, _ = abo
    cli.mode = mode
    with pytest.raises(HTTPException) as e:
        await _analyze([_block(_b64())])
    assert e.value.status_code == 422 and e.value.detail["error"] == "abo_vision_tools_not_restricted"


async def test_cleanup_never_follows_symlink(dev_bridge, abo, tmp_path):
    cli, _, _ = abo
    fremd = tmp_path / "fremde-datei.txt"
    fremd.write_bytes(b"NICHT UEBERSCHREIBEN")
    cli.symlink_target = str(fremd)
    await _analyze([_block(_b64())])
    assert fremd.read_bytes() == b"NICHT UEBERSCHREIBEN"
    assert not os.path.exists(os.path.dirname(cli.seen_files[0]["path"]))


def test_run_completion_maps_cli_tools_to_tools_flag():
    """claude_cli: cli_tools wird zu extra_args {"tools": "Read"} (SDK 0.0.22
    reicht extra_args als --tools Read an die CLI)."""
    import inspect
    import src.claude_cli as cc
    src_text = inspect.getsource(cc.ClaudeCodeCLI.run_completion)
    assert "cli_tools" in inspect.signature(cc.ClaudeCodeCLI.run_completion).parameters
    assert '"tools": ",".join(cli_tools)' in src_text


@pytest.mark.parametrize("mode,status,code", [
    ("bash", 422, "abo_vision_unexpected_tool_use"),
    ("skip_read", 422, "abo_vision_image_not_read"),
    ("no_result", 422, "abo_vision_run_unfinished"),
])
async def test_chat_abo_error_status_one_to_one(dev_bridge, abo, mode, status, code):
    """Zweitprüfung N1: der Chat-Bildweg gibt den Status des Abo-Bildwegs 1:1
    weiter (422 wird von nginx nicht auf weiteren Workern wiederholt) — kein
    Umdeuten zu 429/500 durch classify_exception."""
    cli, _, _ = abo
    cli.mode = mode
    with ExitStack() as stack:
        _handler(stack, AsyncMock(return_value=None))
        with patch.object(credit_pool, "enabled", return_value=False):
            with pytest.raises(HTTPException) as e:
                await main._chat_completions_finanziert(_img_body(), _req(), None)
    assert e.value.status_code == status and e.value.detail["error"] == code
    assert len(cli.calls) == 1


async def test_chat_abo_busy_stays_429(dev_bridge, abo, monkeypatch):
    cli, gates, _ = abo
    gates["limiter"].return_value = (False, "cap full", {}, 60.0)
    with ExitStack() as stack:
        _handler(stack, AsyncMock(return_value=None))
        with patch.object(credit_pool, "enabled", return_value=False):
            with pytest.raises(HTTPException) as e:
                await main._chat_completions_finanziert(_img_body(), _req(), None)
    assert e.value.status_code == 429 and e.value.detail["error"] == "abo_vision_limiter"
    assert e.value.detail["retryable"] is True and cli.calls == []
