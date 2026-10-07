import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from src.jobs.executors import ExecutorHTTPError, erkunder_executor


@pytest.fixture
def setup(monkeypatch, tmp_path):
    tokenfile = tmp_path / "token"
    tokenfile.write_text("synthetic-secret-never-log")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_FILE", str(tokenfile))
    monkeypatch.setenv("ERKUNDER_INTERNAL_TOKEN", "internal-synthetic")
    monkeypatch.setenv("ERKUNDER_URL", "http://coordinator.test")
    monkeypatch.setenv("INSTANCE_NAME", "worker2")
    return tokenfile


def mock_http(monkeypatch, handler):
    factory = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs),
    )


async def test_normal(monkeypatch, setup, auftrag, caplog):
    polls = 0

    def handler(req):
        nonlocal polls
        assert req.headers["X-Erkunder-Intern"] == "internal-synthetic"
        if req.url.path == "/start":
            assert b"synthetic-secret-never-log" in req.content
            return httpx.Response(200, json={"angehaengt": False})
        polls += 1
        state = {"zustand": "laeuft", "schritt": "erkunder-1", "fertig": 0, "gesamt": 5}
        if polls == 2:
            state.update(
                zustand="fertig",
                fertig=5,
                meta={
                    "schema": "erkunder-ergebnis/1",
                    "bericht_id": auftrag["bericht_id"],
                    "prompt_version": "erkunder-prompts/1",
                    "modell": "claude-sonnet-5-5",
                    "schritte": [],
                    "erkunder_ausgefallen": [],
                    "korrekturkreis_gelaufen": False,
                },
            )
        return httpx.Response(200, json=state)

    mock_http(monkeypatch, handler)
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    progress = AsyncMock()
    result = await erkunder_executor(auftrag, None, progress)
    assert result["bericht_id"] == auftrag["bericht_id"]
    assert progress.await_count == 2
    sleep.assert_awaited_once_with(15)
    assert "synthetic-secret-never-log" not in caplog.text
    assert auftrag["vorwissen_md"] not in caplog.text


async def test_busy(monkeypatch, setup, auftrag):
    mock_http(
        monkeypatch, lambda req: httpx.Response(409, json={"belegt": "another-id"})
    )
    with pytest.raises(ExecutorHTTPError) as err:
        await erkunder_executor(auftrag, None, AsyncMock())
    assert err.value.status_code == 429 and err.value.retry_after_s == 120


async def test_abort(monkeypatch, setup, auftrag, caplog):
    mock_http(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            json=(
                {"angehaengt": True}
                if req.url.path == "/start"
                else {
                    "zustand": "abbruch",
                    "schritt": "erkunder-2",
                    "fertig": 0,
                    "gesamt": 5,
                    "fehler": {"grund": "zeit: synthetic-secret-never-log"},
                }
            ),
        ),
    )
    with pytest.raises(RuntimeError, match="erkunder abbruch: erkunder-2: zeit"):
        await erkunder_executor(auftrag, None, AsyncMock())
    assert "synthetic-secret-never-log" not in caplog.text


async def test_missing_token(monkeypatch, auftrag):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_FILE", raising=False)
    with pytest.raises(RuntimeError, match="TOKEN_FILE fehlt"):
        await erkunder_executor(auftrag, None, AsyncMock())


async def test_missing_file(setup, auftrag):
    setup.unlink()
    with pytest.raises(RuntimeError, match="nicht lesbar"):
        await erkunder_executor(auftrag, None, AsyncMock())


async def test_invalid_payload(auftrag):
    auftrag["korrekturkreis"] = 2
    with pytest.raises(ExecutorHTTPError) as err:
        await erkunder_executor(auftrag, None, AsyncMock())
    assert err.value.status_code == 400


async def test_timeout(monkeypatch, setup, auftrag):
    monkeypatch.setenv("ERKUNDER_JOB_TIMEOUT_S", "0.001")
    mock_http(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            json=(
                {"angehaengt": True}
                if req.url.path == "/start"
                else {
                    "zustand": "laeuft",
                    "schritt": "erkunder-1",
                    "fertig": 0,
                    "gesamt": 5,
                }
            ),
        ),
    )
    with pytest.raises(RuntimeError, match="Gesamtfrist"):
        await erkunder_executor(auftrag, None, AsyncMock())
