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
        # SystemMessage has no content list — should not detect tools
        try:
            chunks = [SystemMessage(subtype="init", data={})]
            assert not chunks_have_tool_use(chunks)
        except TypeError:
            pytest.skip("SystemMessage signature mismatch in this SDK version")
