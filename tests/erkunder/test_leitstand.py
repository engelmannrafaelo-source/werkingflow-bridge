import asyncio
import hashlib
import json
import stat
import time
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi import HTTPException

from src.erkunder.leitstand import Coordinator, Start, create_app

_buffer = pa.BufferOutputStream()
pq.write_table(pa.table({"sensor_id": ["pump"], "timestamp": [0], "value": [1.0]}), _buffer)
MEASUREMENT_BYTES = _buffer.getvalue().to_pybytes()


def body(ident="bericht-123", files=None, token="secret"):
    return Start.model_validate(
        {
            "worker": "worker2",
            "claude_token": token,
            "auftrag": {
                "schema": "erkunder-auftrag/1",
                "bericht_id": ident,
                "gegenstand": "Anlage",
                "datenstand": {
                    "von": "2026-01-01",
                    "bis": "2026-02-01",
                    "heute": "2026-10-07",
                },
                "auftrag": None,
                "zweck": "Prüfung",
                "vorwissen_md": "Vorwissen",
                "vertiefung_md": None,
                "dateien": files if files is not None else [{
                    "ziel": "messdaten/test.parquet", "url": "https://download.test/data",
                    "sha256": hashlib.sha256(MEASUREMENT_BYTES).hexdigest(), "bytes": len(MEASUREMENT_BYTES),
                }],
                "korrekturkreis": 1,
            },
        }
    )


class Places:
    def __init__(self, failures=None, review="trägt", hold=False):
        self.failures = failures or {}
        self.review = review
        self.hold = hold
        self.calls = []
        self.running = {}
        self.attempts = {}

    async def __call__(self, request):
        if request.url.host == "download.test":
            return httpx.Response(200, content=MEASUREMENT_BYTES)
        if request.url.path == "/abbrechen":
            return httpx.Response(200, json={"abgebrochen": True})
        if request.method == "POST":
            data = json.loads(request.content)
            self.calls.append(data)
            name = data["schritt"]
            self.attempts[name] = self.attempts.get(name, 0) + 1
            self.running[name] = data
            return httpx.Response(200, json={"zustand": "laeuft"})
        name = request.url.path.split("/")[-1]
        if self.hold:
            await asyncio.sleep(0.001)
            return httpx.Response(200, json={"zustand": "laeuft"})
        data = self.running.get(name)
        if not data:
            return httpx.Response(404, json={"detail": "unbekannt"})
        failed = self.attempts[name] <= self.failures.get(name, 0)
        if not failed:
            filename = "pruefung.md" if name.startswith("pruefung") else "ergebnis.md"
            directory = Path(data["ordner"])
            if name.startswith("pruefung"):
                findings = [] if self.review == "trägt" else [self.review]
                content = "```erkunder-pruefung\n" + json.dumps({"befunde": findings}) + "\n```"
                content += "\n```erkunder-zahlenpruefung\n" + json.dumps({
                    "vollstaendig": True, "zahlen": [{"zitat": "[14](zahl:n)", "zahl": "14", "id": "n"}],
                }) + "\n```"
            else:
                (directory / "skripte").mkdir(exist_ok=True)
                (directory / "skripte/rechnung.py").write_text("# synthetic calculation")
                (directory / "skripte/zahlen.json").write_text(json.dumps({"n": {
                    "wert": 14, "einheit": "Starts", "quelle": "messdaten/test.parquet",
                    "kanaele": ["p"], "raster": "1 min", "auswahl": "alle steigenden Flanken",
                }}))
                content = name + " [14](zahl:n)\n```erkunder-nachweis\n" + json.dumps({
                    "kanaele": {"p": "pump"}, "ergebnisdateien": [{
                        "skript": "skripte/rechnung.py", "ergebnis": "skripte/zahlen.json",
                    }],
                }) + "\n```"
                content += "\n```erkunder-pruefumfang\n[]\n```"
            (directory / filename).write_text(content)
        return httpx.Response(
            200,
            json={
                "zustand": "abbruch" if failed else "fertig",
                "meta": {
                    "status": "abbruch" if failed else "ok",
                    "abbruch_grund": "zeit" if failed else None,
                },
            },
        )


