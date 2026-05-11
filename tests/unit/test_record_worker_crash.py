"""
Regression test: record_worker_crash NameError fix.

Bug: _self_worker was defined only inside conditional branches (Z 2240, 2286).
When SDK returns zero chunks AND is_incomplete_response() returns False,
_self_worker is never bound → NameError swallowed by the except clause
→ record_worker_crash never fires → adaptive limiter stays blind.

Fix: _self_worker defined once at the top of the try block (= worker_id alias).
"""
import os
import sys
import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

# ── claude_code_sdk stub (not installed locally; runs inside Docker on bridge) ──
# Register a minimal stub so src.claude_cli can be imported without the real SDK.
if "claude_code_sdk" not in sys.modules:
    import types
    _sdk_stub = types.ModuleType("claude_code_sdk")

    class _ClaudeCodeOptions:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    class _Message:
        pass

    async def _query_stub(*args, **kwargs):
        return
        yield  # make it an async generator

    _sdk_stub.query = _query_stub
    _sdk_stub.ClaudeCodeOptions = _ClaudeCodeOptions
    _sdk_stub.Message = _Message

    # Minimal type stubs used in claude_cli
    _types_stub = types.ModuleType("claude_code_sdk.types")

    class _TextBlock:
        def __init__(self, text=""):
            self.type = "text"
            self.text = text

    class _ToolUseBlock:
        def __init__(self, id="", name="", input=None):
            self.type = "tool_use"
            self.id = id
            self.name = name
            self.input = input or {}

    _types_stub.TextBlock = _TextBlock
    _types_stub.ToolUseBlock = _ToolUseBlock

    class _AssistantMessage:
        def __init__(self, content=None, model=""):
            self.content = content or []
            self.model = model

    class _SystemMessage:
        def __init__(self, subtype="", data=None):
            self.subtype = subtype
            self.data = data or {}

    _sdk_stub.AssistantMessage = _AssistantMessage
    _sdk_stub.SystemMessage = _SystemMessage

    _errors_stub = types.ModuleType("claude_code_sdk._errors")

    class _MessageParseError(Exception):
        pass

    _errors_stub.MessageParseError = _MessageParseError

    sys.modules["claude_code_sdk"] = _sdk_stub
    sys.modules["claude_code_sdk.types"] = _types_stub
    sys.modules["claude_code_sdk._errors"] = _errors_stub


# ── helpers ──────────────────────────────────────────────────────────────────

async def _empty_generator(*args, **kwargs):
    """Async generator that yields nothing — simulates silent SDK stall."""
    return
    yield  # makes it an async generator


def _make_mock_metrics():
    m = MagicMock()
    m.record_arrival = MagicMock()
    m.record_worker_crash = MagicMock()
    m.record_rate_limit = MagicMock()
    m.record_completion = MagicMock()
    return m


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def worker_env(monkeypatch):
    monkeypatch.setenv("INSTANCE_NAME", "test-worker-crash")
    monkeypatch.setenv("API_KEY", "")          # disable API-key auth
    monkeypatch.setenv("CLAUDE_SKIP_AUTH", "1")
    yield "test-worker-crash"


@pytest.fixture()
def mock_metrics():
    return _make_mock_metrics()


# ── core regression test ──────────────────────────────────────────────────────

def _make_client(main_module, mock_metrics, mock_rl_tracker):
    """Build a TestClient with all necessary dependency overrides."""
    from starlette.testclient import TestClient
    from src.middleware.adaptive_limiter import adaptive_limit_dependency

    async def _override_adaptive():
        return None

    main_module.app.dependency_overrides[adaptive_limit_dependency] = _override_adaptive

    client = TestClient(main_module.app, raise_server_exceptions=False)
    return client


