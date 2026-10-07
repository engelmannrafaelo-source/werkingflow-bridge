"""Two actual place steps, different reports, same UID and IPC namespace."""

import asyncio
import json
import os
import sys
from pathlib import Path

from src.erkunder.platz import Platz, Schritt, create_app

CHILD = r"""
import ctypes, json, os, subprocess, sys
from pathlib import Path
body = json.load(sys.stdin)
folder = Path(body["ordner"])
libc = ctypes.CDLL(None, use_errno=True)
key = os.getuid() * 100
shm = Path("/dev/shm/b1o-secret")
mq = Path("/dev/mqueue/b1o-secret")
if body["prompt"] == "B":
    assert not shm.exists() and not mq.exists(), "IPC file survived"
    for kind in ("shm", "sem", "msg"):
        assert len(Path("/proc/sysvipc", kind).read_text().splitlines()) == 1, kind
else:
    shm.write_text("report A private data")
    # Owner can remove even IPC objects created without read/write permissions.
    assert libc.shmget(key, 4096, 0o1000 | 0o2000) >= 0
    assert libc.semget(key + 1, 1, 0o1000 | 0o2000) >= 0
    assert libc.msgget(key + 2, 0o1000 | 0o2000) >= 0
    fd = libc.mq_open(b"/b1o-secret", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600, None)
    assert fd >= 0, ctypes.get_errno()
    assert libc.mq_send(fd, b"report A private data", 21, 0) == 0
    assert libc.mq_close(fd) == 0
    assert shm.exists() and mq.exists()
# Actual installed Node runtime and Claude CLI bootstrap, under step restrictions.
env = dict(os.environ, HOME=str(folder / ".home"))
node = subprocess.run(["node", "-e", "const {Worker}=require('node:worker_threads');"
    "const b=new SharedArrayBuffer(8); new Worker('process.exit(0)',{eval:true});"
    "if(b.byteLength!==8)process.exit(1)"], env=env, capture_output=True)
assert node.returncode == 0, node.stderr
cli = subprocess.run(["claude", "--version"], env=env, capture_output=True)
assert cli.returncode == 0, cli.stderr
(folder / "ergebnis.md").write_text("IPC probe " + body["prompt"])
print("{}")
"""


async def main():
    assert os.getpid() == 1 and os.getuid() in (1101, 1102, 1103)
    service = Platz(command=[sys.executable, "-c", CHILD], sample_s=0.01)
    app = create_app(service)
    async with app.router.lifespan_context(app):
        for phase in ("A", "B"):
            report = "ipc-report-" + phase.lower()
            folder = Path("/arbeit") / report / "erkunder-1"
            folder.mkdir(parents=True)
            await service.start(
                Schritt(
                    bericht_id=report,
                    schritt="erkunder-1",
                    ordner=str(folder),
                    prompt=phase,
                    timeout_s=30,
                    max_turns=1,
                    claude_token="synthetic-ipc-token",
                )
            )
            await service.task
            state = service.states[(report, "erkunder-1")]
            print(json.dumps({"uid": os.getuid(), "phase": phase, "state": state}))
            assert state["zustand"] == "fertig", state
            assert not service.cleanup_failed
    print("PASS: shm, SysV shm/msg/sem, POSIX mq; Node threads and Claude CLI")


if __name__ == "__main__":
    asyncio.run(main())