async def setup(tmp_path, **kwargs):
    places = Places(**kwargs)
    owners = []
    service = Coordinator(
        tmp_path / "arbeit",
        client=httpx.AsyncClient(transport=httpx.MockTransport(places)),
        chown=lambda *args: owners.append(args),
        poll_s=0,
    )
    await service.startup()
    return service, places, owners


async def finish(service, request=None):
    request = request or body()
    await service.start(request)
    await service.tasks[request.auftrag.bericht_id]
    return service.status(request.auftrag.bericht_id)


async def test_idempotent_and_busy(tmp_path):
    service, places, _ = await setup(tmp_path, hold=True)
    try:
        assert await service.start(body()) == {"angehaengt": False}
        assert await service.start(body(token="new")) == {"angehaengt": True}
        assert service.tokens["bericht-123"] == "new"
        with pytest.raises(HTTPException) as error:
            await service.start(body("bericht-456"))
        assert error.value.status_code == 409
        with pytest.raises(HTTPException) as error:
            service.result("bericht-123")
        assert error.value.status_code == 409
        await asyncio.sleep(0.02)
        assert len(places.calls) == 3
        assert "secret" not in (service.root / "bericht-123/.lauf.json").read_text()
    finally:
        await service.shutdown()


@pytest.mark.parametrize(
    "size,digest", [(3, hashlib.sha256(b"data").hexdigest()), (4, "0" * 64)]
)
async def test_download_integrity(tmp_path, size, digest):
    service, places, _ = await setup(tmp_path)
    try:
        result = await finish(
            service,
            body(
                files=[
                    {
                        "ziel": "messdaten/x.parquet",
                        "url": "https://download.test/file",
                        "sha256": digest,
                        "bytes": size,
                    }
                ]
            ),
        )
        assert result["zustand"] == "abbruch"
        assert result["schritt"] == "daten"
        assert not places.calls
    finally:
        await service.shutdown()


async def test_permissions_and_success(tmp_path):
    service, _, owners = await setup(tmp_path)
    try:
        assert (await finish(service))["zustand"] == "fertig"
        directory = service.root / "bericht-123"
        assert (
            stat.S_IMODE((directory / "eingang/vorwissen.md").stat().st_mode) == 0o644
        )
        assert stat.S_IMODE((directory / "erkunder-1").stat().st_mode) == 0o700
        assert stat.S_IMODE((directory / ".lauf.json").stat().st_mode) == 0o600
        assert {o[1] for o in owners} == {1101, 1102, 1103}
        assert service.result("bericht-123")["gutachten_final"].startswith("harmonisierung [14]")
    finally:
        await service.shutdown()


@pytest.mark.parametrize(
    "failures,expected",
    [
        ({"erkunder-3": 2}, "fertig"),
        ({"erkunder-2": 2, "erkunder-3": 2}, "abbruch"),
        ({"harmonisierung": 1, "pruefung": 1, "erkunder-1": 1}, "fertig"),
        ({"harmonisierung": 2}, "abbruch"),
    ],
)
async def test_retries_and_two_reports(tmp_path, failures, expected):
    service, places, _ = await setup(tmp_path, failures=failures)
    try:
        assert (await finish(service))["zustand"] == expected
        for name in failures:
            assert places.attempts[name] == 2
        if failures == {"erkunder-3": 2}:
            assert service.states["bericht-123"]["erkunder_ausgefallen"] == [
                {"schritt": "erkunder-3", "grund": "zeit"}
            ]
            harmonic = next(c for c in places.calls if c["schritt"] == "harmonisierung")
            assert "Einer der drei Erkunder ist ausgefallen" in harmonic["prompt"]
    finally:
        await service.shutdown()


@pytest.mark.parametrize(
    "review,corrected",
    [
        ("trägt", False),
        ("TRÄGT NICHT", True),
        ("trägt teilweise", True),
        ("traegt nicht", True),
        ("traegt teilweise", True),
    ],
)
async def test_correction_once(tmp_path, review, corrected):
    service, places, _ = await setup(tmp_path, review=review)
    try:
        assert (await finish(service))["zustand"] == "fertig"
        assert len(places.calls) == (7 if corrected else 5)
        result = service.result("bericht-123")
        assert result["gutachten_final"].startswith(
            "harmonisierung-korrektur" if corrected else "harmonisierung"
        )
        assert json.loads(result["pruefung_final"].split("\n")[1])["befunde"] == ([] if review == "trägt" else [review])
    finally:
        await service.shutdown()


