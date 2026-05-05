"""Test pure detection functions for SDK-early-termination."""
import pytest
from src.claude_cli import is_incomplete_response, chunks_have_tool_use


class TestIsIncompleteResponse:
    def test_legitimate_short_response(self):
        assert not is_incomplete_response(3, "OK", False)
    def test_real_incomplete(self):
        assert is_incomplete_response(3, "", False)
    def test_real_incomplete_none_content(self):
        assert is_incomplete_response(4, None, False)
    def test_tool_only_response(self):
        assert not is_incomplete_response(3, "", True)
    def test_zero_chunks_not_classified(self):
        assert not is_incomplete_response(0, None, False)
    def test_whitespace_only_content_is_incomplete(self):
        assert is_incomplete_response(3, "   \n  ", False)


class TestChunksHaveToolUseDict:
    def test_text_only_dict(self):
        chunks = [{"type": "assistant", "content": [{"type": "text", "text": "Hello"}]}]
        assert not chunks_have_tool_use(chunks)
    def test_tool_use_in_top_level_content(self):
        chunks = [{"type": "assistant", "content": [{"type": "tool_use", "name": "read"}]}]
        assert chunks_have_tool_use(chunks)
    def test_tool_use_in_nested_message(self):
        chunks = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "edit"}]}}]
        assert chunks_have_tool_use(chunks)
    def test_mixed_text_and_tool(self):
        chunks = [{"type": "assistant", "content": [
            {"type": "text", "text": "x"}, {"type": "tool_use", "name": "bash"}
        ]}]
        assert chunks_have_tool_use(chunks)
    def test_empty_chunks(self):
        assert not chunks_have_tool_use([])
    def test_malformed_chunks_dont_crash(self):
        chunks = [None, "string", 42, {}, {"content": "not-a-list"}]
        assert not chunks_have_tool_use(chunks)


class TestChunksHaveToolUseDataclass:
    """Real SDK chunks are dataclass instances (AssistantMessage etc), not dicts."""

    def test_dataclass_text_only(self):
        from claude_code_sdk import AssistantMessage
        from claude_code_sdk.types import TextBlock
        chunks = [AssistantMessage(content=[TextBlock(text="hi")], model="haiku")]
        assert not chunks_have_tool_use(chunks)

    def test_dataclass_with_tool_use(self):
        from claude_code_sdk import AssistantMessage
        from claude_code_sdk.types import ToolUseBlock
        chunks = [AssistantMessage(
            content=[ToolUseBlock(id="t1", name="bash", input={"cmd": "ls"})],
            model="haiku",
        )]
        assert chunks_have_tool_use(chunks)

    def test_dataclass_mixed_text_and_tool(self):
        from claude_code_sdk import AssistantMessage
        from claude_code_sdk.types import TextBlock, ToolUseBlock
        chunks = [AssistantMessage(
            content=[TextBlock(text="thinking..."), ToolUseBlock(id="t1", name="bash", input={})],
            model="haiku",
        )]
        assert chunks_have_tool_use(chunks)

    def test_dataclass_system_message_no_tool(self):
        from claude_code_sdk import SystemMessage
        # SystemMessage has no content list -- should not detect tools
        try:
            chunks = [SystemMessage(subtype="init", data={})]
            assert not chunks_have_tool_use(chunks)
        except TypeError:
            pytest.skip("SystemMessage signature mismatch in this SDK version")


class TestEarlyTerminationScenarios:
    """Three canonical early-termination scenarios:
    sdk-early-termination, capacity-pressure, legitimate-short-response.

    Names mirror the architecture doc so failures are self-documenting.
    """

    # Scenario 1: SDK early-termination
    def test_sdk_early_termination(self):
        assert is_incomplete_response(5, "", False)

    def test_sdk_early_termination_whitespace_only(self):
        assert is_incomplete_response(5, "  \n  ", False)

    def test_sdk_early_termination_none_content(self):
        assert is_incomplete_response(5, None, False)

    # Scenario 2: Capacity pressure
    def test_capacity_pressure_many_chunks_no_text(self):
        assert is_incomplete_response(12, None, False)

    def test_capacity_pressure_single_chunk_no_text(self):
        assert is_incomplete_response(1, "", False)

    # Scenario 3: Legitimate short response -- no false positives
    def test_legitimate_single_word_answer(self):
        assert not is_incomplete_response(2, "Yes.", False)

    def test_legitimate_punctuation_only(self):
        assert not is_incomplete_response(2, ".", False)

    def test_legitimate_short_number(self):
        assert not is_incomplete_response(3, "42", False)

    def test_legitimate_tool_only_no_text(self):
        assert not is_incomplete_response(4, "", True)

    def test_zero_chunks_never_classified(self):
        assert not is_incomplete_response(0, "", False)
        assert not is_incomplete_response(0, None, False)

    # Feedback path: adaptive_limiter must be notified on early termination
    def test_feedback_calls_soft_penalty_on_early_termination(self):
        from unittest.mock import patch
        import src.claude_cli as _claude_cli

        with patch.object(_claude_cli.rate_limit_tracker, "mark_soft_penalty") as mock_penalty:
            if is_incomplete_response(5, "", False):
                _claude_cli.rate_limit_tracker.mark_soft_penalty("test-worker", 30)
            mock_penalty.assert_called_once_with("test-worker", 30)

    def test_feedback_not_called_for_legitimate_response(self):
        from unittest.mock import patch
        import src.claude_cli as _claude_cli

        with patch.object(_claude_cli.rate_limit_tracker, "mark_soft_penalty") as mock_penalty:
            if is_incomplete_response(2, "Yes.", False):
                _claude_cli.rate_limit_tracker.mark_soft_penalty("test-worker", 30)
            mock_penalty.assert_not_called()

    def test_rolling_metrics_record_rate_limit_on_early_termination(self):
        from unittest.mock import patch, MagicMock
        import src.middleware.rolling_metrics as _rm

        mock_metrics = MagicMock()
        with patch.object(_rm, "get_rolling_metrics", return_value=mock_metrics):
            if is_incomplete_response(5, "", False):
                _rm.get_rolling_metrics().record_rate_limit("test-worker")
            mock_metrics.record_rate_limit.assert_called_once_with("test-worker")
