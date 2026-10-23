"""Linux conversion supervisor. A receipt means reaped children, not a closed socket.

The lightweight guardian stays outside the converter's process group and adopts
its descendants with PR_SET_CHILD_SUBREAPER. Service death closes the control
pipe; the guardian kills and reaps the converter tree before writing its receipt.
No document content, filename or provider response is written to process logs.
"""
from __future__ import annotations

import asyncio
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".new")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _children() -> list[int]:
    # The guardian has one thread. After the direct child dies, Linux reparents
    # its descendants to this subreaper (including children that used setsid).
    path = Path(f"/proc/self/task/{os.getpid()}/children")
    return [int(pid) for pid in path.read_text().split()]


def _kill_and_reap(process: subprocess.Popen) -> None:
    # Keep the group leader unreaped until the first group kill: its PID cannot
    # be reused in between. Thereafter kill only children still owned by us.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()
    while True:
        children = _children()
        if not children:
            return
        for pid in children:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.waitpid(-1, 0)
        except ChildProcessError:
            return


def guardian(directory: Path) -> None:
    os.umask(0o077)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot establish document subreaper")
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    process = subprocess.Popen(
        [sys.executable, "-m", "src.privacy_service.document_worker", str(directory)],
        start_new_session=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    reason = "completed"
    accepted = directory / "accepted.json"
    deadline = json.loads(accepted.read_text()).get("deadline_at") if accepted.exists() else None
    try:
        while True:
            # waitid WNOWAIT observes exit without releasing the PID. This also
            # lets us clean up grandchildren after a converter crashes/exits.
            exited = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if exited is not None:
                break
            if deadline and datetime.now(timezone.utc) >= datetime.fromisoformat(deadline):
                reason = "deadline_expired"
                break
            readable, _, _ = select.select([sys.stdin.buffer], [], [], 0.25)
            if readable:
                command = os.read(sys.stdin.fileno(), 4096)
                if not command or b"stop" in command:
                    reason = "owner_lost" if not command else "cancelled"
                    break
            if stopping:
                reason = "supervisor_shutdown"
                break
    finally:
        _kill_and_reap(process)
    # Marker is conservative: it is fsynced BEFORE any external Vision send.
    # Killing the local tree cannot acknowledge a provider-side cancellation.
    external = any(directory.glob("provider-pending-*"))
    write_json(directory / "receipt.json", {
        "reason": reason, "local_stopped": True, "reaped": True,
        "converter_exit": process.returncode,
        "provider_outcome": "unknown" if external else "finished_or_not_started",
        "finished_at": time.time(),
    })


async def run(directory: Path, stop: asyncio.Event, *, lock_fd: int | None = None) -> dict:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "src.privacy_service.document_process", str(directory),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        pass_fds=() if lock_fd is None else (lock_fd,),
    )
    wait = asyncio.create_task(process.wait())
    cancel = asyncio.create_task(stop.wait())
    try:
        finished, _ = await asyncio.wait({wait, cancel}, return_when=asyncio.FIRST_COMPLETED)
        if cancel in finished and not wait.done():
            process.stdin.write(b"stop\n")
            await process.stdin.drain()
        await asyncio.shield(wait)
    finally:
        # EOF also covers cancellation of the owner task. Shield the reaper;
        # cancellation of an await never becomes a claimed stop receipt.
        process.stdin.close()
        cancel.cancel()
        await asyncio.gather(cancel, return_exceptions=True)
        await asyncio.shield(wait)
    if process.returncode != 0 or not (directory / "receipt.json").exists():
        raise RuntimeError("document guardian lost; local stop is unconfirmed")
    return json.loads((directory / "receipt.json").read_text())


if __name__ == "__main__":
    guardian(Path(sys.argv[1]))
