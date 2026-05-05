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


class TestChunksHaveToolUse:
    def test_text_only(self):
        chunks = [
            {"type": "system"},
            {"type": "assistant", "content": [{"type": "text", "text": "Hello"}]},
            {"type": "result"},
        ]
        assert not chunks_have_tool_use(chunks)

    def test_tool_use_in_top_level_content(self):
        chunks = [
            {"type": "assistant", "content": [{"type": "tool_use", "name": "read"}]},
        ]
        assert chunks_have_tool_use(chunks)

    def test_tool_use_in_nested_message(self):
        chunks = [
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "edit"}]}},
        ]
        assert chunks_have_tool_use(chunks)

    def test_mixed_text_and_tool(self):
        chunks = [
            {"type": "assistant", "content": [
                {"type": "text", "text": "I'll use a tool"},
                {"type": "tool_use", "name": "bash"},
            ]},
        ]
        assert chunks_have_tool_use(chunks)

    def test_empty_chunks(self):
        assert not chunks_have_tool_use([])

    def test_malformed_chunks_dont_crash(self):
        chunks = [None, "string", 42, {}, {"content": "not-a-list"}]
        assert not chunks_have_tool_use(chunks)
