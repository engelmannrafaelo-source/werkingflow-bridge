"""
Tests: thinking budget on the default Claude Code SDK path (2026-09-24).

Measured on Prod: report help-agent asked "Wie heißt diese App? Ein Wort."
with max_tokens=500 → 7018 completion tokens, 70 s, 3.8 cent. The CLI thinks
by default and the SDK path drops max_tokens; neither `thinking` nor the
X-Claude-Max-Thinking-Tokens header reached the CLI. Reproduced locally with
the claude CLI: default → thinking_tokens 565; MAX_THINKING_TOKENS=0 → 0.

These tests pin the mapping and that it reaches the SUBPROCESS env (not
os.environ, which concurrent requests share).
"""

import os
from types import SimpleNamespace
import pytest

from src.models import ChatCompletionRequest, Message


def _req(**kw) -> ChatCompletionRequest:
    return ChatCompletionRequest(model="haiku", messages=[Message(role="user", content="hi")], **kw)


class TestMapping:
    def test_disabled_maps_to_zero(self):
        assert _req(thinking={"type": "disabled"}).sdk_max_thinking_tokens() == 0
        assert _req(thinking={"type": "disabled"}).to_claude_options()["max_thinking_tokens"] == 0

    def test_enabled_budget_maps_to_budget(self):
        r = _req(thinking={"type": "enabled", "budget_tokens": 1024})
        assert r.to_claude_options()["max_thinking_tokens"] == 1024

    @pytest.mark.parametrize("thinking", [None, {"type": "adaptive"}, {"type": "enabled"},
                                          {"type": "enabled", "budget_tokens": "x"},
                                          {"type": "enabled", "budget_tokens": True}])
    def test_unmappable_leaves_cli_default(self, thinking):
        assert "max_thinking_tokens" not in _req(thinking=thinking).to_claude_options()

    def test_mapped_shape_does_not_warn(self, caplog):
        with caplog.at_level("WARNING"):
            _req(thinking={"type": "disabled"}).log_unsupported_parameters()
        assert not any("thinking" in r.message for r in caplog.records)

    def test_unmapped_shape_still_warns(self, caplog):
        with caplog.at_level("WARNING"):
            _req(thinking={"type": "adaptive"}).log_unsupported_parameters()
        assert any("thinking" in r.message for r in caplog.records)


class TestSubprocessEnv:
    def test_budget_reaches_subprocess_env_not_process_env(self):
        from src.claude_cli import apply_thinking_budget
        before = os.environ.get("MAX_THINKING_TOKENS")
        # Plain object: another test module stubs claude_code_sdk in sys.modules;
        # apply_thinking_budget only touches options.env.
        opts = SimpleNamespace(env={"KEEP": "1"})
        apply_thinking_budget(opts, 0)
        assert opts.env == {"KEEP": "1", "MAX_THINKING_TOKENS": "0"}
        assert os.environ.get("MAX_THINKING_TOKENS") == before

    def test_no_budget_leaves_env_untouched(self):
        from src.claude_cli import apply_thinking_budget
        opts = SimpleNamespace(env={})
        apply_thinking_budget(opts, None)
        assert "MAX_THINKING_TOKENS" not in (opts.env or {})

    def test_run_completion_applies_budget_and_chat_paths_pass_it(self):
        """Wiring: run_completion hands its parameter to apply_thinking_budget,
        and every chat call site in main.py passes max_thinking_tokens."""
        import inspect
        from pathlib import Path
        from src import claude_cli
        src = inspect.getsource(claude_cli.ClaudeCodeCLI.run_completion)
        assert "apply_thinking_budget(options, max_thinking_tokens)" in src
        main = (Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()
        chat_calls = main.count("max_thinking_tokens=claude_options.get('max_thinking_tokens')") + \
            main.count("max_thinking_tokens=retry_options.get('max_thinking_tokens')")
        assert chat_calls == 3  # streaming, non-streaming, tool-leak retry