async def test_resume_running_places(tmp_path):
    service, places, _ = await setup(tmp_path, hold=True)
    await service.start(body())
    await asyncio.sleep(0.02)
    assert len(places.calls) == 3
    await service.shutdown()
    places.hold = False
    resumed = Coordinator(
        service.root,
        client=httpx.AsyncClient(transport=httpx.MockTransport(places)),
        chown=lambda *args: None,
        poll_s=0,
    )
    await resumed.startup()
    try:
        assert stat.S_IMODE(resumed.directory("bericht-123").stat().st_mode) == 0o700
        await asyncio.sleep(0)
        assert len(places.calls) == 3
        assert await resumed.start(body(token="fresh")) == {"angehaengt": True}
        await resumed.tasks["bericht-123"]
        assert resumed.status("bericht-123")["zustand"] == "fertig"
        assert len(places.calls) == 8
        assert places.calls[-1]["claude_token"] == "fresh"
    finally:
        await resumed.shutdown()


async def test_cleanup_symlink_and_idempotence(tmp_path):
    service, _, _ = await setup(tmp_path)
    try:
        await finish(service)
        assert (await service.cleanup("bericht-123"))["geloescht_bytes"] > 0
        assert (await service.cleanup("bericht-123"))["geloescht_bytes"] == 0
        outside = tmp_path / "outside"
        outside.mkdir()
        (service.root / "bericht-456").symlink_to(outside)
        with pytest.raises(HTTPException):
            await service.cleanup("bericht-456")
        assert outside.exists()
    finally:
        await service.shutdown()


async def test_housekeeper_at_start(tmp_path):
    service, _, _ = await setup(tmp_path)
    await finish(service)
    path = service.root / "bericht-123/.lauf.json"
    state = json.loads(path.read_text())
    state["aktivitaet"] = time.time() - 7 * 3600
    path.write_text(json.dumps(state))
    await service.shutdown()
    resumed = Coordinator(
        service.root,
        client=httpx.AsyncClient(transport=httpx.MockTransport(Places())),
        chown=lambda *args: None,
    )
    await resumed.startup()
    try:
        assert not (resumed.root / "bericht-123").exists()
    finally:
        await resumed.shutdown()


async def test_auth_every_endpoint(tmp_path, monkeypatch):
    service, _, _ = await setup(tmp_path)
    monkeypatch.setenv("ERKUNDER_INTERNAL_TOKEN", "internal")
    app = create_app(service)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            for method, path in [
                ("POST", "/start"),
                ("POST", "/deploy/pruefen"),
                ("DELETE", "/deploy/pruefen"),
                ("GET", "/status/bericht-123"),
                ("GET", "/ergebnis/bericht-123"),
                ("POST", "/aufraeumen/bericht-123"),
                ("GET", "/openapi.json"),
                ("GET", "/docs"),
            ]:
                response = await client.request(method, path, json={})
                assert response.status_code == 403
    finally:
        await service.shutdown()


async def test_http_start_contract(tmp_path, monkeypatch):
    service, _, _ = await setup(tmp_path, hold=True)
    monkeypatch.setenv("ERKUNDER_INTERNAL_TOKEN", "internal")
    app = create_app(service)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
            headers={"X-Erkunder-Intern": "internal"},
        ) as client:
            first = body().model_dump(mode="json", by_alias=True)
            assert (await client.post("/start", json=first)).json() == {
                "angehaengt": False
            }
            first["worker"] = "worker3"
            assert (await client.post("/start", json=first)).json() == {
                "angehaengt": True
            }
            assert service.states["bericht-123"]["worker"] == "worker3"
            first["auftrag"]["bericht_id"] = "bericht-456"
            response = await client.post("/start", json=first)
            assert response.status_code == 409
            assert response.json() == {"belegt": "bericht-123"}
            first["auftrag"]["schema"] = "wrong"
            assert (await client.post("/start", json=first)).status_code == 400
    finally:
        await service.shutdown()


