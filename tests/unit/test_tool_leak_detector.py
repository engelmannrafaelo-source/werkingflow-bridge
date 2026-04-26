"""Tests for src.tool_leak_detector."""

from src.tool_leak_detector import (
    DEFAULT_MIN_PROMPT_CHARS,
    DEFAULT_MIN_VALID_CHARS,
    TOOL_LEAK_GUARD_REMINDER,
    hardened_system_prompt,
    looks_like_tool_leak,
)


PROMPT_LARGE = DEFAULT_MIN_PROMPT_CHARS + 1
PROMPT_SMALL = DEFAULT_MIN_PROMPT_CHARS - 1


class TestLooksLikeToolLeak:
    def test_classic_write_to_file_intro(self):
        # The exact pattern observed in production (Phase 4 / Plasser run, 2026-04-25).
        assert looks_like_tool_leak(
            "I'll write the research prompt directly to the output file.",
            prompt_chars=PROMPT_LARGE,
        )

    def test_let_me_create_intro(self):
        assert looks_like_tool_leak("Let me create that markdown for you.", prompt_chars=PROMPT_LARGE)

    def test_i_will_generate_intro(self):
        assert looks_like_tool_leak("I will generate the file now.", prompt_chars=PROMPT_LARGE)

    def test_im_going_to_save(self):
        assert looks_like_tool_leak("I'm going to save the result.", prompt_chars=PROMPT_LARGE)

    def test_long_response_is_not_leak(self):
        # Anything past min_valid_chars is treated as real content.
        long_text = "I'll write this. " + ("Real content. " * 30)
        assert len(long_text) >= DEFAULT_MIN_VALID_CHARS
        assert not looks_like_tool_leak(long_text, prompt_chars=PROMPT_LARGE)

    def test_short_response_to_short_prompt_not_flagged(self):
        # Short prompts legitimately produce short responses ("Yes." etc.).
        assert not looks_like_tool_leak("I'll do that.", prompt_chars=PROMPT_SMALL)

    def test_short_response_no_intro_not_flagged(self):
        # A short response without agentic-intro is something else (e.g. a refusal),
        # not a tool leak — caller should surface it.
        assert not looks_like_tool_leak("No.", prompt_chars=PROMPT_LARGE)

    def test_empty_response_not_flagged(self):
        # The empty-response path is handled separately upstream.
        assert not looks_like_tool_leak("", prompt_chars=PROMPT_LARGE)
        assert not looks_like_tool_leak(None, prompt_chars=PROMPT_LARGE)

    def test_intro_in_middle_not_flagged(self):
        # We only flag responses that *open* with the intro.
        text = "# Real header\n\nI'll write more later."
        assert not looks_like_tool_leak(text, prompt_chars=PROMPT_LARGE)


class TestHardenedSystemPrompt:
    def test_prepends_to_existing(self):
        result = hardened_system_prompt("You are an assistant.")
        assert result.startswith(TOOL_LEAK_GUARD_REMINDER)
        assert result.endswith("You are an assistant.")

    def test_handles_none(self):
        result = hardened_system_prompt(None)
        assert result == TOOL_LEAK_GUARD_REMINDER

    def test_handles_empty_string(self):
        result = hardened_system_prompt("")
        assert result == TOOL_LEAK_GUARD_REMINDER
