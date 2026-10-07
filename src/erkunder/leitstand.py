"""Persistent orchestration for isolated Erkunder places (no model execution here)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from .models import Auftrag
from .prompts import (
    PROMPT_VERSION,
    erkunder_prompt,
    harmonisierung_prompt,
    korrektur_prompt,
    pruefung_prompt,
)

LOG = logging.getLogger(__name__)
ID = re.compile(r"^[a-z0-9-]{8,80}$")


class Start(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str | None = None
    worker: str
    claude_token: str
    auftrag: Auftrag


class StepFailed(Exception):
    def __init__(self, step: str, reason: str):
        self.step, self.reason = step, reason
        super().__init__(f"{step}: {reason}")


class Coordinator:
    def __init__(
        self,
        root: Path = Path("/arbeit"),
        *,
        client: httpx.AsyncClient | None = None,
        places: list[str] | None = None,
        chown: Callable[[Path, int, int], Any] = os.chown,
        poll_s: float = 2,
        retention_hours: float = 6,
    ):
        self.root = root
        self.client = client or httpx.AsyncClient(timeout=60, follow_redirects=False)
        self.places = places or [f"http://erkunder-platz-{i}:8200" for i in range(1, 4)]
        self.chown = chown
        self.poll_s = poll_s
        self.retention = retention_hours * 3600
        self.states: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, str] = {}
        self.token_ready: dict[str, asyncio.Event] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.lock = asyncio.Lock()
        self.housekeeper: asyncio.Task[None] | None = None

    def directory(self, ident: str) -> Path:
        if not ID.fullmatch(ident):
            raise HTTPException(400, "Ungültige bericht_id")
        path = self.root / ident
        if path.resolve().parent != self.root.resolve() or path.is_symlink():
            raise HTTPException(400, "Pfad liegt außerhalb /arbeit")
        return path

    def save(self, ident: str) -> None:
        state = self.states[ident]
        state["aktivitaet"] = time.time()
        path = self.directory(ident) / ".lauf.json"
        tmp = path.with_suffix(".new")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)

    async def startup(self) -> None:
        # This lifecycle runs only in the standalone Leitstand, never in a worker.
        # HTTPX INFO and HTTPCore DEBUG expose signed download URLs.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o711)
        self.root.chmod(0o711)
        for path in self.root.iterdir():
            if not ID.fullmatch(path.name):
                continue
            directory = self.directory(path.name)
            file = directory / ".lauf.json"
            if file.exists():
                self.states[path.name] = json.loads(file.read_text())
                self.token_ready[path.name] = asyncio.Event()
        await self.reap()
        for ident, state in list(self.states.items()):
            if state["zustand"] == "laeuft":
                self.tasks[ident] = asyncio.create_task(self.run(ident))
        self.housekeeper = asyncio.create_task(self.housekeeping())

    async def shutdown(self) -> None:
        tasks = list(self.tasks.values())
        if self.housekeeper:
            tasks.append(self.housekeeper)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.aclose()

    async def housekeeping(self) -> None:
        while True:
            await asyncio.sleep(600)
            await self.reap()

    async def reap(self) -> None:
        for ident, state in list(self.states.items()):
            if time.time() - state["aktivitaet"] > self.retention:
                try:
                    await self.cleanup(ident)
                except httpx.HTTPError:
                    LOG.error(
                        "bericht_id=%s grund=platz_neustart aufraeumen=verschoben",
                        ident,
                    )

    async def start(self, body: Start) -> dict[str, bool]:
        ident = body.auftrag.bericht_id
        async with self.lock:
            if ident in self.states:
                self.tokens[ident] = body.claude_token
                self.states[ident]["worker"] = body.worker
                self.token_ready[ident].set()
                return {"angehaengt": True}
            for other, state in self.states.items():
                if state["zustand"] == "laeuft":
                    raise HTTPException(409, {"belegt": other})
            directory = self.directory(ident)
            directory.mkdir(mode=0o711)
            directory.chmod(0o711)
            self.states[ident] = {
                "auftrag": body.auftrag.model_dump(mode="json", by_alias=True),
                "worker": body.worker,
                "zustand": "laeuft",
                "schritt": "daten",
                "fertig": 0,
                "gesamt": 5,
                "schritte": [],
                "active": {},
                "eingang_fertig": False,
                "erkunder_ausgefallen": [],
                "korrekturkreis_gelaufen": False,
            }
            self.tokens[ident] = body.claude_token
            self.token_ready[ident] = asyncio.Event()
            self.token_ready[ident].set()
            self.save(ident)
            self.tasks[ident] = asyncio.create_task(self.run(ident))
            return {"angehaengt": False}

    def headers(self) -> dict[str, str]:
        return {"X-Erkunder-Intern": os.environ.get("ERKUNDER_INTERNAL_TOKEN", "")}

    async def download(self, ident: str, order: Auftrag) -> None:
        entry = self.directory(ident) / "eingang"
        entry.mkdir(exist_ok=True, mode=0o755)
        entry.chmod(0o755)
        for name in ("messdaten", "unterlagen", "plan"):
            (entry / name).mkdir(exist_ok=True, mode=0o755)
            (entry / name).chmod(0o755)
        for name, content in (
            ("vorwissen.md", order.vorwissen_md),
            ("vertiefung.md", order.vertiefung_md),
        ):
            if content is not None:
                (entry / name).write_text(content)
                (entry / name).chmod(0o644)
        for file in order.dateien:
            target = entry / file.ziel
            digest = hashlib.sha256()
            size = 0
            async with asyncio.timeout(120):
                async with self.client.stream("GET", str(file.url)) as response:
                    response.raise_for_status()
                    with target.open("wb") as stream:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > file.bytes:
                                raise StepFailed("daten", "unbekannt: bytes")
                            digest.update(chunk)
                            stream.write(chunk)
            target.chmod(0o644)
            if size != file.bytes or digest.hexdigest().lower() != file.sha256.lower():
                raise StepFailed("daten", "unbekannt: sha256 oder bytes")
        self.states[ident]["eingang_fertig"] = True
        self.save(ident)

    def successful(self, ident: str, name: str) -> bool:
        return any(
            s["name"] == name and s["status"] == "ok"
            for s in self.states[ident]["schritte"]
        )

    def output(self, ident: str, name: str) -> Path:
        filename = "pruefung.md" if name.startswith("pruefung") else "ergebnis.md"
        path = self.directory(ident) / name / filename
        if path.is_symlink() or path.resolve().parent != path.parent.resolve():
            raise StepFailed(name, "cli_fehler: ergebnis-pfad")
        return path

    async def step(
        self,
        ident: str,
        name: str,
        slot: int,
        prompt: str,
        inputs: dict[str, str] | None = None,
    ) -> None:
        state = self.states[ident]
        if self.successful(ident, name):
            return
        attempts = sum(s["name"] == name for s in state["schritte"])
        while attempts < 2:
            active = state["active"].get(name)
            meta: dict[str, Any]
            try:
                if active:
                    slot = active["slot"]
                else:
                    if attempts or slot in state.get("unsichere_plaetze", []):
                        response = await self.client.post(
                            self.places[slot] + "/abbrechen", headers=self.headers()
                        )
                        response.raise_for_status()
                        state["unsichere_plaetze"] = [
                            p for p in state.get("unsichere_plaetze", []) if p != slot
                        ]
                    await self.token_ready[ident].wait()
                    directory = self.directory(ident) / name
                    if directory.exists():
                        if directory.is_symlink():
                            raise StepFailed(name, "cli_fehler: schritt-pfad")
                        shutil.rmtree(directory)
                    directory.mkdir(mode=0o700)
                    directory.chmod(0o700)
                    for filename, text in (inputs or {}).items():
                        file = directory / filename
                        file.write_text(text)
                        file.chmod(0o600)
                        self.chown(file, 1101 + slot, 1100)
                    self.chown(directory, 1101 + slot, 1100)
                    state["active"][name] = {
                        "slot": slot,
                        "started": time.time(),
                        "worker": state["worker"],
                    }
                    state["schritt"] = name
                    self.save(ident)
                if not active:
                    response = await self.client.post(
                        self.places[slot] + "/schritt",
                        headers=self.headers(),
                        json={
                            "bericht_id": ident,
                            "schritt": name,
                            "ordner": str(self.directory(ident) / name),
                            "prompt": prompt,
                            "timeout_s": 1200,
                            "max_turns": 100,
                            "claude_token": self.tokens[ident],
                        },
                    )
                    response.raise_for_status()
                while True:
                    if time.time() - state["active"][name]["started"] > 1260:
                        response = await self.client.post(
                            self.places[slot] + "/abbrechen", headers=self.headers()
                        )
                        response.raise_for_status()
                        meta = {"status": "abbruch", "abbruch_grund": "zeit"}
                        break
                    response = await self.client.get(
                        f"{self.places[slot]}/schritt/{ident}/{name}",
                        headers=self.headers(),
                    )
                    response.raise_for_status()
                    result = response.json()
                    if result["zustand"] != "laeuft":
                        meta = result["meta"]
                        break
                    self.save(ident)
                    await asyncio.sleep(self.poll_s)
            except (httpx.HTTPError, KeyError, ValueError):
                state.setdefault("unsichere_plaetze", []).append(slot)
                meta = {"status": "abbruch", "abbruch_grund": "platz_neustart"}
            attempts += 1
            record = {
                "name": name,
                "versuch": attempts,
                "status": meta["status"],
                "abbruch_grund": meta.get("abbruch_grund"),
                "dauer_s": meta.get("dauer_s", 0.0),
                "zuege": meta.get("zuege", 0),
                "tokens": meta.get(
                    "tokens",
                    {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
                ),
                "ram_spitze_mb": meta.get("ram_spitze_mb", 0),
                "worker": state["active"].get(name, {}).get("worker", state["worker"]),
            }
            if record["status"] == "ok":
                try:
                    if not self.output(ident, name).read_text().strip():
                        raise ValueError("empty")
                except (OSError, ValueError, StepFailed):
                    record.update(
                        status="abbruch", abbruch_grund="cli_fehler: kein ergebnis"
                    )
            state["schritte"].append(record)
            state["active"].pop(name, None)
            if record["status"] == "ok":
                state["fertig"] += 1
            self.save(ident)
            if record["status"] == "ok":
                return
        reason = next(
            s["abbruch_grund"] for s in reversed(state["schritte"]) if s["name"] == name
        )
        raise StepFailed(name, reason)

    async def run(self, ident: str) -> None:
        state = self.states[ident]
        order = Auftrag.model_validate(state["auftrag"])
        try:
            if not state["eingang_fertig"]:
                await self.download(ident, order)
            results = await asyncio.gather(
                *(
                    self.step(ident, f"erkunder-{i + 1}", i, erkunder_prompt(order))
                    for i in range(3)
                ),
                return_exceptions=True,
            )
            failed = []
            for index, result in enumerate(results):
                if isinstance(result, BaseException):
                    if not isinstance(result, StepFailed):
                        raise result
                    failed.append(
                        {"schritt": f"erkunder-{index + 1}", "grund": result.reason}
                    )
            state["erkunder_ausgefallen"] = failed
            if len(failed) > 1:
                raise StepFailed(
                    failed[-1]["schritt"], "unbekannt: weniger als zwei Gutachten"
                )
            reports = {
                f"gutachten-{i}.md": self.output(ident, f"erkunder-{i}").read_text()
                for i in range(1, 4)
                if self.successful(ident, f"erkunder-{i}")
            }
            await self.step(
                ident,
                "harmonisierung",
                0,
                harmonisierung_prompt(order, failed),
                reports,
            )
            harmonized = self.output(ident, "harmonisierung").read_text()
            await self.step(
                ident,
                "pruefung",
                0,
                pruefung_prompt(order),
                {"gutachten.md": harmonized},
            )
            review = self.output(ident, "pruefung").read_text()
            if re.search(r"tr(?:ä|ae)gt\s+(?:nicht|teilweise)", review, re.IGNORECASE):
                state["korrekturkreis_gelaufen"] = True
                state["gesamt"] = 7
                self.save(ident)
                await self.step(
                    ident,
                    "harmonisierung-korrektur",
                    0,
                    korrektur_prompt(order),
                    {"gutachten.md": harmonized, "pruefung.md": review},
                )
                await self.step(
                    ident,
                    "pruefung-korrektur",
                    0,
                    pruefung_prompt(order),
                    {
                        "gutachten.md": self.output(
                            ident, "harmonisierung-korrektur"
                        ).read_text()
                    },
                )
            state["zustand"] = "fertig"
            state["meta"] = {
                "schema": "erkunder-ergebnis/1",
                "bericht_id": ident,
                "prompt_version": PROMPT_VERSION,
                "modell": "claude-sonnet-5-5",
                "schritte": state["schritte"],
                "erkunder_ausgefallen": failed,
                "korrekturkreis_gelaufen": state["korrekturkreis_gelaufen"],
            }
        except asyncio.CancelledError:
            raise
        except Exception as error:
            state["zustand"] = "abbruch"
            state["schritt"] = (
                error.step if isinstance(error, StepFailed) else state["schritt"]
            )
            state["fehler"] = (
                error.reason
                if isinstance(error, StepFailed)
                else ("unbekannt: " + type(error).__name__)
            )
            LOG.error(
                "bericht_id=%s schritt=%s grund=%s",
                ident,
                state["schritt"],
                state["fehler"],
            )
        self.save(ident)

    def status(self, ident: str) -> dict[str, Any]:
        self.directory(ident)
        if ident not in self.states:
            raise HTTPException(404, "unbekannt")
        state = self.states[ident]
        return {
            k: state[k]
            for k in ("zustand", "schritt", "fertig", "gesamt", "meta", "fehler")
            if k in state
        }

    def result(self, ident: str) -> dict[str, Any]:
        if self.status(ident)["zustand"] != "fertig":
            raise HTTPException(409, "noch nicht fertig")
        state = self.states[ident]
        texts: dict[str, str] = {}
        scripts: dict[str, dict[str, str]] = {}
        shortened: list[str] = []
        for item in state["schritte"]:
            name = item["name"]
            if item["status"] != "ok":
                continue
            texts[name] = self.output(ident, name).read_text()
            scripts[name] = {}
            budget = 200 * 1024
            directory = self.directory(ident) / name
            for file in sorted((directory / "skripte").rglob("*")):
                if file.suffix not in (".py", ".sh") or not file.is_file():
                    continue
                if file.is_symlink() or not file.resolve().is_relative_to(
                    directory.resolve()
                ):
                    raise HTTPException(409, "Skriptpfad außerhalb Schrittordner")
                with file.open("rb") as stream:
                    data = stream.read(budget + 1)
                if len(data) > budget:
                    if name not in shortened:
                        shortened.append(name)
                    data = data[:budget]
                scripts[name][str(file.relative_to(directory))] = data.decode(
                    "utf-8", errors="ignore"
                )
                budget -= len(data)
        suffix = "-korrektur" if state["korrekturkreis_gelaufen"] else ""
        return {
            "schema": "erkunder-texte/1",
            "bericht_id": ident,
            "texte": texts,
            "skripte": scripts,
            "skripte_gekuerzt": shortened,
            "gutachten_final": texts["harmonisierung" + suffix],
            "pruefung_final": texts["pruefung" + suffix],
        }

    async def cleanup(self, ident: str) -> dict[str, Any]:
        async with self.lock:
            path = self.directory(ident)
            state = self.states.get(ident)
            task = self.tasks.pop(ident, None)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if state:
                slots = {a["slot"] for a in state["active"].values()}
                slots.update(state.get("unsichere_plaetze", []))
                for slot in slots:
                    response = await self.client.post(
                        self.places[slot] + "/abbrechen",
                        headers=self.headers(),
                    )
                    response.raise_for_status()
            size = (
                sum(
                    f.stat().st_size
                    for f in path.rglob("*")
                    if f.is_file() and not f.is_symlink()
                )
                if path.exists()
                else 0
            )
            if path.exists():
                shutil.rmtree(path)
            self.states.pop(ident, None)
            self.tokens.pop(ident, None)
            self.token_ready.pop(ident, None)
            LOG.info("bericht_id=%s geloescht_bytes=%d", ident, size)
            return {"bericht_id": ident, "geloescht_bytes": size}


def create_app(coordinator: Coordinator | None = None) -> FastAPI:
    service = coordinator or Coordinator(
        retention_hours=float(os.environ.get("ERKUNDER_RETENTION_HOURS", "6"))
    )

    async def authorize(x_erkunder_intern: str | None) -> None:
        expected = os.environ.get("ERKUNDER_INTERNAL_TOKEN", "")
        if (
            not expected
            or not x_erkunder_intern
            or not hmac.compare_digest(expected, x_erkunder_intern)
        ):
            raise HTTPException(403, "Interner Schlüssel erforderlich")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.startup()
        yield
        await service.shutdown()

    app = FastAPI(
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.coordinator = service

    @app.middleware("http")
    async def protect_every_request(request, call_next):
        try:
            await authorize(request.headers.get("X-Erkunder-Intern"))
        except HTTPException as error:
            return JSONResponse(
                status_code=error.status_code, content={"detail": error.detail}
            )
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse(status_code=400, content={"detail": "Ungültiger Auftrag"})

    @app.post("/start")
    async def start(body: Start):
        try:
            return await service.start(body)
        except HTTPException as error:
            if error.status_code == 409:
                return JSONResponse(status_code=409, content=error.detail)
            raise

    @app.get("/status/{ident}")
    async def status(ident: str):
        return service.status(ident)

    @app.get("/ergebnis/{ident}")
    async def result(ident: str):
        return service.result(ident)

    @app.post("/aufraeumen/{ident}")
    async def cleanup(ident: str):
        return await service.cleanup(ident)

    return app


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=8100, access_log=False)
