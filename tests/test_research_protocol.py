"""Tests for the depth-aware /v1/research CLI execution protocol.

Guards the 2026-08-04 fix: the CLI path used to discard --depth and cap
every research call at a hardcoded "2-3 searches" protocol, silently
dropping categories from multi-question briefs.
"""
import pytest

from src.research_protocol import build_research_execution_prompt, parse_depth


def _prompt(query: str, flags: str = "") -> str:
    p = f'/sc:research "{query}"'
    if flags:
        p += f" {flags}"
    return p


class TestParseDepth:
    def test_each_valid_depth(self):
        for depth in ("quick", "standard", "deep", "exhaustive"):
            assert parse_depth(_prompt("q", f"--depth {depth}")) == depth

    def test_missing_flag_defaults_to_standard(self):
        assert parse_depth(_prompt("q")) == "standard"


class TestBuildExecutionPrompt:
    def test_depth_budgets_reach_the_prompt(self):
        # Budgets must match the cloud-path SSoT (standard: 10/6, deep: 15/10).
        out, _, depth = build_research_execution_prompt(
            _prompt("q", "--depth deep --strategy planning"), 100
        )
        assert depth == "deep"
        assert "up to 15 searches" in out
        assert "up to 10 page fetches" in out

    def test_standard_no_longer_capped_at_3_searches(self):
        out, _, _ = build_research_execution_prompt(
            _prompt("q", "--depth standard --strategy planning"), 100
        )
        assert "2-3 TARGETED searches" not in out
        assert "up to 10 searches" in out

    def test_turn_cap_scales_with_depth(self):
        _, turns_std, _ = build_research_execution_prompt(
            _prompt("q", "--depth standard"), 100
        )
        _, turns_deep, _ = build_research_execution_prompt(
            _prompt("q", "--depth deep"), 100
        )
        assert turns_std == 30
        assert turns_deep == 45

    def test_turn_floor_applies(self):
        _, turns, _ = build_research_execution_prompt(_prompt("q", "--depth quick"), 5)
        assert turns == 20

    def test_caller_turns_below_cap_respected(self):
        _, turns, _ = build_research_execution_prompt(_prompt("q", "--depth deep"), 25)
        assert turns == 25

    def test_flags_stripped_from_query(self):
        out, _, _ = build_research_execution_prompt(
            _prompt("find VDI 2067 ranges", "--depth deep --strategy planning --max-hops 4"),
            100,
        )
        assert "--depth" not in out
        assert "--strategy" not in out
        assert "--max-hops" not in out
        assert "find VDI 2067 ranges" in out

    def test_oa_block_survives_verbatim(self):
        oa_block = "## OA-Literatur\n\n[1] Some paper abstract with --dashes-- inside."
        raw = _prompt("q", "--depth standard --strategy planning") + "\n\n" + oa_block
        out, _, _ = build_research_execution_prompt(raw, 100)
        assert oa_block in out

    def test_no_silent_drop_instruction_present(self):
        out, _, _ = build_research_execution_prompt(_prompt("q"), 100)
        assert "never drop a sub-question silently" in out
        assert "Offene Lücken" in out

    def test_multiline_query_supported(self):
        query = "Question 1: A?\nQuestion 2: B?\nQuestion 3: C?"
        out, _, _ = build_research_execution_prompt(_prompt(query, "--depth deep"), 100)
        assert "Question 3: C?" in out

    def test_plain_research_command_accepted(self):
        out, _, depth = build_research_execution_prompt('/research "q" --depth quick', 100)
        assert depth == "quick"
        assert "QUERY:" in out

    def test_non_research_prompt_rejected(self):
        with pytest.raises(ValueError):
            build_research_execution_prompt("just a chat message", 100)

    def test_empty_prompt_rejected(self):
        with pytest.raises(ValueError):
            build_research_execution_prompt("   ", 100)

    def test_flags_only_prompt_rejected(self):
        with pytest.raises(ValueError):
            build_research_execution_prompt("/sc:research --depth deep", 100)
