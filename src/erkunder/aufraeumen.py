"""Cleanup for a dedicated place UID inside its container PID namespace."""

import asyncio
import os
import shutil
import signal
import stat
from pathlib import Path


def status(pid: Path) -> dict[str, str]:
    return dict(
        line.split(":", 1) for line in (pid / "status").read_text().splitlines()
    )


async def stop_uid_processes(proc: Path = Path("/proc")) -> None:
    # Server and its launcher ancestors belong to the service, not to a step.
    protected = {os.getpid()}
    parent = os.getppid()
    while parent:
        protected.add(parent)
        parent = int(status(Path("/proc") / str(parent))["PPid"])
    uid = os.getuid()
    for _ in range(100):
        alive = False
        for entry in proc.iterdir():
            if not entry.name.isdecimal() or int(entry.name) in protected:
                continue
            try:
                # Pin the task before checking UID, preventing PID-reuse signals.
                fd = os.pidfd_open(int(entry.name))
                try:
                    info = status(entry)
                    if uid not in map(int, info["Uid"].split()):
                        continue
                    if info["State"].strip().startswith(("Z", "X")):
                        continue
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                    alive = True
                finally:
                    os.close(fd)
            except (FileNotFoundError, ProcessLookupError):
                continue  # Exited during enumeration; never swallow permission errors.
        if not alive:
            return
        await asyncio.sleep(0.01)
    raise RuntimeError("Platz-uid hat nach SIGKILL noch aktive Prozesse")


def clear_owned_tmp(directory: Path = Path("/tmp")) -> None:
    for entry in directory.iterdir():
        info = entry.lstat()
        if info.st_uid != os.getuid():
            continue
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(entry)
        else:
            entry.unlink()