async def test_metadata_matches_seam(tmp_path):
    from src.erkunder.models import Ergebnis

    service, _, _ = await setup(
        tmp_path, failures={"erkunder-3": 2}, review="trägt nicht"
    )
    try:
        status = await finish(service)
        Ergebnis.model_validate(status["meta"])
    finally:
        await service.shutdown()


async def test_lost_place_stopped_before_retry(tmp_path):
    service, places, _ = await setup(tmp_path)
    original = places.__call__
    events = []
    lost = False

    async def transport(request):
        nonlocal lost
        events.append((request.method, request.url.path))
        if (
            request.method == "GET"
            and request.url.path.endswith("/erkunder-1")
            and not lost
        ):
            lost = True
            raise httpx.ConnectError("lost", request=request)
        return await original(request)

    await service.client.aclose()
    service.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    try:
        assert (await finish(service))["zustand"] == "fertig"
        assert places.attempts["erkunder-1"] == 2
        lost_index = events.index(("GET", "/schritt/bericht-123/erkunder-1"))
        assert events[lost_index + 1] == ("POST", "/abbrechen")
        assert not service.states["bericht-123"]["unsichere_plaetze"]
    finally:
        await service.shutdown()


async def test_reaper_deletes_report_after_unreachable_place(tmp_path, caplog):
    service, _, _ = await setup(tmp_path)
    try:
        await finish(service)
        state = service.states["bericht-123"]
        state["aktivitaet"] = time.time() - 7 * 3600
        state["unsichere_plaetze"] = [0]
        calls = 0

        async def transport(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(200, json={"abgebrochen": True})

        await service.client.aclose()
        service.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        await service.reap()
        assert "bericht-123" not in service.states
        assert calls == 1
        assert "abbrechen=fehlgeschlagen fehler=ConnectError" in caplog.text
    finally:
        await service.shutdown()


async def test_signed_download_url_not_logged(tmp_path, caplog):
    import logging

    caplog.set_level(logging.INFO)
    service, _, _ = await setup(tmp_path)
    try:
        result = await finish(
            service,
            body(
                files=[
                    {
                        "ziel": "messdaten/x.parquet",
                        "url": "https://download.test/file?signature=private-signature",
                        "sha256": hashlib.sha256(MEASUREMENT_BYTES).hexdigest(),
                        "bytes": len(MEASUREMENT_BYTES),
                    }
                ]
            ),
        )
        assert result["zustand"] == "fertig"
        assert "private-signature" not in caplog.text
        assert "download.test" not in caplog.text
    finally:
        await service.shutdown()


@pytest.mark.parametrize("kind", ["symlink", "fifo", "large"])
async def test_result_rejects_untrusted_result_files(tmp_path, kind):
    import os

    from src.erkunder.dateien import MAX_FILE_BYTES
    from src.erkunder.leitstand import StepFailed

    service, _, _ = await setup(tmp_path)
    try:
        await finish(service)
        path = service.directory("bericht-123") / "erkunder-1" / "ergebnis.md"
        path.unlink(missing_ok=True)
        outside = tmp_path / "other-report"
        outside.write_text("private")
        if kind == "symlink":
            path.symlink_to(outside)
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            with path.open("wb") as stream:
                stream.truncate(MAX_FILE_BYTES + 1)
        with pytest.raises((StepFailed, HTTPException)):
            service.result("bericht-123")
        assert outside.read_text() == "private"
    finally:
        await service.shutdown()


@pytest.mark.parametrize("kind", ["directory", "fifo"])
async def test_result_skips_untrusted_script_files(tmp_path, kind, caplog):
    import os

    service, _, _ = await setup(tmp_path)
    try:
        await finish(service)
        path = service.directory("bericht-123") / "erkunder-1" / "skripte" / "x.py"
        path.parent.mkdir(exist_ok=True)
        if kind == "directory":
            path.mkdir()
        else:
            os.mkfifo(path)

        result = service.result("bericht-123")

        assert result["skripte_uebersprungen"] == ["skripte/x.py"]
        assert "skript=uebersprungen fehler=" in caplog.text
    finally:
        await service.shutdown()


async def test_result_reports_scripts_beyond_budget_instead_of_empty_string(tmp_path):
    """Kein stilles Kappen: ist das 200-KB-Budget aufgebraucht, kommt die
    naechste Datei NICHT als leerer String, sondern gemeldet."""
    service, _, _ = await setup(tmp_path)
    try:
        await finish(service)
        folder = service.directory("bericht-123") / "erkunder-1" / "skripte"
        folder.mkdir(exist_ok=True)
        (folder / "a.py").write_text("x" * (200 * 1024))
        (folder / "b.py").write_text("print(1)")

        result = service.result("bericht-123")

        assert "erkunder-1" in result["skripte_gekuerzt"]
        assert "skripte/b.py" in result["skripte_uebersprungen"]
        assert "skripte/b.py" not in result["skripte"]["erkunder-1"]
        assert "" not in result["skripte"]["erkunder-1"].values()
    finally:
        await service.shutdown()


@pytest.mark.parametrize("failure", ["503", "unreachable", "timeout"])
async def test_cleanup_deletes_report_after_place_abort_failure(
    tmp_path, failure, caplog
):
    service, places, _ = await setup(tmp_path)
    try:
        await finish(service)
        service.states["bericht-123"]["unsichere_plaetze"] = [0]

        async def abort_failure(request):
            if request.url.path == "/abbrechen":
                if failure == "503":
                    return httpx.Response(503, json={"detail": "cleanup failed"})
                if failure == "unreachable":
                    raise httpx.ConnectError("offline", request=request)
                raise httpx.ReadTimeout("timed out", request=request)
            return await places(request)

        await service.client.aclose()
        service.client = httpx.AsyncClient(transport=httpx.MockTransport(abort_failure))
        result = await service.cleanup("bericht-123")

        assert result["bericht_id"] == "bericht-123"
        assert not service.directory("bericht-123").exists()
        expected = {
            "503": "HTTPStatusError",
            "unreachable": "ConnectError",
            "timeout": "ReadTimeout",
        }[failure]
        assert result["platz_abbruch_fehler"] == [{"platz": 0, "fehler": expected}]
        assert f"abbrechen=fehlgeschlagen fehler={expected}" in caplog.text
    finally:
        await service.shutdown()


async def test_size_failure_does_not_prevent_deletion(tmp_path, monkeypatch, caplog):
    service, _, _ = await setup(tmp_path)
    try:
        await finish(service)

        def fail(*args):
            raise OSError("private contents")

        monkeypatch.setattr(Path, "rglob", fail)
        result = await service.cleanup("bericht-123")
        assert result["geloescht_bytes"] is None
        assert not service.directory("bericht-123").exists()
        assert "groesse=unbekannt fehler=OSError" in caplog.text
        assert "private contents" not in caplog.text
    finally:
        await service.shutdown()


async def test_reaper_continues_after_report_error(tmp_path, monkeypatch, caplog):
    service, _, _ = await setup(tmp_path)
    try:
        for ident in ("bericht-123", "bericht-456"):
            await finish(service, body(ident))
            service.states[ident]["aktivitaet"] = time.time() - 7 * 3600
        cleanup = service.cleanup

        async def fail_first(ident):
            if ident == "bericht-123":
                raise RuntimeError("private contents")
            return await cleanup(ident)

        monkeypatch.setattr(service, "cleanup", fail_first)
        await service.reap()
        assert "bericht-123" in service.states
        assert "bericht-456" not in service.states
        assert "RuntimeError" in caplog.text
        assert "private contents" not in caplog.text
    finally:
        await service.shutdown()


async def test_housekeeper_survives_round_error(tmp_path, monkeypatch, caplog):
    service = Coordinator(tmp_path)
    sleep = asyncio.sleep
    rounds = 0

    async def tick(_):
        await sleep(0)

    async def reap():
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            raise OSError("private contents")
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", tick)
    monkeypatch.setattr(service, "reap", reap)
    try:
        with pytest.raises(asyncio.CancelledError):
            await service.housekeeping()
        assert rounds == 2
        assert "hausmeister_runde fehler=OSError" in caplog.text
        assert "private contents" not in caplog.text
    finally:
        await service.shutdown()


@pytest.mark.parametrize("at_startup", [True, False])
async def test_orphans_removed_by_age(tmp_path, at_startup):
    import os

    service = Coordinator(
        tmp_path / "arbeit",
        client=httpx.AsyncClient(transport=httpx.MockTransport(Places())),
    )
    service.root.mkdir()
    old = service.root / "bericht-old"
    fresh = service.root / "bericht-new"
    for directory in (old, fresh):
        directory.mkdir()
        (directory / "eingang").mkdir()
        (directory / "eingang/private").write_text("synthetic customer data")
    os.utime(old, (time.time() - 7 * 3600,) * 2)
    try:
        if at_startup:
            await service.startup()
        else:
            await service.reap()
        assert not old.exists()
        assert fresh.exists()
    finally:
        await service.shutdown()


@pytest.mark.parametrize("token", [None, ""])
async def test_missing_internal_token_refuses_start(tmp_path, monkeypatch, token):
    if token is None:
        monkeypatch.delenv("ERKUNDER_INTERNAL_TOKEN", raising=False)
    else:
        monkeypatch.setenv("ERKUNDER_INTERNAL_TOKEN", token)
    service = Coordinator(tmp_path / "arbeit")
    app = create_app(service)
    try:
        with pytest.raises(RuntimeError, match="ERKUNDER_INTERNAL_TOKEN fehlt"):
            async with app.router.lifespan_context(app):
                pytest.fail("started without token")
        assert not service.root.exists()
    finally:
        await service.shutdown()


@pytest.mark.parametrize("failures", [{}, {"erkunder-1": 2, "erkunder-2": 2}])
async def test_finished_report_sealed_before_same_slots_reused(tmp_path, failures):
    service, places, _ = await setup(tmp_path, failures=failures)
    try:
        await finish(service)
        old = service.directory("bericht-123")
        assert stat.S_IMODE(old.stat().st_mode) == 0o700
        places.failures = {}
        assert (await finish(service, body("bericht-456")))["zustand"] == "fertig"
        assert stat.S_IMODE(old.stat().st_mode) == 0o700
        assert service.result("bericht-456")["gutachten_final"].startswith("harmonisierung [14]")
    finally:
        await service.shutdown()


async def test_cancel_before_task_started_seals_report(tmp_path):
    service, _, _ = await setup(tmp_path)
    await service.start(body())
    await service.shutdown()
    assert stat.S_IMODE(service.directory("bericht-123").stat().st_mode) == 0o700


async def test_revocation_failure_blocks_new_report(tmp_path, monkeypatch):
    service, places, _ = await setup(tmp_path)
    try:
        await finish(service)
        original = Path.chmod

        def fail(path, mode, *args, **kwargs):
            if path == service.directory("bericht-123"):
                raise PermissionError("synthetic")
            return original(path, mode, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "chmod", fail)
            with pytest.raises(PermissionError):
                await service.start(body("bericht-456"))
        assert not service.directory("bericht-456").exists()
        assert len(places.calls) == 5
    finally:
        await service.shutdown()


@pytest.mark.parametrize("failure", ["503", "offline"])
async def test_abort_failure_blocks_new_report_even_after_old_state_deleted(
    tmp_path, failure
):
    service, places, _ = await setup(tmp_path)
    try:
        await finish(service)
        await service.cleanup("bericht-123")

        async def unavailable(request):
            if failure == "offline":
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(503)

        await service.client.aclose()
        service.client = httpx.AsyncClient(transport=httpx.MockTransport(unavailable))
        with pytest.raises(HTTPException) as error:
            await service.start(body("bericht-456"))
        assert error.value.status_code == 503
        assert not service.directory("bericht-456").exists()
        assert len(places.calls) == 5
    finally:
        await service.shutdown()


async def test_startup_seals_legacy_and_orphan_before_abort_failure(tmp_path):
    root = tmp_path / "arbeit"
    root.mkdir()
    for ident in ("bericht-legacy", "bericht-orphan"):
        path = root / ident
        path.mkdir(mode=0o711)
        (path / "data").write_text("synthetic")

    async def offline(request):
        # Revocation must precede the very first network await.
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o700 for p in root.iterdir())
        return httpx.Response(503)

    service = Coordinator(
        root, client=httpx.AsyncClient(transport=httpx.MockTransport(offline)),
        startup_wait_s=0,
    )
    try:
        await service.startup()
        assert not service.ready
        with pytest.raises(HTTPException) as error:
            await service.start(body())
        assert error.value.status_code == 503
        assert not service.tasks
    finally:
        await service.shutdown()