class TestRecordWorkerCrashNotNameError:
    """_self_worker must be defined when record_worker_crash is called."""

    def test_record_worker_crash_called_on_zero_chunks(self, worker_env, mock_metrics):
        """SDK returns zero chunks → record_worker_crash(worker) must fire, not NameError."""
        import src.main as main_module

        mock_rl = MagicMock()
        mock_rl.should_reject_new_request.return_value = False
        mock_rl.get_retry_after.return_value = 0
        mock_rl.is_hard_limited.return_value = False

        with (
            patch("src.main.validate_claude_code_auth", return_value=(True, {"method": "test"})),
            patch("src.main.verify_api_key", new_callable=AsyncMock),
            patch("src.main.rate_limit_tracker", mock_rl),
            patch.object(main_module.claude_cli, "run_completion", side_effect=_empty_generator),
            patch.object(main_module.claude_cli, "parse_claude_message", return_value=None),
            patch("src.middleware.rolling_metrics.get_rolling_metrics", return_value=mock_metrics),
        ):
            client = _make_client(main_module, mock_metrics, mock_rl)
            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude-sonnet-4-5",
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )

        main_module.app.dependency_overrides.clear()

        # Endpoint must return 500 (no content from SDK)
        assert resp.status_code == 500, (
            f"Expected 500 on zero-chunk SDK, got {resp.status_code}: {resp.text[:300]}"
        )
        # Core assertion: record_worker_crash called with the correct worker name
        mock_metrics.record_worker_crash.assert_called_once_with(worker_env)

    def test_record_worker_crash_not_called_on_good_response(self, worker_env, mock_metrics):
        """Sanity: record_worker_crash must NOT fire when SDK returns content."""
        import src.main as main_module

        async def _one_chunk_generator(*args, **kwargs):
            yield {"type": "assistant", "content": [{"type": "text", "text": "Hello!"}]}

        mock_rl = MagicMock()
        mock_rl.should_reject_new_request.return_value = False
        mock_rl.get_retry_after.return_value = 0
        mock_rl.is_hard_limited.return_value = False
        mock_rl.detect_in_text.return_value = None

        with (
            patch("src.main.validate_claude_code_auth", return_value=(True, {"method": "test"})),
            patch("src.main.verify_api_key", new_callable=AsyncMock),
            patch("src.main.rate_limit_tracker", mock_rl),
            patch.object(main_module.claude_cli, "run_completion", side_effect=_one_chunk_generator),
            patch.object(main_module.claude_cli, "parse_claude_message", return_value="Hello!"),
            patch("src.middleware.rolling_metrics.get_rolling_metrics", return_value=mock_metrics),
        ):
            client = _make_client(main_module, mock_metrics, mock_rl)
            client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude-sonnet-4-5",
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )

        main_module.app.dependency_overrides.clear()
        mock_metrics.record_worker_crash.assert_not_called()


# ── scope isolation test (pure logic, no HTTP) ────────────────────────────────

class TestSelfWorkerScope:
    """
    Direct verification that _self_worker is available outside conditional branches.
    Tests the invariant without requiring a full HTTP round-trip.
    """

    def test_self_worker_equals_instance_name(self, monkeypatch):
        """
        After the fix, _self_worker == os.getenv('INSTANCE_NAME', 'unknown')
        at any point in the function, regardless of which code branch ran.
        """
        monkeypatch.setenv("INSTANCE_NAME", "scope-check-worker")
        import importlib
        import src.main as main_module
        importlib.reload(main_module)

        # _self_worker is now worker_id, which is set unconditionally.
        # We verify by checking the source doesn't have the variable only in branches:
        import inspect
        src_lines = inspect.getsource(main_module.chat_completions)
        # The variable must NOT appear for the first time only inside an `if` block
        # — rough structural check: count top-level assignments vs branch assignments.
        # A proper check: if we can grep for the unconditional assignment.
        first_assignment = next(
            (l.strip() for l in src_lines.splitlines() if "_self_worker =" in l),
            None,
        )
        assert first_assignment is not None, "_self_worker must be assigned somewhere"
        # After fix: first assignment is before any `if is_incomplete_response`
        incomplete_check_pos = src_lines.find("is_incomplete_response(")
        self_worker_pos = src_lines.find("_self_worker =")
        assert self_worker_pos < incomplete_check_pos, (
            "_self_worker must be defined BEFORE the is_incomplete_response branch\n"
            f"  _self_worker first at: {self_worker_pos}\n"
            f"  is_incomplete_response at: {incomplete_check_pos}"
        )
