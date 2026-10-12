"""CHATFWD: when the relay supervisor cancels a run, the CLI process (and
its children) really ends — the supervisor's task.cancel() reaches the same
bounded close as the research deadline (BRLZR P3, test_cli_frist_prozessende).

Needs the REAL SDK, like test_cli_frist_prozessende.py — run it on its own;
in a session where another module installed the SDK stand-in it skips loudly.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time

import claude_code_sdk
import pytest

if not isinstance(getattr(claude_code_sdk, "__file__", None), str):
    pytest.skip(
        "claude_code_sdk is a test stand-in in this session — run "
        "tests/unit/test_chat_weiterleitung_prozessende.py on its own",
        allow_module_level=True,
    )

import src.chat_weiterleitung as cw  # noqa: E402
import src.claude_cli as claude_cli  # noqa: E402


def _lebt(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            zustand = f.read().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return zustand not in ("Z", "X")


@pytest.mark.asyncio
@pytest.mark.parametrize("anlass", ["frist", "aufrufer_weg"])
async def test_aufsicht_beendet_cli_samt_kindern(tmp_path, monkeypatch, anlass):
    from claude_code_sdk import ClaudeCodeOptions
    from claude_code_sdk import query as sdk_query
    from claude_code_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    pids = tmp_path / "pids"
    cli = tmp_path / "claude"  # the name routes it through the spawn patch
    cli.write_text(
        "#!/bin/sh\n"
        "trap '' TERM\n"
        "sleep 60 &\n"
        f"echo $$ $! > {pids}\n"
        "while :; do sleep 1; done\n"
    )
    cli.chmod(0o755)
    monkeypatch.setattr(claude_cli, "CLI_TERM_GRACE_S", 0.5, raising=False)
    monkeypatch.setattr(cw, "CHAT_RELAY_MARGIN_S", 0.0)
    monkeypatch.setattr(cw, "AUFSICHT_TAKT_S", 0.05)

    weg = {"jetzt": False}

    async def caller_gone():
        return weg["jetzt"]

    monkeypatch.setattr("src.activity.delivery.caller_gone", caller_gone)

    tr = SubprocessCLITransport(prompt="p", options=ClaudeCodeOptions(), cli_path=str(cli))

    async def lauf():
        async for _ in sdk_query(prompt="p", transport=tr):
            pass

    async def waechter():  # only so a red run cannot hang the suite
        await asyncio.sleep(10)
        if pids.exists():
            for pid in map(int, pids.read_text().split()):
                if _lebt(pid):
                    os.kill(pid, signal.SIGKILL)

    guard = asyncio.create_task(waechter())
    try:
        if anlass == "frist":
            with pytest.raises(cw.FristAbgelaufen):
                await cw.unter_aufsicht(lauf(), time.time() + 1.0)
        else:
            async def geht():
                await asyncio.sleep(1.0)
                weg["jetzt"] = True

            asyncio.ensure_future(geht())
            with pytest.raises(cw.AufruferWeg):
                await cw.unter_aufsicht(lauf(), time.time() + 3000)
    finally:
        guard.cancel()
        await asyncio.gather(guard, return_exceptions=True)

    cli_pid, kind_pid = map(int, pids.read_text().split())
    for _ in range(40):
        if not (_lebt(cli_pid) or _lebt(kind_pid)):
            break
        await asyncio.sleep(0.05)
    assert not _lebt(cli_pid), "CLI survived the supervisor's cancel"
    assert not _lebt(kind_pid), "the CLI's child survived the supervisor's cancel"
