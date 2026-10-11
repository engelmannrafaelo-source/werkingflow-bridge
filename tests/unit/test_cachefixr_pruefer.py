"""CACHEFIXR (Codex-Pruefung, 11.10.2026): die vier roten Pruefertests, uebernommen.

1. Doc-Agent-Job mit status=error darf nicht als done gespeichert werden.
2.-4. Markierter API-Fehlerzug, danach SDK-Absturz vor dem Result: run_completion
   wirft CliApiError, Chat sync antwortet mit Fehlerstatus, Streaming endet mit
   event: error statt [DONE]. Urteil: /root/projekte/koordination/CACHEFIXR-urteil.md.
"""

from unittest.mock import AsyncMock, patch

import pytest


async def test_doc_agent_error_must_not_be_marked_done():
    from src.jobs import registry
    from src.jobs.executors import doc_agent_executor

    done, error = AsyncMock(), AsyncMock()
    with (
        patch(
            "src.jobs.executors._self_post_json",
            AsyncMock(
                return_value={
                    "status": "error",
                    "error": "API error 400: invalid cache ttl",
                }
            ),
        ),
        patch.object(registry, "get_executor", return_value=doc_agent_executor),
        patch.object(registry.store_client, "mark_done", done),
        patch.object(registry.store_client, "mark_error", error),
        patch.object(registry.store_client, "heartbeat", AsyncMock()),
        patch.object(registry.store_client, "update_progress", AsyncMock()),
    ):
        await registry._run_body("review-job", "doc-agent", {}, None)
    assert not done.called, str(done.call_args)
    assert error.called


async def test_api_flag_then_transport_crash_must_raise(monkeypatch, tmp_path):
    from tests.unit.test_cli_api_fehler import (
        CliApiError,
        _api_error_turn,
        _make_cli,
        claude_cli,
        sdk,
    )

    monkeypatch.setattr(
        claude_cli, "MessageParseError", type("MessageParseError", (Exception,), {})
    )

    async def query(**kw):
        yield sdk.init()
        yield sdk.AssistantMessage(
            content=[sdk.TextBlock(text="Teilantwort")], model="m"
        )
        yield _api_error_turn()
        raise RuntimeError("broken pipe after api-error turn")

    monkeypatch.setattr(claude_cli, "query", query)
    with pytest.raises(CliApiError):
        async for _ in _make_cli(tmp_path).run_completion(prompt="x", model="m"):
            pass


@pytest.mark.parametrize("stream", [False, True])
def test_api_flag_then_crash_http_must_not_succeed(monkeypatch, tmp_path, stream):
    from tests.unit import test_cli_api_fehler as t

    async def broken(prompt, options):
        yield t.sdk.init()
        yield t.sdk.AssistantMessage(
            content=[t.sdk.TextBlock(text="Teilantwort")], model="m"
        )
        yield t._api_error_turn()
        raise RuntimeError("broken pipe after api-error turn")

    monkeypatch.setattr(t, "_query", lambda *args: broken)
    monkeypatch.setattr(
        t.claude_cli, "MessageParseError", type("MessageParseError", (Exception,), {})
    )
    resp = t._chat(monkeypatch, tmp_path, stream)
    if stream:
        assert "event: error" in resp.text and "[DONE]" not in resp.text, resp.text
    else:
        assert resp.status_code >= 400, resp.text
