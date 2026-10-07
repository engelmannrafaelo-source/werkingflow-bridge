"""Synthetic report lifecycle probe; run as root in a disposable container."""

import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path

import httpx
from fastapi import HTTPException

from src.erkunder.leitstand import Coordinator, Start

ROOT = Path("/arbeit")
UID = int(sys.argv[1])


def order(ident):
    return Start.model_validate(
        {
            "worker": "synthetic",
            "claude_token": "synthetic-token",
            "auftrag": {
                "schema": "erkunder-auftrag/1",
                "bericht_id": ident,
                "gegenstand": "synthetic",
                "datenstand": {
                    "von": "2026-01-01",
                    "bis": "2026-01-02",
                    "heute": "2026-01-03",
                },
                "auftrag": None,
                "zweck": "test",
                "vorwissen_md": "synthetic customer A",
                "vertiefung_md": None,
                "dateien": [],
                "korrekturkreis": 1,
            },
        }
    )


def drop_uid():
    os.setgroups([])
    os.setgid(1100)
    os.setuid(UID)


def readable(path, expected):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())",
            str(path),
        ],
        preexec_fn=drop_uid,
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) == expected, (UID, path, result.stderr)
    if not expected:
        assert b"PermissionError" in result.stderr, result.stderr


class Places:
    def __init__(self, fail=False, hold=False):
        self.fail, self.hold = fail, hold
        self.running = {}
        self.held = None
        self.offline = False

    async def __call__(self, request):
        if request.url.path == "/abbrechen":
            if self.offline:
                return httpx.Response(503)
            if self.held is not None:
                os.kill(self.held.pid, signal.SIGKILL)
                self.held.wait(timeout=5)
                self.held = None
            return httpx.Response(200, json={"abgebrochen": True})
        if request.method == "POST":
            data = json.loads(request.content)
            self.running[data["schritt"]] = data
            return httpx.Response(200, json={"zustand": "laeuft"})
        if self.hold:
            return httpx.Response(200, json={"zustand": "laeuft"})
        data = self.running[request.url.path.split("/")[-1]]
        name = data["schritt"]
        path = Path(data["ordner"]) / (
            "pruefung.md" if name.startswith("pruefung") else "ergebnis.md"
        )
        path.write_text(
            "trägt" if name.startswith("pruefung") else "synthetic result A"
        )
        os.chown(
            path,
            1101 + int(name[-1]) - 1 if name.startswith("erkunder") else 1101,
            1100,
        )
        return httpx.Response(
            200,
            json={
                "zustand": "abbruch" if self.fail else "fertig",
                "meta": {
                    "status": "abbruch" if self.fail else "ok",
                    "abbruch_grund": "synthetic",
                },
            },
        )


def coordinator(places):
    return Coordinator(
        ROOT,
        client=httpx.AsyncClient(transport=httpx.MockTransport(places)),
        poll_s=0.001,
    )


def denied(ident):
    directory = ROOT / ident
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    readable(directory / "eingang/vorwissen.md", False)
    readable(directory / f"erkunder-{UID - 1100}/ergebnis.md", False)
    # Knowing the report ID, nested path and UID-owned contents grants no access.


async def crash():
    places = Places(hold=True)
    service = coordinator(places)
    await service.startup()
    await service.start(order("bericht-crash"))
    while len(places.running) != 3:
        await asyncio.sleep(0.001)
    folder = ROOT / "bericht-crash" / f"erkunder-{UID - 1100}"
    (folder / "ergebnis.md").write_text("synthetic unfinished A")
    os._exit(0)  # No shutdown, no finally: abrupt Leitstand death.


async def main():
    places = Places()
    service = coordinator(places)
    await service.startup()
    for ident, fail in [("bericht-success", False), ("bericht-failure", True)]:
        places.fail = fail
        await service.start(order(ident))
        await service.tasks[ident]
        denied(ident)
    places.fail = False
    assert service.result("bericht-success")["gutachten_final"] == "synthetic result A"
    places.hold = True
    places.running.clear()
    await service.start(order("bericht-bbbbb"))
    while len(places.running) != 3:
        await asyncio.sleep(0.001)
    readable(ROOT / "bericht-bbbbb/eingang/vorwissen.md", True)
    denied("bericht-success")
    denied("bericht-failure")
    places.hold = False
    await service.tasks["bericht-bbbbb"]
    denied("bericht-success")
    denied("bericht-failure")
    places.hold = True
    await service.start(order("bericht-cancel"))
    await asyncio.sleep(0.03)
    readable(ROOT / "bericht-cancel/eingang/vorwissen.md", True)
    await service.cleanup("bericht-cancel")
    assert not (ROOT / "bericht-cancel").exists()
    places.offline = True
    try:
        await service.start(order("bericht-blocked"))
    except HTTPException as error:
        assert error.status_code == 503
    else:
        raise AssertionError("unclean place reused")
    assert not (ROOT / "bericht-blocked").exists()
    places.offline = False
    await service.shutdown()

    subprocess.run([sys.executable, __file__, str(UID), "crash"], check=True)
    readable(ROOT / "bericht-crash/eingang/vorwissen.md", True)
    # An old place may retain cwd/open descriptors across chmod. The abort
    # acknowledgement must kill that process before resume or reassignment.
    places = Places()
    places.held = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        cwd=ROOT / "bericht-crash" / f"erkunder-{UID - 1100}",
        preexec_fn=drop_uid,
    )
    held = places.held
    resumed = coordinator(places)
    await resumed.startup()
    denied("bericht-crash")
    assert held.poll() == -signal.SIGKILL
    await asyncio.sleep(0.01)
    assert not places.running  # No fresh token: still sealed, no new step.
    await resumed.start(order("bericht-crash"))
    await resumed.tasks["bericht-crash"]
    denied("bericht-crash")
    assert resumed.result("bericht-crash")["gutachten_final"] == "synthetic result A"
    await resumed.start(order("bericht-after"))
    await resumed.tasks["bericht-after"]
    denied("bericht-crash")
    await resumed.shutdown()
    print(
        f"PASS uid={UID}: success, failure, cancellation, same-place reuse, "
        "abort failure, abrupt restart, retained cwd, root results, fresh-token resume"
    )


asyncio.run(crash() if len(sys.argv) > 2 else main())
