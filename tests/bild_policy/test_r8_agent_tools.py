"""Offline: real entry methods, exact SDK options, no SDK/network credentials."""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.routing import bild_policy as bp


@pytest.fixture(autouse=True)
def energy():
    token = bp.CURRENT_APP.set("werking-energy")
    yield
    bp.CURRENT_APP.reset(token)


@pytest.mark.parametrize(
    "tools",
    [None, ["WebFetch"], ["Agent"], ["mcp__x"]],
)
def test_unsafe_tools_are_refused(tools):
    with pytest.raises(bp.ImageEgressRefused, match="Werkzeugzugriff") as error:
        bp.guard_agent_tools("test", tools)
    assert error.value.status == 403


def test_no_tools_are_removed_from_cli_not_just_permission_denied():
    options = SimpleNamespace(
        allowed_tools=[],
        disallowed_tools=["Read", "Bash"],
        mcp_servers={},
        extra_args={"tools": "default"},
    )
    bp.constrain_agent_options(options)
    assert options.extra_args["tools"] == ""
    assert options.extra_args["strict-mcp-config"] is None
    assert options.extra_args["setting-sources"] == ""
    assert options.extra_args["disable-slash-commands"] is None


def test_explicit_text_tools_are_preserved():
    options = SimpleNamespace(
        allowed_tools=["Grep", "Glob"],
        disallowed_tools=[],
        mcp_servers={},
        extra_args={},
    )
    bp.constrain_agent_options(options)
    assert options.extra_args["tools"] == "Grep,Glob"


@pytest.mark.parametrize(
    "extra", [{"resume": "old-session"}, {"mcp_servers": {"x": {}}}]
)
def test_history_and_other_tool_servers_cannot_reintroduce_images(extra):
    with pytest.raises(bp.ImageEgressRefused):
        bp.guard_agent_tools("test", ["Grep"], **extra)


def test_allowed_other_app_is_unchanged():
    bp.CURRENT_APP.set("werking-report")
    assert bp.guard_agent_tools("test", ["Read", "Bash"]) is None


def original_method(name):
    """Load the original method without optional SDK import side effects."""
    source = Path(__file__).parents[2] / "src/claude_cli.py"
    tree = ast.parse(source.read_text())
    node = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == name
    )
    scope = {}
    exec("from typing import *\nfrom pathlib import Path", scope)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), scope)
    return scope[name]


def test_native_entry_refuses_before_subprocess():
    with pytest.raises(bp.ImageEgressRefused):
        asyncio.run(
            original_method("run_native_cli")(SimpleNamespace(), "Lies plan.png")
        )


def test_sdk_entry_refuses_before_setup_or_query():
    async def run():
        async for _ in original_method("run_completion")(
            SimpleNamespace(), "Lies plan.png", allowed_tools=["WebFetch"]
        ):
            pytest.fail("SDK started")

    with pytest.raises(bp.ImageEgressRefused):
        asyncio.run(run())


def test_erkunder_uses_guarded_read_and_bash(tmp_path):
    from src.erkunder.kind import sdk_options

    options = sdk_options(
        {"ordner": str(tmp_path), "max_turns": 1, "claude_token": "offline"}
    )
    assert {"Read", "Bash"} <= set(options.allowed_tools)
    assert set(options.hooks) >= {"PreToolUse", "PostToolUse"}
    assert options.extra_args["tools"].split(",") == options.allowed_tools


def test_system_prompt_is_checked_before_sdk_setup():
    async def run():
        async for _ in original_method("run_completion")(
            SimpleNamespace(),
            "Text",
            system_prompt=[{"type": "image"}],
            disallowed_tools=["Read", "Bash"],
        ):
            pytest.fail("SDK started")

    with pytest.raises(bp.ImageEgressRefused):
        asyncio.run(run())


def test_sdk_final_options_guard_is_at_dispatch():
    # Integration lint for the single known SDK dispatch; not an egress inventory.
    source = Path(__file__).parents[2] / "src/claude_cli.py"
    code = source.read_text()
    assert (
        "constrain_agent_options(options)\n"
        "                        async for message in query(" in code
    )


@pytest.mark.parametrize(
    "source",
    [
        {"type": "base64", "media_type": "application/pdf", "data": "fake"},
        {"type": "url", "url": "https://offline.invalid/plan.pdf"},
    ],
)
def test_native_document_cannot_hide_raster_images(source):
    with pytest.raises(bp.ImageEgressRefused):
        bp.guard_provider_payload("anthropic", {"type": "document", "source": source})
