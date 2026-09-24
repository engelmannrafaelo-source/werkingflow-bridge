"""TOOLS_DISABLED_DENYLIST must name every built-in tool of the pinned CLI.

The SDK has no "disable all tools" switch: enable_tools=false works only by
listing each tool in disallowed_tools. Any built-in the list misses stays
callable, the model uses it, max_turns=1 ends in error_max_turns, the CLI
exits 1 and the caller sees sdk_disconnect. That happened on 2026-09-24 with
CLI 2.1.280 and its new 'ListAgents' tool (~1/3 of Sonnet chats on prod).

CLI_2_1_280_BUILTINS is the init 'tools' array of `claude -p hi --max-turns 1
--output-format stream-json --verbose` on CLI 2.1.280 (Dev worker, 24.09.).
When bumping the CLI in docker/Dockerfile.worker, re-run that command and update
this set — a failing test here is the reminder.
"""
import re
from pathlib import Path

from src.main import TOOLS_DISABLED_DENYLIST

CLI_2_1_280_BUILTINS = {
    'Bash', 'CronCreate', 'CronDelete', 'CronList', 'DesignSync', 'Edit',
    'EnterWorktree', 'ExitWorktree', 'ListAgents', 'NotebookEdit', 'Read',
    'ReportFindings', 'ScheduleWakeup', 'SendMessage', 'Skill', 'Task',
    'TaskCreate', 'TaskGet', 'TaskList', 'TaskStop', 'TaskUpdate',
    'ToolSearch', 'WebFetch', 'WebSearch', 'Workflow', 'Write',
}


def test_denylist_covers_every_cli_2_1_280_builtin():
    missing = CLI_2_1_280_BUILTINS - set(TOOLS_DISABLED_DENYLIST)
    assert not missing, f"tools-disabled chats can still call: {sorted(missing)}"


def test_list_agents_is_denied():
    """Regression 2026-09-24: ListAgents -> error_max_turns -> sdk_disconnect."""
    assert 'ListAgents' in TOOLS_DISABLED_DENYLIST


def test_builtin_set_matches_pinned_cli_version():
    """The pinned set is only valid for the CLI the worker image installs."""
    dockerfile = Path(__file__).resolve().parents[2] / 'docker' / 'Dockerfile.worker'
    text = dockerfile.read_text()
    versions = set(re.findall(r'@anthropic-ai/claude-code@(\d+\.\d+\.\d+)', text))
    assert versions == {'2.1.280'}, (
        f"Dockerfile.worker pins Claude Code {sorted(versions)} — re-read the "
        "init 'tools' array for that version and update CLI_2_1_280_BUILTINS"
    )