async def test_terminal_revocation_failure_is_visible_and_blocks_reuse(
    tmp_path, monkeypatch
):
    service, _, _ = await setup(tmp_path)
    try:
        await service.start(body())
        with monkeypatch.context() as patch:

            def fail(_):
                raise PermissionError("synthetic private error")

            patch.setattr(service, "seal", fail)
            with pytest.raises(PermissionError):
                await service.tasks["bericht-123"]
            status = service.status("bericht-123")
            assert status["zustand"] == "abbruch"
            assert status["fehler"] == "Bericht-Trennung: Rechteentzug fehlgeschlagen"
            with pytest.raises(PermissionError):
                await service.start(body("bericht-456"))
            assert not service.directory("bericht-456").exists()
    finally:
        await service.shutdown()


async def test_cancelled_running_task_revokes_report(tmp_path):
    service, places, _ = await setup(tmp_path, hold=True)
    try:
        await service.start(body())
        while len(places.calls) < 3:
            await asyncio.sleep(0)
        service.tasks["bericht-123"].cancel()
        with pytest.raises(asyncio.CancelledError):
            await service.tasks["bericht-123"]
        assert stat.S_IMODE(service.directory("bericht-123").stat().st_mode) == 0o700
    finally:
        await service.shutdown()


@pytest.mark.parametrize("review", ["trägt", "trägt teilweise"])
async def test_result_detects_mutation_after_review_and_restart(tmp_path, review):
    from src.erkunder.leitstand import StepFailed

    service, _, _ = await setup(tmp_path, review=review)
    assert (await finish(service))["zustand"] == "fertig"
    name = "harmonisierung-korrektur" if "teilweise" in review else "harmonisierung"
    path = service.directory("bericht-123") / name / "ergebnis.md"
    path.write_text("nach Pruefung manipuliert")
    with pytest.raises(StepFailed, match="Integritaet"):
        service.result("bericht-123")
    await service.shutdown()
    resumed = Coordinator(
        service.root,
        client=httpx.AsyncClient(transport=httpx.MockTransport(Places())),
        chown=lambda *args: None,
    )
    await resumed.startup()
    try:
        with pytest.raises(StepFailed, match="Integritaet"):
            resumed.result("bericht-123")
    finally:
        await resumed.shutdown()


