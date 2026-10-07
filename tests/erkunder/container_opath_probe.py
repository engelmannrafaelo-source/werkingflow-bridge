"""Root sets up production DAC ownership, then PID 1 becomes an unprivileged place."""

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

from src.erkunder.dateien import MAX_FILE_BYTES, read_bytes, read_text
from src.erkunder.platz import Platz, Schritt, create_app

CHILD = '''import json, sys
from pathlib import Path
body = json.load(sys.stdin)
folder = Path(body["ordner"])
(folder / "ergebnis.md").write_text("synthetic result")
(folder / "skripte").mkdir()
(folder / "skripte" / "probe.py").write_text("# synthetic script")
print("{}")
'''


async def main():
    assert os.getpid() == 1 and os.getuid() == 0
    uid = int(sys.argv[1])
    root = Path("/arbeit")
    report = root / "synthetic-report"
    report.mkdir(mode=0o711)
    folder = report / "erkunder-1"
    folder.mkdir(mode=0o700)
    os.chown(folder, uid, 1100)
    for parent in (root, report):
        assert parent.stat().st_uid == 0
        assert stat.S_IMODE(parent.stat().st_mode) == 0o711
    os.setgroups([])
    os.setgid(1100)
    os.setuid(uid)
    assert os.getuid() == uid != 0
    for parent in (root, report):
        try:
            list(parent.iterdir())
        except PermissionError:
            pass
        else:
            raise AssertionError("Place can list root-owned parent")

    service = Platz(command=[sys.executable, "-c", CHILD], sample_s=0.01)
    app = create_app(service)
    async with app.router.lifespan_context(app):
        request = Schritt(
            bericht_id=report.name, schritt=folder.name, ordner=str(folder),
            prompt="synthetic step", timeout_s=10, max_turns=1,
            claude_token="synthetic-child-token",
        )
        await service.start(request)
        await service.task
        state = service.states[(report.name, folder.name)]
        print(json.dumps({"uid": uid, "state": state}), flush=True)
        assert state["zustand"] == "fertig", state
        assert state["fehler"] is None, state
        assert not service.cleanup_failed
        assert read_text(folder / "ergebnis.md") == "synthetic result"
        assert not (folder / ".home").exists()

        link = folder / "parent-link"
        link.symlink_to(folder, target_is_directory=True)
        leaf = folder / "leaf-link"
        leaf.symlink_to(folder / "ergebnis.md")
        oversized = folder / "large"
        with oversized.open("wb") as stream:
            stream.truncate(MAX_FILE_BYTES + 1)
        fifo = folder / "fifo"
        os.mkfifo(fifo)
        for path in (link / "ergebnis.md", leaf, oversized, fifo):
            try:
                read_bytes(path)
            except (OSError, ValueError):
                print(f"rejected: {path.name}", flush=True)
            else:
                raise AssertionError(f"Unsafe file accepted: {path}")
    print("PASS: root-owned 0711 parents, real step, links, size and FIFO", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
