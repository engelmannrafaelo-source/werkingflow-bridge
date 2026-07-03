"""
extract_result_usage: real API usage from CLI result chunks.

The critical case is the SDK ResultMessage after run_completion's attr-dict
conversion: it has 'subtype' + 'usage' but NO 'type' key. Every consumer that
checked `chunk.get("type") == "result"` missed it — which is why chat and
research billing silently fell back to the len//4 char estimate for years.
"""
from src.claude_cli import extract_result_usage


SDK_USAGE = {
    "input_tokens": 10,
    "output_tokens": 20_582,
    "cache_creation_input_tokens": 35_277,
    "cache_read_input_tokens": 9_057,
}


def test_sdk_resultmessage_attr_dict_without_type_key():
    chunk = {"subtype": "success", "usage": dict(SDK_USAGE), "num_turns": 1}
    assert extract_result_usage(chunk) == {
        "input_tokens": 10,
        "output_tokens": 20_582,
        "cache_creation_tokens": 35_277,
        "cache_read_tokens": 9_057,
    }


def test_json_mode_result_with_type_key():
    chunk = {"type": "result", "subtype": "complete", "usage": dict(SDK_USAGE)}
    assert extract_result_usage(chunk)["cache_read_tokens"] == 9_057


def test_missing_cache_fields_default_to_zero():
    chunk = {"subtype": "success", "usage": {"input_tokens": 5, "output_tokens": 7}}
    usage = extract_result_usage(chunk)
    assert usage["cache_creation_tokens"] == 0
    assert usage["cache_read_tokens"] == 0


def test_converted_assistant_message_is_not_a_result():
    # AssistantMessage attr-dicts have content/model but neither 'type' nor
    # 'subtype' — must not be mistaken for a result chunk.
    assert extract_result_usage({"content": [], "model": "claude-sonnet-4-5"}) is None


def test_non_result_typed_chunks_are_ignored():
    assert extract_result_usage({"type": "assistant", "usage": dict(SDK_USAGE)}) is None


def test_synthetic_error_markers_without_usage_yield_none():
    chunk = {"type": "result", "subtype": "timeout_incomplete", "is_error": True}
    assert extract_result_usage(chunk) is None


def test_non_dict_and_malformed_usage_yield_none():
    assert extract_result_usage("not a dict") is None
    assert extract_result_usage({"subtype": "success", "usage": "garbage"}) is None


def test_non_numeric_token_values_coerce_to_zero():
    chunk = {
        "subtype": "success",
        "usage": {"input_tokens": None, "output_tokens": {"nested": 1}},
    }
    usage = extract_result_usage(chunk)
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
