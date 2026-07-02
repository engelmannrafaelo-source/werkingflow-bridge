"""Test find_truncation_marker: explicit truncation markers from run_completion.

Context: the SDK stream can end WITHOUT the CLI's result message (e.g. the
Anthropic client inside the claude CLI hits API_TIMEOUT_MS mid-generation).
run_completion yields {"type": "result", "subtype": "no_completion_marker",
"is_error": True} in that case — the non-streaming chat path must surface a
503 instead of returning the partial text as finish_reason=stop.
"""
from src.claude_cli import find_truncation_marker


def _text_chunk(text: str = "hello") -> dict:
    return {"type": "assistant", "content": [{"type": "text", "text": text}]}


class TestFindTruncationMarker:
    def test_clean_stream_has_no_marker(self):
        chunks = [_text_chunk(), {"type": "result", "subtype": "success", "is_error": False}]
        assert find_truncation_marker(chunks) is None

    def test_no_completion_marker_detected(self):
        marker = {
            "type": "result",
            "subtype": "no_completion_marker",
            "is_error": True,
            "chunks_received": 531,
        }
        chunks = [_text_chunk("partial json…"), marker]
        assert find_truncation_marker(chunks) is marker

    def test_timeout_incomplete_detected(self):
        marker = {"type": "result", "subtype": "timeout_incomplete", "is_error": True}
        assert find_truncation_marker([_text_chunk(), marker]) is marker

    def test_other_error_results_not_matched(self):
        # error_max_turns / error_during_execution keep their own handling paths
        chunks = [
            _text_chunk(),
            {"type": "result", "subtype": "error_max_turns", "is_error": True},
            {"type": "result", "subtype": "error_during_execution", "is_error": True},
        ]
        assert find_truncation_marker(chunks) is None

    def test_marker_without_is_error_not_matched(self):
        chunks = [{"type": "result", "subtype": "no_completion_marker", "is_error": False}]
        assert find_truncation_marker(chunks) is None

    def test_malformed_chunks_dont_crash(self):
        chunks = [None, "string", 42, {}, {"type": "result"}, _text_chunk()]
        assert find_truncation_marker(chunks) is None

    def test_empty_chunks(self):
        assert find_truncation_marker([]) is None
