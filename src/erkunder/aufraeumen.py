"""Cleanup for a dedicated place UID inside its container PID namespace."""

import asyncio
import os
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


def remove_tree(path: str | Path, *, dir_fd: int | None = None) -> None:
    """Remove a reaped step's tree, including mode-000 dirs, without following links.

    O_PATH can pin an unreadable inode. chmod via its proc fd changes exactly
    that directory (never a symlink target), then all descent stays fd-relative.
    No privilege escalation: the place owns its directories. Call only after
    killing/reaping every step process, so no producer can race removal.
    """
    pinned = os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        if not stat.S_ISDIR(os.fstat(pinned).st_mode):
            os.unlink(path, dir_fd=dir_fd)
            return
        os.chmod(f"/proc/self/fd/{pinned}", 0o700)
        readable = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                           dir_fd=pinned)
        try:
            for name in os.listdir(readable):
                remove_tree(name, dir_fd=readable)
        finally:
            os.close(readable)
        os.rmdir(path, dir_fd=dir_fd)
    finally:
        os.close(pinned)


def clear_owned_tmp(directory: Path = Path("/tmp")) -> None:
    parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in os.listdir(parent):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if info.st_uid == os.getuid():
                remove_tree(name, dir_fd=parent)
    finally:
        os.close(parent)


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
        except ProcessLookupError:
            continue  # procfs reports ESRCH when the enumerated thread has exited.
        except FileNotFoundError as error:
            try:
                task.stat()
            except (FileNotFoundError, ProcessLookupError):
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
