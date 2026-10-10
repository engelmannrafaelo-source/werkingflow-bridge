"""Isolated SDK entry point; credentials arrive only through stdin."""

import asyncio
import json
import posixpath
import re
import sys
from pathlib import Path
from typing import Any

from src.sdk_parser import install_resilient_parser

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


LESEWERKZEUGE = {"Read": "read", "Grep": "grep", "Glob": "glob"}
# Pfadwoerter in einem Shell-Befehl, die eingang/ beruehren (ohne Befehlstext).
_SHELL_PFAD = re.compile(r"""[^\s'"`;|&<>()]*eingang(?:/[^\s'"`;|&<>()]*)?""")


def _unter_eingang(raw: str, cwd: str, eingang: str) -> str | None:
    """Pfad relativ zu eingang/ ("." fuer den Ordner selbst), sonst None."""
    pfad = posixpath.normpath(posixpath.join(cwd, raw))
    if pfad == eingang:
        return "."
    if pfad.startswith(eingang + "/"):
        return pfad[len(eingang) + 1 :]
    return None


def lesezugriff(name: str, eingabe: Any, cwd: str) -> list[dict[str, str]]:
    """Read/Grep/Glob auf eingang/ und eingang-Pfade in Bash-Befehlen."""
    if not isinstance(eingabe, dict):
        return []
    cwd = posixpath.normpath(cwd)
    eingang = posixpath.join(posixpath.dirname(cwd), "eingang")
    if name in LESEWERKZEUGE:
        raw = eingabe.get("file_path", eingabe.get("path", "."))
        if not isinstance(raw, str):
            return []
        if name == "Glob" and isinstance(eingabe.get("pattern"), str):
            raw = posixpath.join(raw, eingabe["pattern"])
        pfad = _unter_eingang(raw, cwd, eingang)
        return [{"werkzeug": LESEWERKZEUGE[name], "pfad": pfad}] if pfad else []
    if name == "Bash" and isinstance(eingabe.get("command"), str):
        gefunden = []
        for wort in _SHELL_PFAD.findall(eingabe["command"]):
            pfad = _unter_eingang(wort, cwd, eingang)
            eintrag = {"werkzeug": "bash", "pfad": pfad}
            if pfad and eintrag not in gefunden:
                gefunden.append(eintrag)
        return gefunden
    return []


def sdk_options(body: dict[str, Any]) -> Any:
    from claude_code_sdk import ClaudeCodeOptions

    home = str(Path(body["ordner"]) / ".home")
    Path(home, "mpl").mkdir(parents=True, exist_ok=True)
    Path(home, "tmp").mkdir(parents=True, exist_ok=True)
    return ClaudeCodeOptions(
        model=body.get("modell", "claude-sonnet-5-5"),
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
    from claude_code_sdk import AssistantMessage, ResultMessage, ToolUseBlock, query

    install_resilient_parser()
    result = None
    zugriffe: list[dict[str, str]] = []
    async for message in query(prompt=body["prompt"], options=sdk_options(body)):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    zugriffe += lesezugriff(block.name, block.input, body["ordner"])
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
        "lesezugriffe": zugriffe,
    }


def failure_reason(error: Exception) -> str:
    """Return a status-safe reason without serializing SDK payloads or prompts."""
    return f"cli_fehler: SDK-Lauf: {type(error).__name__}"


if __name__ == "__main__":
    try:
        output = asyncio.run(run(json.load(sys.stdin)))
    except Exception as error:
        # SDK exceptions can contain credentials/prompts: never serialize them.
        print(json.dumps({"fehler": failure_reason(error)}))
        sys.exit(1)
    print(json.dumps(output))