async def test_reviewer_mutation_fails_before_completion(tmp_path):
    service, places, _ = await setup(tmp_path)

    async def malicious(request):
        response = await places(request)
        if request.method == "GET" and request.url.path.endswith("/pruefung"):
            path = service.directory("bericht-123") / "harmonisierung/ergebnis.md"
            path.write_text("manipuliert")
        return response

    await service.client.aclose()
    service.client = httpx.AsyncClient(transport=httpx.MockTransport(malicious))
    try:
        state = await finish(service)
        assert state["zustand"] == "abbruch"
        assert "Integritaet" in state["fehler"]
    finally:
        await service.shutdown()


async def test_compose_order_waits_for_places_without_restarting(tmp_path, caplog):
    root = tmp_path / "arbeit"
    (root / "old-report").mkdir(parents=True)
    available = asyncio.Event()
    calls = []

    async def place(request):
        calls.append(request.url.host)
        assert stat.S_IMODE((root / "old-report").stat().st_mode) == 0o700
        return httpx.Response(200 if available.is_set() else 503)

    service = Coordinator(
        root, client=httpx.AsyncClient(transport=httpx.MockTransport(place)),
        startup_wait_s=5,
    )
    task = asyncio.create_task(service.startup())
    await asyncio.sleep(0.02)
    assert not task.done() and not service.ready
    with pytest.raises(HTTPException) as error:
        await service.start(body())
    assert error.value.status_code == 503
    available.set()
    await task
    assert service.ready and len(set(calls)) == 3
    assert "wartet auf Plaetze" in caplog.text
    await service.shutdown()


