"""One isolated child process at a time, with process-group and cgroup supervision."""

import asyncio
import hmac
import json
import os
import signal
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr


class Schritt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bericht_id: str = Field(pattern=r"^[a-z0-9-]{8,80}$")
    schritt: str = Field(
        pattern=(
            r"^(erkunder-[123]|harmonisierung|pruefung|"
            r"harmonisierung-korrektur|pruefung-korrektur)$"
        )
    )
    ordner: str
    prompt: str
    timeout_s: float = Field(gt=0)
    max_turns: int = Field(gt=0)
    claude_token: SecretStr = Field(min_length=1)


def has_content(path: Path) -> bool:
    with path.open(encoding="utf-8") as source:
        while chunk := source.read(64 * 1024):
            if chunk.strip():
                return True
    return False


def contains_secret(path: Path, token: bytes) -> bool:
    overlap = b""
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            data = overlap + chunk
            if token in data:
                return True
            overlap = data[-(len(token) - 1) :] if len(token) > 1 else b""
    return False


class Platz:
    def __init__(
        self,
        root: Path = Path("/arbeit"),
        cgroup: Path = Path("/sys/fs/cgroup"),
        command: list[str] | None = None,
        sample_s: float = 2.0,
    ):
        self.root = root
        self.cgroup = cgroup
        self.command = command or [sys.executable, "-m", "src.erkunder.kind"]
        self.sample_s = sample_s
        self.task: asyncio.Task[None] | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.states: dict[tuple[str, str], dict[str, Any]] = {}
        self.lock = asyncio.Lock()
        self.cancelled = False

    def memory(self) -> tuple[int, int]:
        current = int((self.cgroup / "memory.current").read_text())
        events = dict(
            line.split()
            for line in (self.cgroup / "memory.events").read_text().splitlines()
        )
        return current, int(events["oom_kill"])

    def kill(self) -> None:
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return  # The process group already exited.

    async def start(self, body: Schritt) -> dict[str, str]:
        async with self.lock:
            if self.task and not self.task.done():
                raise HTTPException(409, "belegt")
            expected = self.root / body.bericht_id / body.schritt
            if Path(body.ordner).resolve() != expected or not expected.is_dir():
                raise HTTPException(400, "ungueltiger Schrittordner")
            if expected.resolve() != expected:
                raise HTTPException(400, "Schrittordner ist ein Symlink")
            self.cancelled = False
            self.states[(body.bericht_id, body.schritt)] = {"zustand": "laeuft"}
            self.task = asyncio.create_task(self.run(body))
        return {"zustand": "laeuft"}

    async def abort(self) -> dict[str, bool]:
        active = self.task is not None and not self.task.done()
        if active:
            self.cancelled = True
            self.kill()
            assert self.task is not None
            await self.task
        return {"abgebrochen": active}

    async def run(self, body: Schritt) -> None:
        started = time.monotonic()
        peak = 0
        reason = None
        metrics: dict[str, Any] = {}
        communication = None
        self.process = None
        try:
            peak, initial_oom = self.memory()
            if self.cancelled:
                raise RuntimeError("abgebrochen")
            self.process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                env={
                    key: value
                    for key, value in os.environ.items()
                    if key
                    in {
                        "PATH",
                        "LANG",
                        "LC_ALL",
                        "PYTHONPATH",
                        "HTTPS_PROXY",
                        "HTTP_PROXY",
                        "NO_PROXY",
                        "https_proxy",
                        "http_proxy",
                        "no_proxy",
                        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                    }
                },
            )
            payload = body.model_dump()
            payload["claude_token"] = body.claude_token.get_secret_value()
            communication = asyncio.create_task(
                self.process.communicate(json.dumps(payload).encode())
            )
            while True:
                current, oom = self.memory()
                peak = max(peak, current)
                if oom > initial_oom:
                    reason = "speicher"
                    break
                if self.cancelled:
                    reason = "cli_fehler: abgebrochen"
                    break
                remaining = body.timeout_s - (time.monotonic() - started)
                if remaining <= 0:
                    reason = "zeit"
                    break
                done, _ = await asyncio.wait(
                    [communication], timeout=min(self.sample_s, remaining)
                )
                if done:
                    current, oom = self.memory()
                    peak = max(peak, current)
                    if oom > initial_oom:
                        reason = "speicher"
                    elif self.process.returncode != 0:
                        reason = "cli_fehler: Kindprozess"
                    else:
                        stdout, _ = communication.result()
                        metrics = json.loads(stdout)
                        if metrics.get("fehler"):
                            reason = "cli_fehler: SDK-Lauf"
                    break
            self.kill()
            await communication
            folder = Path(body.ordner)
            result = folder / (
                "pruefung.md" if body.schritt.startswith("pruefung") else "ergebnis.md"
            )
            token = body.claude_token.get_secret_value().encode()
            candidates = [folder / "ergebnis.md", folder / "pruefung.md"]
            scripts = folder / "skripte"
            if scripts.is_symlink():
                scripts.unlink()
                reason = "cli_fehler: Skriptordner-Symlink"
            elif scripts.is_dir():
                candidates.extend(p for p in scripts.rglob("*") if p.is_file())
            for candidate in candidates:
                if candidate.is_symlink():
                    candidate.unlink()
                    reason = "cli_fehler: Ergebnis-Symlink"
                elif candidate.is_file() and contains_secret(candidate, token):
                    candidate.unlink()
                    reason = "geheimnis_im_ergebnis"
            if reason is None and (not result.is_file() or not has_content(result)):
                reason = "cli_fehler: kein ergebnis"
        except Exception:
            reason = reason or "cli_fehler: Platz-Ausfuehrung"
        finally:
            self.kill()
            if communication:
                await communication
            elif self.process:
                await self.process.wait()
            meta = {
                "name": body.schritt,
                "versuch": 1,
                "status": "abbruch" if reason else "ok",
                "abbruch_grund": reason,
                "dauer_s": round(time.monotonic() - started, 3),
                "zuege": metrics.get("zuege", 0),
                "tokens": metrics.get(
                    "tokens",
                    {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
                ),
                "ram_spitze_mb": peak / 1024 / 1024,
                "worker": os.environ.get("INSTANCE_NAME", "erkunder-platz"),
            }
            self.states[(body.bericht_id, body.schritt)] = {
                "zustand": "abbruch" if reason else "fertig",
                "meta": meta,
                "fehler": reason,
            }


def create_app(platz: Platz | None = None) -> FastAPI:
    service = platz or Platz()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await service.abort()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authenticate(request, call_next):
        expected = os.environ.get("ERKUNDER_INTERNAL_TOKEN", "")
        supplied = request.headers.get("X-Erkunder-Intern", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            return JSONResponse(
                status_code=403, content={"detail": "nicht freigegeben"}
            )
        return await call_next(request)

    @app.post("/schritt")
    async def start(body: Schritt) -> dict[str, str]:
        return await service.start(body)

    @app.get("/schritt/{bericht_id}/{schritt}")
    async def status(bericht_id: str, schritt: str) -> dict[str, Any]:
        state = service.states.get((bericht_id, schritt))
        if state is None:
            raise HTTPException(404, "unbekannt")
        return state

    @app.post("/abbrechen")
    async def abort() -> dict[str, bool]:
        return await service.abort()

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8200, access_log=False)
