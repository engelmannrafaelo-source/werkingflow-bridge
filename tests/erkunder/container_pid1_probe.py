"""Run inside the built image as PID 1; no real SDK or external service needed."""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from src.erkunder.platz import Platz, Schritt, create_app

CHILD = """import json, os, sys, time
from pathlib import Path
body = json.load(sys.stdin)
p = Path(body["ordner"])
try:
    Path("/proc/1/environ").read_bytes()
except PermissionError:
    pass
else:
    raise AssertionError("PID 1 environment readable from step")
# Inspect every readable process environment from the real step child.
assert "ERKUNDER_INTERNAL_TOKEN" not in os.environ
assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ
for entry in Path("/proc").iterdir():
    if not entry.name.isdecimal():
        continue
    try:
        environment = (entry / "environ").read_bytes()
    except (PermissionError, FileNotFoundError, ProcessLookupError):
        continue
    assert b"synthetic-container-token" not in environment
    assert body["claude_token"].encode() not in environment
if body["prompt"] == "orphan-burst":
    for _ in range(700):
        intermediate = os.fork()
        if intermediate == 0:
            if os.fork() == 0:
                os.setsid()
                os._exit(0)
            os._exit(0)
        _, status = os.waitpid(intermediate, 0)
        assert status == 0, ("intermediate fork failed", status)
    # Still inside this step: allow a drain cycle and check for zombie buildup.
    time.sleep(0.05)
    tasks = [e for e in Path("/proc").iterdir() if e.name.isdecimal()]
    assert len(tasks) == 2, [e.name for e in tasks]
for _ in range(64):
    if os.fork() == 0:
        os.setsid()
        os.close(0); os.close(1); os.close(2)
        time.sleep(60)
        os._exit(0)
(p / "ready").touch()
(p / "ergebnis.md").write_text("synthetic result")
if body["prompt"] in {"timeout", "cancel"}:
    time.sleep(60)
if body["prompt"] == "error":
    sys.exit(7)
print("{}")
"""


async def main():
    assert os.getpid() == 1
    assert signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL
    service = Platz(command=[sys.executable, "-c", CHILD], sample_s=0.01)
    app = create_app(service)
    async with app.router.lifespan_context(app):
        # Verify the boundary with a separate process of the actual place UID.
        probe = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "-c",
            "cat /proc/1/environ >/dev/null",
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await probe.communicate()
        assert probe.returncode != 0 and b"Permission denied" in stderr
        print("same-UID Bash: /proc/1/environ denied", flush=True)
        for step, mode in enumerate(
            ["orphan-burst"] + ["success", "timeout", "error", "cancel"] * 3
        ):
            folder = service.root / f"synthetic-{step}" / "erkunder-1"
            folder.mkdir(parents=True)
            request = Schritt(
                bericht_id=folder.parent.name,
                schritt=folder.name,
                ordner=str(folder),
                prompt=mode,
                timeout_s=10 if mode == "orphan-burst" else 2,
                max_turns=1,
                claude_token="synthetic-child-token",
            )
            await service.start(request)
            if mode == "cancel":
                async with asyncio.timeout(5):
                    while not (folder / "ready").exists():
                        assert not service.task.done()
                        await asyncio.sleep(0.01)
                service.task.cancel()
                try:
                    await service.task
                except asyncio.CancelledError:
                    pass
            else:
                await service.task
            state = service.states[(request.bericht_id, request.schritt)]
            assert not service.cleanup_failed, state
            assert state["zustand"] == (
                "fertig" if mode in {"success", "orphan-burst"} else "abbruch"
            )
            assert (folder / "ready").exists(), state
            assert (
                service.process.returncode
                == {
                    "orphan-burst": 0,
                    "success": 0,
                    "error": 7,
                    "timeout": -9,
                    "cancel": -9,
                }[mode]
            )
            # All 64 setsid descendants have disappeared, not merely stopped.
            tasks = [p.name for p in Path("/proc").iterdir() if p.name.isdecimal()]
            assert tasks == ["1"], tasks
            assert signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL
            print(
                json.dumps({"step": step, "mode": mode, "processes": tasks}), flush=True
            )
        print(
            "PASS: 700 in-step orphans + 832 setsid remnants reaped; "
            "PID limit 512 intact",
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
