"""CACHE2 Fix 1 an der CLI-Schnittstelle: was ``query`` (claude_code_sdk) bekommt.

Die Kette davor (Request → Marke → Anonymisierung → Abflachen → Schnitt) prueft
test_cache_boundary.py. Diese Datei importiert src.claude_cli auf Modulebene und
teilt damit das vorbestehende Sammelproblem der Gesamtsuite (ein frueher gesammeltes
Modul stubbt claude_code_sdk); in eigener pytest-Sitzung (ohne dieses Modul)
laeuft sie gruen.
"""

from unittest.mock import Mock, patch

import pytest

from src.claude_cli import ClaudeCodeCLI
from tests.unit.test_cache_boundary import AUFGABE, STABIL, _chain, _marked_blocks, _req

# --- run_completion → query: what the SDK/CLI really receives ---------------

@pytest.fixture
def mock_session_mgr(monkeypatch):
    mgr = Mock()
    sess = Mock()
    sess.cli_session_id = "test-session-cache2"
    sess.cancellation_token = Mock()
    sess.cancellation_token.is_set.return_value = False
    mgr.create_session.return_value = sess
    monkeypatch.setattr("src.cli_session_manager.cli_session_manager", mgr)
    return mgr


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.setattr("src.auth.validate_claude_code_auth",
                        lambda: (True, {"valid": True, "method": "stub"}))
    auth = Mock()
    auth.get_claude_code_env_vars.return_value = {}
    monkeypatch.setattr("src.auth.auth_manager", auth)
    inst = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
    inst.timeout = 600
    inst.cwd = tmp_path
    inst.cache_dir = tmp_path
    inst.max_cache_size_mb = 10
    inst.claude_env_vars = {}
    inst.file_discovery = Mock()
    inst.file_discovery.discover_files_from_sdk_messages.return_value = []
    inst.file_discovery.discover_files_from_directory_scan.return_value = []
    return inst


async def _sdk_input(cli, **kw):
    got = {}

    async def fake_query(prompt, options):
        if isinstance(prompt, str):
            got["argv"] = prompt
        else:
            got["stream"] = [m async for m in prompt]
        return
        yield

    with patch("src.claude_cli.query", side_effect=fake_query):
        async for _ in cli.run_completion(**kw):
            pass
    return got


class TestCliInterface:
    @pytest.mark.asyncio
    async def test_blocks_and_boundary_reach_the_sdk(self, cli, mock_session_mgr):
        prompt, at, status, system = _chain(_req(_marked_blocks()).messages)
        got = await _sdk_input(cli, prompt=prompt, system_prompt=system, cache_boundary=at)
        assert "argv" not in got
        (msg,) = got["stream"]
        assert msg["type"] == "user" and msg["message"]["role"] == "user"
        assert msg["message"]["content"] == [
            {"type": "text", "text": "Human: " + STABIL, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "\n" + AUFGABE},
        ]

    @pytest.mark.asyncio
    async def test_without_boundary_small_prompt_stays_argv_string(self, cli, mock_session_mgr):
        got = await _sdk_input(cli, prompt="Human: hallo")
        assert got == {"argv": "Human: hallo"}

    @pytest.mark.asyncio
    async def test_prompt_rewritten_in_run_completion_drops_boundary_loudly(
            self, cli, mock_session_mgr, caplog):
        # enable_file_discovery rewrites the prompt (OUTPUT_FILE_PATH) — the
        # index would point into the wrong text, so the boundary must go.
        with caplog.at_level("ERROR"):
            got = await _sdk_input(cli, prompt="Human: " + STABIL, cache_boundary=10,
                                   enable_file_discovery=True,
                                   allowed_tools=["Write"])
        content = got["stream"][0]["message"]["content"] if "stream" in got else got["argv"]
        assert isinstance(content, str)
        assert "running WITHOUT cache boundary" in caplog.text