async def test_reattach_deadline_releases_allocation_but_retains_report(tmp_path):
    service, _, _ = await setup(tmp_path, hold=True)
    await service.start(body())
    await asyncio.sleep(0.01)
    await service.shutdown()
    resumed = Coordinator(
        service.root, client=httpx.AsyncClient(transport=httpx.MockTransport(Places())),
        chown=lambda *args: None, poll_s=0, reattach_wait_s=0.01,
    )
    await resumed.startup()
    try:
        await resumed.tasks["bericht-123"]
        state = resumed.status("bericht-123")
        assert state["zustand"] == "abbruch"
        assert "Zeitgrenze" in state["fehler"]
        assert resumed.directory("bericht-123").exists()
        assert (await finish(resumed, body("bericht-456")))["zustand"] == "fertig"
    finally:
        await resumed.shutdown()


async def test_startup_http_timeout_stays_alive_and_cleanup_recovers(
    tmp_path, monkeypatch,
):
    root = tmp_path / "arbeit"
    (root / "old-report").mkdir(parents=True)
    (root / "another-report").mkdir()
    available = False

    async def place(request):
        if not available:
            await asyncio.sleep(10)
        return httpx.Response(200)

    service = Coordinator(
        root, client=httpx.AsyncClient(transport=httpx.MockTransport(place)),
        startup_wait_s=0.02,
    )
    monkeypatch.setenv("ERKUNDER_INTERNAL_TOKEN", "synthetic")
    app = create_app(service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test",
            headers={"X-Erkunder-Intern": "synthetic"},
        ) as api:
            assert (await api.get("/bereitschaft")).status_code == 503
            assert (await api.get("/__bereitschaft__")).status_code == 503
            response = await api.post(
                "/start", json=body().model_dump(mode="json", by_alias=True)
            )
            assert response.status_code == 503
            available = True
            response = await api.post("/aufraeumen/old-report")
            assert response.status_code == 200
            assert (await api.get("/bereitschaft")).json() == {"bereit": True}
            assert (await api.get("/__bereitschaft__")).status_code == 404
            assert not (root / "old-report").exists()


