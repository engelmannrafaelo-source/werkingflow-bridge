"""Private durable attempt protocol. Source/results never leave authenticated routes.

One shared local volume per document service; flock survives service restart
through the guardian's inherited descriptor. Missing receipts are UNKNOWN, not
permission to launch another conversion. No result eviction while unacknowledged.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .document_process import run, write_json, execution_identity

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/document/calls")
ROOT = Path("/var/lib/bridge-document-jobs")
_tasks: set[asyncio.Task] = set()
_semaphore = None


def configure(semaphore) -> None:
    global _semaphore
    _semaphore = semaphore


def call_path(call_id: str) -> Path:
    try:
        if uuid.UUID(call_id).hex != call_id:
            raise ValueError()
    except ValueError:
        raise HTTPException(400, "invalid document call id") from None
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    return ROOT / call_id


@contextmanager
def locked(path: Path):
    # A service-wide short admission lock also covers partial writes after a
    # crash before acceptance. No converter runs while holding this lock.
    with (ROOT / ".admission").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def read(path: Path, name: str) -> dict:
    return json.loads((path / name).read_text())


def status(path: Path) -> dict:
    if not (path / "accepted.json").exists():
        raise HTTPException(404, "document call not found")
    accepted = read(path, "accepted.json")
    answer = {key: accepted[key] for key in ("call_id", "attempt_id", "digest", "deadline_at")}
    if (path / "receipt.json").exists():
        receipt = read(path, "receipt.json")
        answer["stop_receipt"] = receipt
        if (path / "cancel.json").exists():
            answer["status"] = "cancelled"
        elif receipt["reason"] == "completed" and (path / "result.json").exists():
            envelope = read(path, "result.json")
            answer["status"] = "done" if envelope["status_code"] == 200 else "error"
            answer["result"] = envelope
        else:
            answer["status"] = "interrupted"
        return answer
    answer["status"] = "accepted"
    if (path / "started.json").exists():
        with (path / "execution.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                answer["status"] = "running"
            else:
                # Includes crash between durable start and process creation.
                # No provider call may be repeated from an unknown outcome.
                started = read(path, "started.json")
                previous = started.get("execution_identity")
                if previous is not None and previous != execution_identity():
                    # Boot/PID namespace/PID-1 start changed: the old container
                    # process tree cannot still exist in this service instance.
                    write_json(path / "receipt.json", {
                        "reason": "service_instance_gone", "local_stopped": True,
                        "reaped": True,
                        "provider_outcome": "unknown" if any(path.glob("provider-pending-*")) else "finished_or_not_started",
                        "finished_at": datetime.now(timezone.utc).timestamp(),
                    })
                    return status(path)
                answer["status"] = "unknown"
    return answer


async def execute(path: Path) -> None:
    stop = asyncio.Event()
    with (path / "execution.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if (path / "started.json").exists() or (path / "receipt.json").exists():
            return
        accepted = read(path, "accepted.json")

        async def cancellation():
            while True:
                if (path / "cancel.json").exists():
                    stop.set()
                    return
                deadline = accepted["deadline_at"]
                if deadline and datetime.now(timezone.utc) >= datetime.fromisoformat(deadline):
                    write_json(path / "cancel.json", {"reason": "deadline_expired"})
                    stop.set()
                    return
                await asyncio.sleep(1)

        observer = asyncio.create_task(cancellation())
        acquire = asyncio.create_task(_semaphore.acquire())
        stopped = asyncio.create_task(stop.wait())
        acquired = False
        try:
            await asyncio.wait({acquire, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if stop.is_set():
                write_json(path / "receipt.json", {
                    "reason": "cancelled_before_start", "local_stopped": True,
                    "reaped": True, "provider_outcome": "not_started",
                })
                return
            await acquire
            acquired = True
            write_json(path / "started.json", {"attempt_id": accepted["attempt_id"], "execution_identity": execution_identity()})
            await run(path, stop, lock_fd=lock.fileno())
        except Exception as exc:
            logger.error("document attempt supervisor failed (%s); outcome unconfirmed", type(exc).__name__)
        finally:
            if not acquire.done():
                acquire.cancel()
            await asyncio.gather(acquire, return_exceptions=True)
            # acquire and stop may finish in the same loop turn.
            if acquired or (not acquire.cancelled() and acquire.exception() is None):
                _semaphore.release()
            observer.cancel()
            stopped.cancel()
            await asyncio.gather(observer, stopped, return_exceptions=True)


def schedule(path: Path):
    task = asyncio.create_task(execute(path))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@router.put("/{call_id}")
async def accept(call_id: str, request: Request):
    from .app import _read_uploaded_file
    path = call_path(call_id)
    content, filename, mime, fields = await _read_uploaded_file(request)
    deadline = request.headers.get("x-document-deadline")
    if deadline:
        try:
            parsed = datetime.fromisoformat(deadline)
            if parsed.tzinfo is None:
                raise ValueError()
            deadline = parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            raise HTTPException(400, "document deadline must be absolute with timezone") from None
    spec = {"filename": filename, "mime_type": mime, "fields": fields,
            "managed": True, "repair_actualtext": request.headers.get("x-document-actualtext-repair") == "true"}
    digest = hashlib.sha256(content).hexdigest() + ":" + hashlib.sha256(
        json.dumps({**spec, "deadline_at": deadline}, sort_keys=True).encode()
    ).hexdigest()
    with locked(path):
        if (path / "accepted.json").exists():
            if read(path, "accepted.json").get("tombstone"):
                return JSONResponse(status_code=200, content=status(path))
            if read(path, "accepted.json")["digest"] != digest:
                raise HTTPException(409, "document call digest conflict")
        else:
            from src.vision_provider import vision_available
            if str(fields.get("describe_images", "")).lower() in ("1", "true", "yes", "on") and not vision_available():
                raise HTTPException(503, "document Vision is not configured")
            path.mkdir(mode=0o700, exist_ok=True)
            with (path / "source").open("wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            write_json(path / "request.json", spec)
            write_json(path / "accepted.json", {
                "call_id": call_id, "attempt_id": uuid.uuid4().hex,
                "digest": digest, "deadline_at": deadline,
            })
    schedule(path)
    return JSONResponse(status_code=202, content=status(path))


@router.get("/{call_id}")
async def get(call_id: str):
    path = call_path(call_id)
    answer = status(path)
    if answer["status"] == "accepted":
        schedule(path)  # accepted before service died, never started
    return answer


@router.delete("/{call_id}")
async def cancel(call_id: str):
    path = call_path(call_id)
    with locked(path):
        if not (path / "accepted.json").exists():
            path.mkdir(mode=0o700, exist_ok=True)
            write_json(path / "cancel.json", {"reason": "user_cancelled"})
            write_json(path / "receipt.json", {
                "reason": "cancelled_before_acceptance", "local_stopped": True,
                "reaped": True, "provider_outcome": "not_started",
            })
            write_json(path / "accepted.json", {
                "call_id": call_id, "attempt_id": None, "digest": None,
                "deadline_at": None, "tombstone": True,
            })
        current = status(path)
        if current["status"] in ("done", "error", "cancelled", "interrupted"):
            return current
        if not (path / "cancel.json").exists():
            write_json(path / "cancel.json", {"reason": "user_cancelled"})
    if current["status"] == "accepted":
        schedule(path)
    return JSONResponse(status_code=202, content={**current, "cancel_requested": True})
