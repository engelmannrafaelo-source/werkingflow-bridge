"""Disposable namespace: locked leftovers, unhealthy place, no new report."""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import uvicorn
from fastapi import HTTPException

from src.erkunder import platz
from src.erkunder.gesundheit import PORTS, probe
from src.erkunder.leitstand import Coordinator, Start
from src.erkunder.platz import Platz, Schritt, create_app

CHILD = r"""
import json, os, sys
from pathlib import Path
body = json.load(sys.stdin)
folder = Path(body["ordner"])
for parent in (Path("/tmp"), Path("/dev/shm"), folder / ".home"):
    root = parent / "locked"
    if body["prompt"] == "A":
        nested = root / "nested"
        nested.mkdir(parents=True)
        (nested / "file").write_text("private")
        (nested / "link").symlink_to("/arbeit/keep", target_is_directory=True)
        (nested / "dangling").symlink_to("/missing")
        nested.chmod(0)
        root.chmod(0)
    else:
        assert not root.exists(), str(root)
assert Path("/arbeit/keep/file").read_text() == "untouched"
assert Path("/arbeit/keep").stat().st_mode & 0o777 == 0o755
(folder / "ergebnis.md").write_text("cleanup probe " + body["prompt"])
print("{}")
"""


async def main():
    assert os.getpid() == 1 and os.getuid() in (1101, 1102, 1103)
    keep = Path("/arbeit/keep")
    keep.mkdir(mode=0o755)
    (keep / "file").write_text("untouched")
    service = Platz(command=[sys.executable, "-c", CHILD], sample_s=0.01)
    app = create_app(service)
    try:
        async with app.router.lifespan_context(app):
            for phase in ("A", "B", "FAIL"):
                folder = (
                    Path("/arbeit") / ("cleanup-report-" + phase.lower()) / "erkunder-1"
                )
                folder.mkdir(parents=True)
                if phase == "FAIL":

                    def fail():
                        raise PermissionError("synthetic private text")

                    platz.clear_owned_tmp = fail
                body = Schritt(
                    bericht_id=folder.parent.name,
                    schritt="erkunder-1",
                    ordner=str(folder),
                    prompt=phase,
                    timeout_s=10,
                    max_turns=1,
                    claude_token="synthetic",
                )
                await service.start(body)
                await service.task
                state = service.states[(body.bericht_id, body.schritt)]
                print(json.dumps({"uid": os.getuid(), "phase": phase, "state": state}))
                assert state["zustand"] == ("abbruch" if phase == "FAIL" else "fertig")
                assert service.cleanup_failed == (phase == "FAIL")

            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=PORTS["platz"],
                    lifespan="off",
                    log_level="error",
                    access_log=False,
                )
            )
            serving = asyncio.create_task(server.serve())
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        await asyncio.sleep(0.01)
                os.environ["ERKUNDER_INTERNAL_TOKEN"] = "synthetic-container-token"
                try:
                    await asyncio.to_thread(probe, "platz")
                except RuntimeError as error:
                    assert "HTTP 503: Platz-Aufraeumen fehlgeschlagen" in str(error)
                    print("PASS health red:", error)
                else:
                    raise AssertionError("health green after cleanup failure")
                async with httpx.AsyncClient() as client:
                    response = await client.get(
                        f"http://127.0.0.1:{PORTS['platz']}/__bereitschaft__",
                        headers={"X-Erkunder-Intern": "synthetic-container-token"},
                    )
                    assert response.status_code == 503
                    root = Path("/arbeit/coordinator")
                    root.mkdir()
                    coordinator = Coordinator(
                        root,
                        client=client,
                        places=[f"http://127.0.0.1:{PORTS['platz']}"],
                    )
                    coordinator.ready = True
                    start = Start.model_validate(
                        {
                            "worker": "synthetic",
                            "claude_token": "synthetic",
                            "auftrag": {
                                "schema": "erkunder-auftrag/1",
                                "bericht_id": "new-report-123",
                                "gegenstand": "synthetic",
                                "zweck": "Test",
                                "datenstand": {
                                    "von": "2026-01-01",
                                    "bis": "2026-01-02",
                                    "heute": "2026-01-03",
                                },
                                "auftrag": None,
                                "vorwissen_md": "Test",
                                "vertiefung_md": None,
                                "dateien": [],
                                "korrekturkreis": 1,
                            },
                        }
                    )
                    try:
                        await coordinator.start(start)
                    except HTTPException as error:
                        assert error.status_code == 503
                    else:
                        raise AssertionError("unhealthy place assigned")
                    assert not coordinator.tasks and not coordinator.states
                    assert not (root / "new-report-123").exists()
                    print("PASS no assignment / no report created")
            finally:
                server.should_exit = True
                await serving
    except HTTPException as error:
        # Lifespan must also report the cleanup failure, never heal silently.
        assert service.cleanup_failed and error.status_code == 503
    print("PASS locked /tmp, /dev/shm, .home; symlink targets untouched; fail closed")


if __name__ == "__main__":
    asyncio.run(main())
