from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.privacy_service import model_proxy
from src.providers import privacy_proxy
from src.routing.bild_policy import CURRENT_APP


def test_privacy_contract_rejects_keys():
    from src.privacy_service.provider_credentials import (
        CONTRACT,
        require_keyless_runtime,
    )

    # Names are explicit and package-local; both sides checked in release test.
    assert {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"} <= set(
        CONTRACT["forbidden"]
    )
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        require_keyless_runtime({"ANTHROPIC_API_KEY": "do-not-log"})


@pytest.mark.parametrize(
    "app_id", ["werking-energy", "", "unknown-app", "werking-report"]
)
def test_privacy_proxy_blocks_image_before_provider(monkeypatch, app_id):
    monkeypatch.setenv("PRIVACY_MODEL_PROXY_TOKEN", "local-only")
    monkeypatch.setenv("ANTHROPIC_VISION_API_KEY", "provider-offline")
    app = FastAPI()
    app.include_router(privacy_proxy.router)
    sender = AsyncMock()
    monkeypatch.setattr(httpx.AsyncClient, "post", sender)
    response = TestClient(app).post(
        "/internal/privacy/messages",
        headers={"x-privacy-model-token": "local-only"},
        json={
            "app": app_id,
            "payload": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "url",
                                    "url": "https://offline.invalid/x",
                                },
                            }
                        ],
                    }
                ]
            },
        },
    )
    assert response.status_code == 403
    sender.assert_not_called()


def test_privacy_proxy_text_uses_only_server_key(monkeypatch):
    monkeypatch.setenv("PRIVACY_MODEL_PROXY_TOKEN", "local-only")
    monkeypatch.setenv("ANTHROPIC_VISION_API_KEY", "provider-offline")
    app = FastAPI()
    app.include_router(privacy_proxy.router)
    sender = AsyncMock(
        return_value=httpx.Response(
            200, json={"content": [{"type": "text", "text": "ok"}]}
        )
    )
    monkeypatch.setattr(httpx.AsyncClient, "post", sender)
    response = TestClient(app).post(
        "/internal/privacy/messages",
        headers={"x-privacy-model-token": "local-only"},
        json={
            "app": "werking-energy",
            "payload": {"messages": [{"role": "user", "content": "Text"}]},
        },
    )
    assert response.status_code == 200
    assert sender.call_args.kwargs["headers"]["x-api-key"] == "provider-offline"
    assert "provider-offline" not in response.text
    assert (
        TestClient(app).post("/internal/privacy/messages", json={}).status_code == 403
    )


def test_keyless_privacy_calls_bridge_not_provider(monkeypatch):
    import asyncio

    monkeypatch.setenv(
        "PRIVACY_MODEL_PROXY_URL", "http://bridge.invalid/internal/privacy"
    )
    monkeypatch.setenv("PRIVACY_MODEL_PROXY_TOKEN", "local-only")
    sender = AsyncMock(return_value=httpx.Response(200, json={"content": []}))
    monkeypatch.setattr(httpx.AsyncClient, "post", sender)
    context = CURRENT_APP.set("werking-energy")
    try:
        asyncio.run(
            model_proxy.post_messages(
                {"messages": [{"role": "user", "content": "Text"}]}
            )
        )
    finally:
        CURRENT_APP.reset(context)
    assert sender.call_args.args[0] == "http://bridge.invalid/internal/privacy/messages"
    assert sender.call_args.kwargs["headers"] == {"x-privacy-model-token": "local-only"}


def test_privacy_vision_capability_uses_only_gemini(monkeypatch):
    from types import SimpleNamespace

    from src.providers import gemini_vision

    monkeypatch.setenv("PRIVACY_MODEL_PROXY_TOKEN", "local-only")
    analyze = AsyncMock(
        return_value=SimpleNamespace(content="description", stop_reason="end_turn")
    )
    monkeypatch.setattr(
        gemini_vision,
        "get_gemini_vision_provider",
        lambda: SimpleNamespace(analyze=analyze),
    )
    app = FastAPI()
    app.include_router(privacy_proxy.router)
    response = TestClient(app).post(
        "/internal/privacy/vision",
        headers={"x-privacy-model-token": "local-only"},
        json={
            "messages": [{"role": "user", "content": "synthetic image placeholder"}],
            "model": gemini_vision.resolve_gemini_vision_model(),
            "max_tokens": 1500,
            "temperature": 0.1,
            "system_prompt": "Describe",
        },
    )
    assert response.status_code == 200
    assert response.json()["content"] == "description"
    analyze.assert_awaited_once()


def test_privacy_process_refuses_direct_provider_credentials():
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-c", "import src.privacy_service"],
        cwd=root,
        env={"PATH": os.defpath, "OPENAI_API_KEY": "offline-do-not-log"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "provider credential boundary" in result.stderr
    assert "offline-do-not-log" not in result.stderr
