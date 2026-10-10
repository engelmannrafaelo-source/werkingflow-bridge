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
import stat
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from .dateien import read_bytes, read_text
from .models import EINGANGSORDNER, VORGABE_MODELL, Auftrag
from .prompts import (
    PROMPT_VERSION,
    erkunder_prompt,
    harmonisierung_prompt,
    korrektur_prompt,
    pruefung_prompt,
)
from .pruefkreis import (
    artifact_hashes,
    manifest_text,
    migriere_altauftrag,
    prueferzahlen,
    pruefumfang,
    pruefurteil,
    zahlenbelege,
)
from .quellen import kanalmanifest

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
        startup_wait_s: float = 120,
        reattach_wait_s: float = 300,
    ):
        self.root = root
        self.client = client or httpx.AsyncClient(timeout=60, follow_redirects=False)
        self.places = places or [f"http://erkunder-platz-{i}:8200" for i in range(1, 4)]
        self.chown = chown
        self.poll_s = poll_s
        self.retention = retention_hours * 3600
        self.startup_wait_s = startup_wait_s
        self.reattach_wait_s = reattach_wait_s
        self.ready = False
        self.states: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, str] = {}
        self.token_ready: dict[str, asyncio.Event] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.lock = asyncio.Lock()
        self.startup_lock = asyncio.Lock()
        self.deploy_pending = False
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

    def seal(self, ident: str) -> None:
        """Retain results for root, revoke every place's traversal permission."""
        directory = self.directory(ident)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise RuntimeError("Berichtordner gehört nicht dem Leitstand")
        directory.chmod(0o700)

    def seal_reports(self) -> bool:
        found = False
        for path in self.root.iterdir():
            if ID.fullmatch(path.name):
                self.seal(path.name)
                found = True
        return found

    async def stop_places(self) -> None:
        # Also covers processes whose /schritt response or state write was lost.
        # A retained cwd/file descriptor bypasses ancestor chmod: require the
        # place's process cleanup acknowledgement before any UID is reused.
        for slot, place in enumerate(self.places):
            try:
                response = await self.client.post(
                    place + "/abbrechen", headers=self.headers()
                )
                response.raise_for_status()
            except httpx.HTTPError as error:
                LOG.error("platz=%s bericht_trennung=fehlgeschlagen", slot)
                raise HTTPException(
                    503, "Bericht-Trennung: Platz nicht bereinigt"
                ) from error

    async def startup(self) -> None:
        async with self.startup_lock:
            if not self.ready:
                await self.initialize()

    async def initialize(self) -> None:
        # This lifecycle runs only in the standalone Leitstand, never in a worker.
        # HTTPX INFO and HTTPCore DEBUG expose signed download URLs.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o711)
        self.root.chmod(0o711)
        # Seal first, even orphan directories and legacy six-hour retention.
        # Compose starts places after this container. Wait without granting work.
        # On expiry stay alive but unavailable, rather than entering a restart loop.
        self.ready = False
        if self.seal_reports():
            deadline = time.monotonic() + self.startup_wait_s
            while True:
                try:
                    async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                        await self.stop_places()
                    break
                except (HTTPException, TimeoutError):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        LOG.error(
                            "Leitstand gesperrt: Plaetze nach %.1fs nicht bereit; "
                            "POST /aufraeumen/<id> zum Bereinigen und Wiederholen",
                            self.startup_wait_s,
                        )
                        return
                    LOG.warning("Leitstand wartet auf Plaetze; Vergabe gesperrt")
                    await asyncio.sleep(min(2, remaining))
        for path in self.root.iterdir():
            if not ID.fullmatch(path.name):
                continue
            directory = self.directory(path.name)
            file = directory / ".lauf.json"
            if file.exists():
                self.states[path.name] = migriere_altauftrag(json.loads(read_text(file)))
                self.token_ready[path.name] = asyncio.Event()
        await self.reap()
        for ident, state in list(self.states.items()):
            if state["zustand"] == "laeuft":
                # Old processes were stopped. Recreate interrupted steps only
                # after the worker reattaches with a fresh in-memory token.
                state["active"] = {}
                state["unsichere_plaetze"] = []
                state["wiederaufnahme"] = True
                self.save(ident)
                self.tasks[ident] = asyncio.create_task(self.run(ident))
        self.housekeeper = asyncio.create_task(self.housekeeping())
        self.ready = True

    async def shutdown(self) -> None:
        tasks = list(self.tasks.values())
        if self.housekeeper:
            tasks.append(self.housekeeper)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            for ident in self.states:
                if self.directory(ident).exists():
                    self.seal(ident)
            if any(s["zustand"] == "laeuft" for s in self.states.values()):
                await self.stop_places()
        finally:
            await self.client.aclose()

    async def housekeeping(self) -> None:
        while True:
            await asyncio.sleep(600)
            try:
                await self.reap()
            except Exception as error:
                LOG.error("hausmeister_runde fehler=%s", type(error).__name__)

    async def reap(self) -> None:
        for ident, state in list(self.states.items()):
            try:
                if time.time() - state["aktivitaet"] > self.retention:
                    await self.cleanup(ident)
            except Exception as error:
                LOG.error(
                    "bericht_id=%s aufraeumen=verschoben fehler=%s",
                    ident,
                    type(error).__name__,
                )
        for path in self.root.iterdir():
            if not ID.fullmatch(path.name) or path.name in self.states:
                continue
            try:
                directory = self.directory(path.name)
                if (directory / ".lauf.json").exists():
                    continue
                if time.time() - directory.stat().st_mtime > self.retention:
                    await self.cleanup(path.name)
            except Exception as error:
                LOG.error(
                    "bericht_id=%s verwaist=verschoben fehler=%s",
                    path.name,
                    type(error).__name__,
                )

    async def prepare_deploy(self) -> dict:
        """Check idle and close admission under the same lock as /start."""
        async with self.lock:
            running = sorted(
                ident for ident, state in self.states.items()
                if state["zustand"] == "laeuft"
            )
            if not running:
                self.deploy_pending = True
            return {"bereit": not running, "berichte": running}

    async def cancel_deploy(self) -> dict:
        async with self.lock:
            self.deploy_pending = False
            return {"freigegeben": True}

    async def start(self, body: Start) -> dict[str, bool]:
        ident = body.auftrag.bericht_id
        async with self.lock:
            if not self.ready:
                raise HTTPException(503, "Leitstand wartet auf bereinigte Plaetze")
            if ident in self.states:
                self.tokens[ident] = body.claude_token
                self.states[ident]["worker"] = body.worker
                self.token_ready[ident].set()
                return {"angehaengt": True}
            if self.deploy_pending:
                raise HTTPException(503, "Leitstand wird ausgerollt")
            for other, state in self.states.items():
                if state["zustand"] == "laeuft":
                    raise HTTPException(409, {"belegt": other})
            # Gate every new report, including after cleanup removed its state.
            # Errors propagate; neither a task nor a writable report is created.
            self.seal_reports()
            await self.stop_places()
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
        for name in EINGANGSORDNER:
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
        self.states[ident]["quellkanaele"] = kanalmanifest(entry, order)
        (entry / "quellen.md").write_text(manifest_text(order) + "\n\n## Messkanäle je Quelle\n\n"
                                            + json.dumps(self.states[ident]["quellkanaele"], ensure_ascii=False))
        (entry / "quellen.md").chmod(0o644)
        self.states[ident]["eingang_fertig"] = True
        self.save(ident)

    def successful(self, ident: str, name: str) -> bool:
        return any(
            s["name"] == name and s["status"] == "ok"
            for s in self.states[ident]["schritte"]
        )

    def output(self, ident: str, name: str) -> str:
        filename = "pruefung.md" if name.startswith("pruefung") else "ergebnis.md"
        try:
            text = read_text(self.directory(ident) / name / filename)
            records = [
                item for item in self.states[ident]["schritte"]
                if item["name"] == name and item["status"] == "ok"
            ]
            if records and records[-1].get("sha256") != hashlib.sha256(
                text.encode("utf-8")
            ).hexdigest():
                raise StepFailed(
                    name, "Ergebnis-Integritaet: Text veraendert oder Hash fehlt"
                )
            return text
        except (OSError, ValueError) as error:
            raise StepFailed(name, "cli_fehler: ergebnis-pfad oder groesse") from error

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
                            "modell": state["auftrag"].get("modell", VORGABE_MODELL),
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
            if "lesezugriffe" in meta:
                record["lesezugriffe"] = meta["lesezugriffe"]
            if record["status"] == "ok":
                try:
                    text = self.output(ident, name)
                    if not text.strip():
                        raise ValueError("empty")
                    record["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
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
            await self.prepare_run(ident, order)
            failed = await self.explore(ident, order)
            reports = {
                f"gutachten-{i}.md": self.output(ident, f"erkunder-{i}")
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
            await self.review_loop(ident, order)
            self.complete(ident, failed)
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
        finally:
            # No await between completion and revocation. The entire retained
            # report is now root-only, including inputs, scripts and results.
            try:
                self.seal(ident)
            except Exception as error:
                state["zustand"] = "abbruch"
                state["fehler"] = "Bericht-Trennung: Rechteentzug fehlgeschlagen"
                self.save(ident)
                LOG.error(
                    "bericht_id=%s rechteentzug=fehlgeschlagen fehler=%s",
                    ident,
                    type(error).__name__,
                )
                raise
        self.save(ident)

    async def prepare_run(self, ident: str, order: Auftrag) -> None:
        state = self.states[ident]
        if state.pop("wiederaufnahme", False):
            LOG.warning(
                "bericht_id=%s wartet maximal %.1fs auf frischen Token; "
                "Ausweg POST /aufraeumen/%s", ident, self.reattach_wait_s, ident,
            )
            try:
                async with asyncio.timeout(self.reattach_wait_s):
                    await self.token_ready[ident].wait()
            except TimeoutError as error:
                raise StepFailed(
                    "wiederaufnahme", "Wiederanhaengen: Zeitgrenze erreicht"
                ) from error
            self.directory(ident).chmod(0o711)
        if not state["eingang_fertig"]:
            await self.download(ident, order)

    async def explore(self, ident: str, order: Auftrag) -> list[dict]:
        state = self.states[ident]
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
        return failed

    def complete(self, ident: str, failed: list[dict]) -> None:
        state = self.states[ident]
        # Validate again after the reviewer (same UID as harmonization).
        # result() validates the exact bytes it returns as well.
        for item in state["schritte"]:
            if item["status"] == "ok":
                self.output(ident, item["name"])
        state["zustand"] = "fertig"
        state["meta"] = {
            "schema": "erkunder-ergebnis/1",
            "bericht_id": ident,
            "prompt_version": PROMPT_VERSION,
            "modell": state["auftrag"].get("modell", VORGABE_MODELL),
            "schritte": [
                {k: v for k, v in item.items() if k != "sha256"}
                for item in state["schritte"]
            ],
            "erkunder_ausgefallen": failed,
            "korrekturkreis_gelaufen": state["korrekturkreis_gelaufen"],
            "offene_befunde_anzahl": len(state["offene_befunde"]),
            "pruefstatus": "offen" if state["offene_befunde"] else "widerspruchsfrei",
            "korrekturrunden": state["korrekturrunden"],
        }

    def report_evidence(self, ident: str, name: str, report: str, order: Auftrag) -> list[str]:
        state = self.states[ident]
        machine, artifacts = zahlenbelege(
            self.directory(ident) / name, report, state["quellkanaele"],
        )
        previous = state.get("nachweis_hashes", {}).get(name)
        if previous is not None and previous != artifact_hashes(artifacts):
            raise StepFailed(name, "Nachweis-Integritaet: Skript/Ergebnis nachträglich verändert")
        machine.extend(pruefumfang(report, order.pruefliste))
        state.setdefault("nachweise", {})[name] = artifacts
        state.setdefault("nachweis_hashes", {})[name] = artifact_hashes(artifacts)
        self.save(ident)
        return machine

    async def review_loop(self, ident: str, order: Auftrag) -> None:
        """Resume numbered steps; a fresh review owns every corrected version."""
        state = self.states[ident]
        state["quellkanaele"] = kanalmanifest(self.directory(ident) / "eingang", order)
        report_name = "harmonisierung"
        for round_no in range(order.korrekturkreis + 1):
            suffix = "" if round_no == 0 else "-korrektur" + (f"-{round_no}" if round_no > 1 else "")
            review_name = "pruefung" + suffix
            report = self.output(ident, report_name)
            machine = self.report_evidence(ident, report_name, report, order)
            await self.step(ident, review_name, 0, pruefung_prompt(order), {
                "gutachten.md": report,
                "maschinenbefunde.json": json.dumps(machine, ensure_ascii=False),
            })
            try:
                findings = pruefurteil(self.output(ident, review_name))
            except ValueError as error:
                findings = [f"Prüfurteil unvollständig: {error}"]
            self.report_evidence(ident, report_name, report, order)
            machine.extend(prueferzahlen(
                self.directory(ident) / report_name, report, self.output(ident, review_name),
                state["quellkanaele"],
            ))
            state.update(
                offene_befunde=list(dict.fromkeys(machine + findings)),
                gutachten_schritt=report_name, pruefung_schritt=review_name,
                korrekturrunden=round_no,
            )
            self.save(ident)
            if not state["offene_befunde"] or round_no == order.korrekturkreis:
                return
            next_no = round_no + 1
            report_name = "harmonisierung-korrektur" + (f"-{next_no}" if next_no > 1 else "")
            state["korrekturkreis_gelaufen"] = True
            state["gesamt"] = 5 + 2 * next_no
            self.save(ident)
            await self.step(ident, report_name, 0, korrektur_prompt(order), {
                "gutachten.md": report,
                "pruefung.md": self.output(ident, review_name),
                "maschinenbefunde.json": json.dumps(state["offene_befunde"], ensure_ascii=False),
            })

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
        skipped: list[str] = []
        for item in state["schritte"]:
            name = item["name"]
            if item["status"] != "ok":
                continue
            texts[name] = self.output(ident, name)
            scripts[name] = {}
            budget = 200 * 1024
            directory = self.directory(ident) / name
            for file in sorted((directory / "skripte").rglob("*")):
                if file.suffix not in (".py", ".sh"):
                    continue
                if budget <= 0:
                    # Budget aufgebraucht: die Datei kommt NICHT als leerer
                    # String (sah wie ein leeres Skript aus), sondern ist
                    # gemeldet: Schritt in skripte_gekuerzt, Datei in
                    # skripte_uebersprungen.
                    if name not in shortened:
                        shortened.append(name)
                    skipped.append(str(file.relative_to(directory)))
                    continue
                try:
                    data = read_bytes(file, limit=budget)
                except (OSError, ValueError) as error:
                    relative = str(file.relative_to(directory))
                    skipped.append(relative)
                    LOG.warning(
                        "bericht_id=%s schritt=%s skript=uebersprungen fehler=%s",
                        ident,
                        name,
                        type(error).__name__,
                    )
                    continue
                if len(data) > budget:
                    if name not in shortened:
                        shortened.append(name)
                    data = data[:budget]
                scripts[name][str(file.relative_to(directory))] = data.decode(
                    "utf-8", errors="ignore"
                )
                budget -= len(data)
        final = self.final_report(state, texts, scripts)
        return {
            "schema": "erkunder-texte/1",
            "bericht_id": ident,
            "texte": texts,
            "skripte": scripts,
            "skripte_gekuerzt": shortened,
            "skripte_uebersprungen": skipped,
            "gutachten_final": final,
            "pruefung_final": texts[state["pruefung_schritt"]],
        }

    @staticmethod
    def final_report(state: dict, texts: dict, scripts: dict) -> str:
        if state.get("altauftrag_ungeprueft"):
            return str(texts[state["gutachten_schritt"]])
        for name, artifacts in state.get("nachweise", {}).items():
            if artifact_hashes(artifacts) != state["nachweis_hashes"][name]:
                raise StepFailed(name, "Nachweis-Integritaet: gespeicherte Belege verändert")
            scripts[name].update(artifacts)
        final = texts[state["gutachten_schritt"]]
        if state["offene_befunde"]:
            final += "\n\n## Offene Befunde nach Korrekturgrenze\n\n" + "\n".join(
                "- " + finding for finding in state["offene_befunde"]
            )
        final += "\n\n" + manifest_text(Auftrag.model_validate(state["auftrag"]))
        return final

    async def cleanup(self, ident: str) -> dict[str, Any]:
        async with self.lock:
            path = self.directory(ident)
            state = self.states.get(ident)
            if path.exists():
                self.seal(ident)
            abort_errors: list[dict[str, Any]] = []
            task = self.tasks.pop(ident, None)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if state:
                slots = {a["slot"] for a in state["active"].values()}
                slots.update(state.get("unsichere_plaetze", []))
                for slot in slots:
                    try:
                        response = await self.client.post(
                            self.places[slot] + "/abbrechen",
                            headers=self.headers(),
                        )
                        response.raise_for_status()
                    except httpx.HTTPError as error:
                        abort_errors.append(
                            {"platz": slot, "fehler": type(error).__name__}
                        )
                        LOG.error(
                            "bericht_id=%s platz=%s abbrechen=fehlgeschlagen fehler=%s",
                            ident,
                            slot,
                            type(error).__name__,
                        )
            size = None
            try:
                size = (
                    sum(
                        f.lstat().st_size
                        for f in path.rglob("*")
                        if f.is_file() and not f.is_symlink()
                    )
                    if path.exists()
                    else 0
                )
            except Exception as error:
                LOG.error(
                    "bericht_id=%s groesse=unbekannt fehler=%s",
                    ident,
                    type(error).__name__,
                )
            if path.exists():
                shutil.rmtree(path)
            self.states.pop(ident, None)
            self.tokens.pop(ident, None)
            self.token_ready.pop(ident, None)
            LOG.info("bericht_id=%s geloescht_bytes=%s", ident, size)
            return {
                "bericht_id": ident,
                "geloescht_bytes": size,
                "platz_abbruch_fehler": abort_errors,
            }


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
        if not os.environ.get("ERKUNDER_INTERNAL_TOKEN"):
            raise RuntimeError("ERKUNDER_INTERNAL_TOKEN fehlt")
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
        if not service.ready and not (
            request.method == "POST" and request.url.path.startswith("/aufraeumen/")
        ):
            # Also keep authenticated missing-route probes (B1n) unhealthy.
            return JSONResponse(
                status_code=503,
                content={"detail": "Leitstand wartet auf bereinigte Plaetze"},
            )
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse(status_code=400, content={"detail": "Ungültiger Auftrag"})

    @app.get("/bereitschaft")
    async def readiness():
        if not service.ready:
            raise HTTPException(503, "Leitstand wartet auf bereinigte Plaetze")
        return {"bereit": True}

    @app.post("/start")
    async def start(body: Start):
        try:
            return await service.start(body)
        except HTTPException as error:
            if error.status_code == 409:
                return JSONResponse(status_code=409, content=error.detail)
            raise

    @app.post("/deploy/pruefen")
    async def prepare_deploy():
        return await service.prepare_deploy()

    @app.delete("/deploy/pruefen")
    async def cancel_deploy():
        return await service.cancel_deploy()

    @app.get("/status/{ident}")
    async def status(ident: str):
        return service.status(ident)

    @app.get("/ergebnis/{ident}")
    async def result(ident: str):
        try:
            return service.result(ident)
        except StepFailed as error:
            LOG.error("bericht_id=%s ergebnis=%s", ident, error.reason)
            raise HTTPException(409, error.reason) from error

    @app.post("/aufraeumen/{ident}")
    async def cleanup(ident: str):
        result = await service.cleanup(ident)
        if not service.ready:
            await service.startup()
        return result

    return app


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=8100, access_log=False)