async def test_result_http_integrity_error_is_explicit(tmp_path, monkeypatch):
    service, _, _ = await setup(tmp_path)
    monkeypatch.setenv("ERKUNDER_INTERNAL_TOKEN", "synthetic")
    try:
        assert (await finish(service))["zustand"] == "fertig"
        path = service.directory("bericht-123") / "harmonisierung/ergebnis.md"
        path.write_text("changed")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(create_app(service)), base_url="http://test",
            headers={"X-Erkunder-Intern": "synthetic"},
        ) as api:
            response = await api.get("/ergebnis/bericht-123")
            assert response.status_code == 409
            assert "Integritaet" in response.json()["detail"]
    finally:
        await service.shutdown()


async def test_deploy_gate_waits_then_atomically_blocks_new_reports(tmp_path):
    service, places, _ = await setup(tmp_path, hold=True)
    try:
        await service.start(body())
        assert await service.prepare_deploy() == {
            "bereit": False, "berichte": ["bericht-123"]
        }
        assert not service.deploy_pending
        # Reattachment remains available to the running report.
        assert await service.start(body(token="fresh")) == {"angehaengt": True}
        places.hold = False
        await service.tasks["bericht-123"]
        assert await service.prepare_deploy() == {"bereit": True, "berichte": []}
        with pytest.raises(HTTPException) as error:
            await service.start(body("bericht-456"))
        assert error.value.status_code == 503
        await service.cancel_deploy()
        assert (await finish(service, body("bericht-456")))["zustand"] == "fertig"
    finally:
        await service.shutdown()
