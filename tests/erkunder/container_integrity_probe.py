"""A real UID 1101 reviewer modifies the earlier harmonization file."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

from src.erkunder.leitstand import Coordinator, Start


def reviewer_uid():
    os.setgroups([])
    os.setgid(1100)
    os.setuid(1101)


async def main():
    running = {}
    attacked = False

    async def place(request):
        nonlocal attacked
        if request.url.path == "/abbrechen":
            return httpx.Response(200, json={"abgebrochen": True})
        if request.method == "POST":
            body = json.loads(request.content)
            running[body["schritt"]] = body
            return httpx.Response(200, json={"zustand": "laeuft"})
        name = request.url.path.split("/")[-1]
        directory = Path(running[name]["ordner"])
        filename = "pruefung.md" if name.startswith("pruefung") else "ergebnis.md"
        output = directory / filename
        output.write_text("trägt" if name == "pruefung" else name)
        os.chown(output, directory.stat().st_uid, 1100)
        if name == "pruefung":
            target = directory.parent / "harmonisierung/ergebnis.md"
            attack = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; "
                    "Path(sys.argv[1]).write_text('nach Pruefung manipuliert')",
                    str(target),
                ],
                preexec_fn=reviewer_uid,
                capture_output=True,
            )
            assert attack.returncode == 0, attack.stderr
            attacked = True
        return httpx.Response(200, json={"zustand": "fertig", "meta": {"status": "ok"}})

    service = Coordinator(
        client=httpx.AsyncClient(transport=httpx.MockTransport(place))
    )
    await service.startup()
    try:
        order = Start.model_validate(
            {
                "worker": "synthetic",
                "claude_token": "synthetic-token",
                "auftrag": {
                    "schema": "erkunder-auftrag/1",
                    "bericht_id": "integrity-report",
                    "gegenstand": "synthetic",
                    "zweck": "Probe",
                    "auftrag": None,
                    "datenstand": {
                        "von": "2026-01-01",
                        "bis": "2026-02-01",
                        "heute": "2026-10-07",
                    },
                    "dateien": [],
                    "korrekturkreis": 1,
                },
            }
        )
        await service.start(order)
        await service.tasks[order.auftrag.bericht_id]
        state = service.status(order.auftrag.bericht_id)
        assert attacked
        assert state["zustand"] == "abbruch", state
        assert "Integritaet" in state["fehler"], state
        print(
            "PASS: UID 1101 mutation succeeds on disk but fails result integrity", state
        )
    finally:
        await service.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
