"""Isolated SDK entry point; credentials arrive only through stdin."""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ALLOWED_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "LS"]
DISALLOWED_TOOLS = [
    "Task",
    "exit_plan_mode",
    "MultiEdit",
    "NotebookRead",
    "NotebookEdit",
    "WebFetch",
    "TodoRead",
    "TodoWrite",
    "WebSearch",
    "ToolSearch",
    "Workflow",
    "Skill",
    "Agent",
    "SendMessage",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "TaskStop",
    "TaskUpdate",
    "CronCreate",
    "CronDelete",
    "CronList",
    "Monitor",
    "ScheduleWakeup",
    "DesignSync",
    "EnterWorktree",
    "ExitWorktree",
    "PushNotification",
    "RemoteTrigger",
    "ReportFindings",
    "ListAgents",
]


def sdk_options(body: dict[str, Any]) -> Any:
    from claude_code_sdk import ClaudeCodeOptions

    home = str(Path(body["ordner"]) / ".home")
    Path(home, "mpl").mkdir(parents=True, exist_ok=True)
    Path(home, "tmp").mkdir(parents=True, exist_ok=True)
    return ClaudeCodeOptions(
        model="claude-sonnet-5-5",
        cwd=body["ordner"],
        max_turns=body["max_turns"],
        allowed_tools=ALLOWED_TOOLS,
        disallowed_tools=DISALLOWED_TOOLS,
        mcp_servers={},
        permission_mode="bypassPermissions",
        extra_args={"settings": "/etc/erkunder/settings.json"},
        env={
            "HOME": home,
            "TMPDIR": f"{home}/tmp",
            "CLAUDE_CODE_OAUTH_TOKEN": body["claude_token"],
            "MPLCONFIGDIR": f"{home}/mpl",
            "PATH": "/opt/rechnen/bin:/usr/local/bin:/usr/bin:/bin",
        },
    )


async def run(body: dict[str, Any]) -> dict[str, Any]:
    from claude_code_sdk import ResultMessage, query

    result = None
    async for message in query(prompt=body["prompt"], options=sdk_options(body)):
        if isinstance(message, ResultMessage):
            result = message
    if result is None or result.is_error:
        raise RuntimeError("cli_fehler: SDK ohne erfolgreiches ResultMessage")
    usage = result.usage or {}
    return {
        "zuege": result.num_turns,
        "tokens": {
            "input": usage.get("input_tokens", 0),
            "output": usage.get("output_tokens", 0),
            "cache_read": usage.get("cache_read_input_tokens", 0),
            "cache_creation": usage.get("cache_creation_input_tokens", 0),
        },
    }


if __name__ == "__main__":
    try:
        output = asyncio.run(run(json.load(sys.stdin)))
    except Exception:
        # SDK exceptions can contain credentials/prompts: never serialize them.
        print(json.dumps({"fehler": "cli_fehler: SDK-Lauf gescheitert"}))
        sys.exit(1)
    print(json.dumps(output))
