"""
Tests for ARG_MAX / large-prompt argv fix.

Verifies that:
1. run_native_cli passes prompt via stdin (NOT as a positional argv arg).
2. run_completion does NOT set options.system_prompt for large system prompts,
   and instead writes the content to a temp file and sets the ContextVar so
   the subprocess-exec patch can inject --system-prompt-file.
3. The temp file is cleaned up in the finally block after the request.
4. Small prompts still work via the normal (argv) path.
5. A RuntimeError is raised when a large system_prompt is encountered and the
   subprocess exec patch was not applied (fail-loud, no silent failure).
"""

import asyncio
import os
import tempfile
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import AsyncMock, Mock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------
import src.claude_cli as _cli_module
from src.claude_cli import (
    ClaudeCodeCLI,
    LARGE_ARG_THRESHOLD_BYTES,
    _current_sys_prompt_tempfile,
    _subprocess_exec_patch_applied,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_contextvar():
    """Clear ContextVar before each test to avoid cross-test contamination."""
    _current_sys_prompt_tempfile.set(None)
    yield
    _current_sys_prompt_tempfile.set(None)


@pytest.fixture
def mock_auth(monkeypatch):
    """Stub out auth so ClaudeCodeCLI() can be constructed without real credentials."""
    monkeypatch.setattr(
        "src.auth.validate_claude_code_auth",
        lambda: (True, {"valid": True, "method": "stub"}),
    )
    mock_mgr = Mock()
    mock_mgr.get_claude_code_env_vars.return_value = {}
    monkeypatch.setattr("src.auth.auth_manager", mock_mgr)
    return mock_mgr


@pytest.fixture
def mock_session_mgr(monkeypatch):
    """Stub out cli_session_manager."""
    mock_mgr = Mock()
    sess = Mock()
    sess.cli_session_id = "test-session-xyz"
    sess.cancellation_token = Mock()
    sess.cancellation_token.is_set.return_value = False
    mock_mgr.create_session.return_value = sess
    mock_mgr.complete_session = Mock()
    monkeypatch.setattr("src.cli_session_manager.cli_session_manager", mock_mgr)
    return mock_mgr


@pytest.fixture
def cli(mock_auth, tmp_path):
    """Create a ClaudeCodeCLI instance with a writable cwd."""
    with patch("src.file_discovery.FileDiscoveryService.__init__", return_value=None):
        with patch.object(ClaudeCodeCLI, "_cleanup_old_cache_files", return_value=None):
            instance = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
            instance.timeout = 600
            instance.cwd = tmp_path
            instance.cache_dir = Path("/tmp")
            instance.max_cache_size_mb = 10
            instance.claude_env_vars = {}
            instance.file_discovery = Mock()
            instance.file_discovery.discover_files_from_sdk_messages.return_value = []
            instance.file_discovery.discover_files_from_directory_scan.return_value = []
            return instance


# ===========================================================================
# 1. run_native_cli — prompt via stdin, NOT argv
# ===========================================================================

class TestRunNativeCliStdin:
    """run_native_cli must pass the prompt via stdin, never as an argv element."""

    @pytest.mark.asyncio
    async def test_prompt_not_in_argv(self, cli, tmp_path):
        """The prompt string must NOT appear in the args list passed to subprocess.run."""
        prompt_text = "Hello from test"
        captured_kwargs = {}

        def fake_run(args, **kwargs):
            captured_kwargs.update(kwargs)
            captured_kwargs["args"] = args
            result = Mock()
            result.returncode = 0
            result.stdout = "ok"
            result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            await cli.run_native_cli(prompt=prompt_text, session_dir=tmp_path)

        assert prompt_text not in captured_kwargs["args"], (
            "Prompt must NOT be passed as an argv element (ARG_MAX risk)"
        )

    @pytest.mark.asyncio
    async def test_prompt_passed_via_stdin(self, cli, tmp_path):
        """subprocess.run must receive the prompt as the `input` keyword arg."""
        prompt_text = "Hello from test"
        captured_kwargs = {}

        def fake_run(args, **kwargs):
            captured_kwargs.update(kwargs)
            result = Mock()
            result.returncode = 0
            result.stdout = "ok"
            result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            await cli.run_native_cli(prompt=prompt_text, session_dir=tmp_path)

        assert captured_kwargs.get("input") == prompt_text, (
            "Prompt must be passed as input= (stdin) to subprocess.run"
        )

    @pytest.mark.asyncio
    async def test_large_prompt_via_stdin(self, cli, tmp_path):
        """A prompt larger than ARG_MAX threshold must also go via stdin."""
        large_prompt = "x" * (LARGE_ARG_THRESHOLD_BYTES + 10_000)
        captured_kwargs = {}

        def fake_run(args, **kwargs):
            captured_kwargs.update(kwargs)
            result = Mock()
            result.returncode = 0
            result.stdout = "done"
            result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            await cli.run_native_cli(prompt=large_prompt, session_dir=tmp_path)

        assert captured_kwargs.get("input") == large_prompt
        assert large_prompt not in captured_kwargs["args"]


# ===========================================================================
# 2. run_completion — system_prompt handling
# ===========================================================================

class TestRunCompletionLargeSystemPrompt:
    """run_completion must route large system prompts via a temp file, not argv."""

    def _make_sdk_message_stream(self):
        """Minimal async generator that looks like a successful SDK response."""
        async def _gen(*args, **kwargs):
            msg = Mock()
            msg.__class__.__name__ = "ResultMessage"
            msg.type = "result"
            msg.subtype = "success"
            # Make it behave like a dict for .get() calls in the code
            msg.get = lambda k, d=None: {"type": "result", "subtype": "success"}.get(k, d)
            yield msg
        return _gen

    @pytest.mark.asyncio
    async def test_small_system_prompt_uses_options(self, cli, mock_session_mgr, tmp_path):
        """system_prompt smaller than threshold → options.system_prompt is set normally."""
        small_sp = "Be concise."
        options_received = {}

        async def fake_query(prompt, options):
            options_received["system_prompt"] = getattr(options, "system_prompt", None)
            return
            yield  # make it an async generator

        with patch("src.claude_cli.query", side_effect=fake_query):
            async for _ in cli.run_completion(
                prompt="hi",
                system_prompt=small_sp,
            ):
                pass

        assert options_received.get("system_prompt") == small_sp

    @pytest.mark.asyncio
    async def test_large_system_prompt_not_in_options(self, cli, mock_session_mgr, tmp_path):
        """system_prompt > threshold must NOT be set on options (would go as argv)."""
        large_sp = "S" * (LARGE_ARG_THRESHOLD_BYTES + 10_000)
        options_received = {}

        async def fake_query(prompt, options):
            options_received["system_prompt"] = getattr(options, "system_prompt", None)
            return
            yield

        with patch("src.claude_cli.query", side_effect=fake_query):
            async for _ in cli.run_completion(
                prompt="hi",
                system_prompt=large_sp,
            ):
                pass

        assert options_received.get("system_prompt") is None, (
            "Large system_prompt must NOT be put on options.system_prompt (would become argv)"
        )

    @pytest.mark.asyncio
    async def test_large_system_prompt_written_to_temp_file(
        self, cli, mock_session_mgr, tmp_path
    ):
        """For a large system_prompt, a readable temp file must exist during the query."""
        large_sp = "S" * (LARGE_ARG_THRESHOLD_BYTES + 10_000)
        tempfile_path_during_query: list = []

        async def fake_query(prompt, options):
            # Capture ContextVar value while the query is in progress
            tempfile_path_during_query.append(_current_sys_prompt_tempfile.get())
            return
            yield

        with patch("src.claude_cli.query", side_effect=fake_query):
            async for _ in cli.run_completion(
                prompt="hi",
                system_prompt=large_sp,
            ):
                pass

        assert len(tempfile_path_during_query) == 1
        path = tempfile_path_during_query[0]
        assert path is not None, "ContextVar must be set to the temp file path"

        # The temp file is cleaned up in the finally block, so by now it should be gone.
        assert not os.path.exists(path), (
            "Temp file must be cleaned up after the request completes"
        )

    @pytest.mark.asyncio
    async def test_large_system_prompt_temp_file_contains_correct_content(
        self, cli, mock_session_mgr
    ):
        """Content written to the temp file must exactly match the original system_prompt."""
        large_sp = "SYSTEM_" * 20_000  # ~140 KB

        file_content_snapshot: list = []

        async def fake_query(prompt, options):
            path = _current_sys_prompt_tempfile.get()
            if path and os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    file_content_snapshot.append(fh.read())
            return
            yield

        with patch("src.claude_cli.query", side_effect=fake_query):
            async for _ in cli.run_completion(
                prompt="hi",
                system_prompt=large_sp,
            ):
                pass

        assert file_content_snapshot == [large_sp], (
            "Temp file must contain the exact system_prompt text"
        )

    @pytest.mark.asyncio
    async def test_contextvar_cleared_after_request(self, cli, mock_session_mgr):
        """ContextVar must be None after run_completion finishes (cleanup in finally)."""
        large_sp = "X" * (LARGE_ARG_THRESHOLD_BYTES + 1)

        async def fake_query(prompt, options):
            return
            yield

        with patch("src.claude_cli.query", side_effect=fake_query):
            async for _ in cli.run_completion(
                prompt="hi",
                system_prompt=large_sp,
            ):
                pass

        assert _current_sys_prompt_tempfile.get() is None, (
            "ContextVar must be cleared after the request (finally block)"
        )

    @pytest.mark.asyncio
    async def test_large_system_prompt_fails_loudly_without_patch(
        self, cli, mock_session_mgr, monkeypatch
    ):
        """RuntimeError must be raised when patch is not applied and system_prompt is large."""
        monkeypatch.setattr(_cli_module, "_subprocess_exec_patch_applied", False)

        large_sp = "Y" * (LARGE_ARG_THRESHOLD_BYTES + 1)

        with pytest.raises(RuntimeError, match="subprocess exec patch was NOT applied"):
            async for _ in cli.run_completion(
                prompt="hi",
                system_prompt=large_sp,
            ):
                pass


# ===========================================================================
# 3. Module-level constants and patch state
# ===========================================================================

class TestModuleConstants:
    def test_threshold_is_below_arg_max(self):
        """LARGE_ARG_THRESHOLD_BYTES must leave a safe margin below typical ARG_MAX."""
        # Linux ARG_MAX is typically 131072 bytes (128 KB).
        # Our threshold must be strictly less than that.
        assert LARGE_ARG_THRESHOLD_BYTES < 131_072, (
            "Threshold must be below the Linux ARG_MAX limit of 128 KB"
        )
        assert LARGE_ARG_THRESHOLD_BYTES >= 10_000, (
            "Threshold must be large enough not to trigger for normal prompts"
        )

    def test_patch_was_applied_at_import(self):
        """The subprocess exec patch must be applied when the module is imported."""
        assert _subprocess_exec_patch_applied is True, (
            "The subprocess exec patch must have been applied during module import"
        )

    def test_contextvar_default_is_none(self):
        """ContextVar default must be None (no temp file for requests that don't need it)."""
        assert _current_sys_prompt_tempfile.get() is None
