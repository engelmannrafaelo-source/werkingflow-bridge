import asyncio
import hashlib
import json
import stat
import time
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from src.erkunder.leitstand import Coordinator, Start, create_app


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
                "dateien": files or [],
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
            return httpx.Response(200, content=b"data")
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
            Path(data["ordner"], filename).write_text(
                self.review if name.startswith("pruefung") else name
            )
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
        assert service.result("bericht-123")["gutachten_final"] == "harmonisierung"
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
        assert result["gutachten_final"] == (
            "harmonisierung-korrektur" if corrected else "harmonisierung"
        )
        assert result["pruefung_final"] == review
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
        assert await resumed.start(body(token="fresh")) == {"angehaengt": True}
        await resumed.tasks["bericht-123"]
        assert resumed.status("bericht-123")["zustand"] == "fertig"
        assert len(places.calls) == 5
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


async def test_reaper_retries_unreachable_place(tmp_path):
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
        assert "bericht-123" in service.states
        await service.reap()
        assert "bericht-123" not in service.states
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
                        "sha256": hashlib.sha256(b"data").hexdigest(),
                        "bytes": 4,
                    }
                ]
            ),
        )
        assert result["zustand"] == "fertig"
        assert "private-signature" not in caplog.text
        assert "download.test" not in caplog.text
    finally:
        await service.shutdown()
