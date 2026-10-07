"""One isolated child process at a time, with process-group and cgroup supervision."""

import asyncio
import base64
import hmac
import json
import logging
import os
import re
import shutil
import signal
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote_from_bytes

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from src.erkunder.aufraeumen import (
    clear_owned_tmp,
    reap_adopted_children,
    reap_children,
    require_proc_children,
    stop_uid_processes,
)
from src.erkunder.dateien import read_bytes, read_text
from src.erkunder.ipc import clear_owned_ipc
from src.erkunder.prozessschutz import protect_process

LOG = logging.getLogger(__name__)


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
    return bool(read_text(path).strip())


def contains_secret(path: Path, token: bytes) -> bool:
    variants = {token, token[::-1], token.hex().encode(), token.hex().upper().encode()}
    quoted = quote_from_bytes(token, safe="").encode()
    variants.update((quoted, re.sub(rb"%[0-9A-F]{2}", lambda m: m[0].lower(), quoted)))
    variants.update(
        "".join(f"%{byte:02{case}}" for byte in token).encode() for case in ("x", "X")
    )
    # Whole-token base64 also embedded in a longer encoded value: all alignments.
    for offset in range(3):
        for encode in (base64.b64encode, base64.urlsafe_b64encode):
            encoded = encode(b"\0" * offset + token)
            variants.add(
                encoded[(offset * 8 + 5) // 6 : (offset + len(token)) * 8 // 6]
            )
    # Recognize substantial fragments without relying on the common token prefix.
    variants.update(token[i : i + 16] for i in range(max(0, len(token) - 15)))
    data = read_bytes(path)
    return any(value and value in data for value in variants)


def child_failure_reason(output: bytes) -> str:
    """Use the child's sanitized failure reason; never expose its raw stderr."""
    try:
        message = json.loads(output)
    except (TypeError, ValueError):
        return "cli_fehler: Kindprozess"
    reason = message.get("fehler") if isinstance(message, dict) else None
    if isinstance(reason, str) and re.fullmatch(
        r"cli_fehler: SDK-Lauf: [A-Za-z][A-Za-z0-9_]*", reason
    ):
        return reason
    return "cli_fehler: Kindprozess"


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
        self.cleanup_failed = False

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
            if self.cleanup_failed:
                raise HTTPException(503, "Platz-Aufraeumen fehlgeschlagen")
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
        if self.cleanup_failed:
            raise HTTPException(503, "Platz-Aufraeumen fehlgeschlagen")
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
            temporary = Path(body.ordner) / ".home" / "tmp"
            temporary.mkdir(parents=True, exist_ok=True)
            self.process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                env={
                    "TMPDIR": str(temporary),
                    **{
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
                },
            )
            payload = body.model_dump()
            payload["claude_token"] = body.claude_token.get_secret_value()
            communication = asyncio.create_task(
                self.process.communicate(json.dumps(payload).encode())
            )
            while True:
                reap_adopted_children(self.process.pid)
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
                    [communication],
                    # Reap during long steps, before the PID budget is exhausted.
                    timeout=min(self.sample_s, remaining, 0.01),
                )
                if done:
                    current, oom = self.memory()
                    peak = max(peak, current)
                    stdout, _ = communication.result()
                    if oom > initial_oom:
                        reason = "speicher"
                    elif self.process.returncode != 0:
                        reason = child_failure_reason(stdout)
                    else:
                        metrics = json.loads(stdout)
                        if metrics.get("fehler"):
                            reason = child_failure_reason(stdout)
                    break
            self.kill()
            await stop_uid_processes()
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
                elif candidate.exists():
                    try:
                        if contains_secret(candidate, token):
                            candidate.unlink()
                            reason = "geheimnis_im_ergebnis"
                    except (OSError, ValueError):
                        reason = reason or "cli_fehler: ergebnis-pfad oder groesse"
            if reason is None and (not result.is_file() or not has_content(result)):
                reason = "cli_fehler: kein ergebnis"
        except asyncio.CancelledError:
            reason = "cli_fehler: abgebrochen"
            raise
        except Exception as error:
            # Exception text/tracebacks can contain the request's OAuth token.
            LOG.error(
                "bericht_id=%s schritt=%s ausfuehrung=fehlgeschlagen fehler=%s",
                body.bericht_id, body.schritt, type(error).__name__,
            )
            reason = reason or "cli_fehler: Platz-Ausfuehrung"
        finally:
            # Fail closed even if another cancellation interrupts this finally.
            self.cleanup_failed = True
            cleanup_cancelled = False
            try:
                self.kill()
                await stop_uid_processes()
                # Only one step owns subprocesses. Settle its watcher before
                # PID 1 waits for any adopted child, including on cancellation.
                if communication:
                    settled = await asyncio.gather(
                        communication, return_exceptions=True
                    )
                    if isinstance(settled[0], BaseException):
                        reason = reason or "cli_fehler: Kindkommunikation"
                if self.process:
                    await self.process.wait()
                await reap_children()
                clear_owned_tmp()
                clear_owned_ipc()
                home = Path(body.ordner) / ".home"
                if home.is_symlink():
                    home.unlink()
                elif home.exists():
                    shutil.rmtree(home)
                self.cleanup_failed = False
            except (Exception, asyncio.CancelledError) as error:
                cleanup_cancelled = isinstance(error, asyncio.CancelledError)
                self.cleanup_failed = True
                reason = "cli_fehler: Platz-Aufraeumen"
                LOG.error(
                    "bericht_id=%s schritt=%s aufraeumen=fehlgeschlagen fehler=%s",
                    body.bericht_id,
                    body.schritt,
                    type(error).__name__,
                )
            if self.cleanup_failed and communication:
                communication.cancel()
                await asyncio.gather(communication, return_exceptions=True)
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
            if cleanup_cancelled:
                raise asyncio.CancelledError


def create_app(platz: Platz | None = None) -> FastAPI:
    service = platz or Platz()
    internal_token = ""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal internal_token
        require_proc_children()
        protect_process()
        internal_token = os.environ.pop("ERKUNDER_INTERNAL_TOKEN", "")
        if not internal_token:
            raise RuntimeError("ERKUNDER_INTERNAL_TOKEN fehlt")
        try:
            yield
        finally:
            await service.abort()
            internal_token = ""

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authenticate(request, call_next):
        supplied = request.headers.get("X-Erkunder-Intern", "")
        if not internal_token or not hmac.compare_digest(internal_token, supplied):
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
