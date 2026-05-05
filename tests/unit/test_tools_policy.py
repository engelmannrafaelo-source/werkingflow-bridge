"""Tools-policy enforcement: chat_completions has no tools unless explicit research header."""
import pytest
from src.main import enforce_tools_policy


class TestEnforceToolsPolicy:
    def test_default_off_stays_off(self):
        assert enforce_tools_policy(False, "") is False

    def test_explicit_on_without_header_dropped(self):
        """The whole point: client says tools=true, no header → forced off."""
        assert enforce_tools_policy(True, "") is False

    def test_explicit_on_with_research_header_honored(self):
        """/sc:research path — header signals legitimate tool need."""
        assert enforce_tools_policy(True, "Read,Grep,WebFetch") is True

    def test_explicit_on_with_wildcard_header_honored(self):
        """X-Claude-Allowed-Tools='*' → SDK default (all tools)."""
        assert enforce_tools_policy(True, "*") is True

    def test_explicit_off_with_header_stays_off(self):
        """Helper only enforces enable_tools field — header path runs separately."""
        assert enforce_tools_policy(False, "Read,Grep") is False

    def test_default_off_with_header_stays_off(self):
        """Default false, header set — pure pass-through, no upgrade."""
        assert enforce_tools_policy(False, "*") is False
