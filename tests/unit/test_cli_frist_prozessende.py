"""BRLZR P3 (12.10.2026): the CLI really ends at the research deadline.

claude-code-sdk 0.0.25 SubprocessCLITransport.close() sends SIGTERM and then
waits for the process without a timeout — a CLI that ignores SIGTERM kept the
aborted run hanging (Codex BRLZR, measured). src.claude_cli bounds it: own
process group per CLI, SIGTERM, CLI_TERM_GRACE_S, SIGKILL to the group.

Needs the REAL SDK: several test modules replace claude_code_sdk with a
MagicMock in sys.modules; in a session where that happened first, this file
skips loudly — run it on its own (as the BRLZR evidence does).
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import claude_code_sdk
import pytest

if not isinstance(getattr(claude_code_sdk, "__file__", None), str):
    pytest.skip(
        "claude_code_sdk is a test stand-in in this session — run "
        "tests/unit/test_cli_frist_prozessende.py on its own",
        allow_module_level=True,
    )

import src.claude_cli as claude_cli  # noqa: E402

# ── BRLZR P3: the CLI really ends at the deadline, children included ───────

def _lebt(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            zustand = f.read().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return zustand not in ("Z", "X")


@pytest.mark.asyncio
async def test_sigterm_resistente_cli_wird_samt_kindern_beendet(tmp_path, monkeypatch):
    """Real claude-code-sdk transport and query(), no model: a 'claude' that
    ignores SIGTERM and has started a child (which ignores it too). At the
    deadline both must be gone within the grace period — without a guard."""
    import os
    import signal

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

    tr = SubprocessCLITransport(
        prompt="p", options=ClaudeCodeOptions(), cli_path=str(cli)
    )
    waechter_noetig = []

    async def waechter():  # only so a red run cannot hang the suite
        await asyncio.sleep(8)
        if pids.exists():
            for pid in map(int, pids.read_text().split()):
                if _lebt(pid):
                    waechter_noetig.append(pid)
                    os.kill(pid, signal.SIGKILL)

    guard = asyncio.create_task(waechter())
    t0 = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(1.0):
                async for _ in sdk_query(prompt="p", transport=tr):
                    pass
        dauer = time.monotonic() - t0
    finally:
        guard.cancel()
        await asyncio.gather(guard, return_exceptions=True)

    cli_pid, kind_pid = map(int, pids.read_text().split())
    for _ in range(40):
        if not (_lebt(cli_pid) or _lebt(kind_pid)):
            break
        await asyncio.sleep(0.05)
    assert waechter_noetig == []
    assert dauer < 1.0 + 0.5 + getattr(claude_cli, "CLI_KILL_REAP_S", 5.0)
    assert not _lebt(cli_pid), "CLI survived the deadline"
    assert not _lebt(kind_pid), "the CLI's child survived the deadline"
    assert cli_pid not in getattr(claude_cli, "_OWN_SESSION_PIDS", set())


@pytest.mark.asyncio
async def test_normales_ende_toetet_keine_gruppe(tmp_path, monkeypatch):
    """A CLI that ended by itself: close() kills nothing and forgets the pid."""
    from claude_code_sdk import ClaudeCodeOptions
    from claude_code_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o755)
    tr = SubprocessCLITransport(
        prompt="p", options=ClaudeCodeOptions(), cli_path=str(cli)
    )
    await tr.connect()
    pid = tr._process.pid
    assert pid in claude_cli._OWN_SESSION_PIDS
    await tr._process.wait()
    with patch.object(claude_cli, "_kill_own_group") as kill:
        await tr.close()
    kill.assert_not_called()
    assert pid not in claude_cli._OWN_SESSION_PIDS



# ── BRLZR R2: an ended CLI does not take its group's cleanup with it ───────
# Codex BRLZR R2 counter-samples (local-storage/brlzr-review-20261012/r2/),
# taken over as acceptance tests.


@pytest.mark.asyncio
async def test_beendeter_leiter_laesst_kein_kind_zurueck(tmp_path, monkeypatch):
    """The CLI exits by itself, its SIGTERM-resistant child keeps stdout open:
    the SDK waits until the deadline. After it, the child must be gone and
    the group unregistered."""
    import os
    import signal

    from claude_code_sdk import ClaudeCodeOptions
    from claude_code_sdk import query as sdk_query
    from claude_code_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    path = tmp_path / "pids"
    cli = tmp_path / "claude"
    cli.write_text(
        f"#!/bin/sh\ntrap \"\" TERM\nsleep 30 &\necho $$ $! > {path}\nexit 0\n"
    )
    cli.chmod(0o755)
    monkeypatch.setattr(claude_cli, "CLI_TERM_GRACE_S", 0.1)
    tr = SubprocessCLITransport(
        prompt="p", options=ClaudeCodeOptions(), cli_path=str(cli)
    )
    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.3):
                async for _ in sdk_query(prompt="p", transport=tr):
                    pass
        parent, child = map(int, path.read_text().split())
        assert not _lebt(child), "deadline returned but CLI child is still alive"
        assert parent not in claude_cli._OWN_SESSION_PIDS
    finally:
        if path.exists():
            for pid in map(int, path.read_text().split()):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.mark.asyncio
async def test_gruppenkill_laesst_fremden_prozess_leben(tmp_path, monkeypatch):
    import os

    import anyio
    from claude_code_sdk import ClaudeCodeOptions
    from claude_code_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    outsider = await anyio.open_process(["/bin/sleep", "30"])
    cli = tmp_path / "claude"
    cli.write_text('#!/bin/sh\ntrap "" TERM\nwhile :; do sleep 1; done\n')
    cli.chmod(0o755)
    monkeypatch.setattr(claude_cli, "CLI_TERM_GRACE_S", 0.1)
    tr = SubprocessCLITransport(
        prompt="p", options=ClaudeCodeOptions(), cli_path=str(cli)
    )
    try:
        await tr.connect()
        ownpid = tr._process.pid
        assert os.getpgid(ownpid) == ownpid != os.getpgrp()
        await tr.close()
        assert not _lebt(ownpid)
        assert _lebt(outsider.pid)
        with patch.object(os, "killpg") as kill:
            claude_cli._kill_own_group(outsider.pid)
            kill.assert_not_called()
    finally:
        outsider.kill()
        await outsider.wait()
        if tr._process is not None:
            await tr.close()
