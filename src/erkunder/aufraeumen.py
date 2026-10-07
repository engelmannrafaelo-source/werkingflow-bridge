"""Cleanup for a dedicated place UID inside its container PID namespace."""

import asyncio
import os
import shutil
import signal
import stat
import threading
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


async def reap_children() -> None:
    """Reap adopted descendants in PID 1, after the subprocess owner has waited.

    Keep SIGCHLD unchanged: a handler calling waitpid(-1) or SIG_IGN would
    steal the direct child's exit status from asyncio's child watcher.
    Outside the dedicated PID namespace, unrelated children are not ours.
    """
    if os.getpid() != 1:
        return
    for _ in range(100):
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                break
        # SIGKILL/reparenting can still be in flight after the /proc scan.
        await asyncio.sleep(0.01)
    raise RuntimeError("Platz hat nach SIGKILL noch nicht erntbare Kinder")


def require_proc_children(proc: Path = Path("/proc")) -> None:
    """Check the calling task before the place can report readiness."""
    children = proc / "self" / "task" / str(threading.get_native_id()) / "children"
    try:
        children.read_text()
    except OSError as error:
        raise RuntimeError(
            f"Platz nicht bereit: {children} nicht lesbar; "
            "Linux CONFIG_PROC_CHILDREN und zugaengliches procfs erforderlich"
        ) from error


def reap_adopted_children(watched_pid: int, proc: Path = Path("/proc")) -> None:
    """Drain PID 1's adoptees without consuming asyncio's child's status.

    Platz starts exactly one watched subprocess at a time. Its PID is excluded
    even after exit; only specific other direct children are waited for. No
    global wait/SIGCHLD handler races the PidfdChildWatcher. Enumerate every
    server thread because Linux exposes children per task, not per process.
    """
    if os.getpid() != 1:
        return
    for task in (proc / "self" / "task").iterdir():
        try:
            children = (task / "children").read_text().split()
        except FileNotFoundError as error:
            try:
                task.stat()
            except FileNotFoundError:
                continue  # A server thread exited during enumeration.
            raise RuntimeError(
                f"Platz-Ernte fehlgeschlagen: {task / 'children'} fehlt; "
                "Linux CONFIG_PROC_CHILDREN erforderlich"
            ) from error
        for child in children:
            pid = int(child)
            if pid == watched_pid:
                continue
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                continue  # No longer our child; never wait for a replacement.
