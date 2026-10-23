"""Private durable attempt protocol. Source/results never leave authenticated routes.

One shared local volume per document service; flock survives service restart
through the guardian's inherited descriptor. Missing receipts are UNKNOWN, not
permission to launch another conversion. ACK or ended-content privacy retention
removes payloads; durable technical tombstones prevent another execution.
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
import time
from pathlib import Path
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .document_process import run, write_json, execution_identity

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/document/calls")
ROOT = Path("/var/lib/bridge-document-jobs")
_tasks: set[asyncio.Task] = set()
_semaphore = None
# Privacy retention after confirmed end, never a conversion runtime limit.
CONTENT_RETENTION_SECONDS = 7 * 24 * 3600
_retention_task = None


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
    if (path / "released.json").exists():
        released = read(path, "released.json")
        return {**answer, "status": released["status"], "content_state": "removed",
                "stop_receipt": read(path, "receipt.json")}
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
            if read(path, "accepted.json").get("tombstone") or (path / "released.json").exists():
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
                "digest": digest, "deadline_at": deadline, "accepted_at": time.time(),
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
        if current["status"] in ("done", "error", "cancelled", "interrupted", "released", "expired"):
            return current
        if not (path / "cancel.json").exists():
            write_json(path / "cancel.json", {"reason": "user_cancelled"})
    if current["status"] == "accepted":
        schedule(path)
    return JSONResponse(status_code=202, content={**current, "cancel_requested": True})


class Acknowledgement(BaseModel):
    attempt_id: str | None
    digest: str | None


def remove_content(path: Path) -> None:
    """Only called under admission AND execution locks, after a durable tombstone."""
    retained = {"accepted.json", "receipt.json", "released.json", "execution.lock"}
    for item in path.iterdir():
        if item.name not in retained:
            if not item.is_file():
                raise RuntimeError("unexpected document spool entry")
            item.unlink(missing_ok=True)
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def release_content(path: Path, state: str) -> None:
    receipt = read(path, "receipt.json")
    if not receipt.get("local_stopped") or not receipt.get("reaped"):
        raise HTTPException(409, "document stop is unconfirmed")
    if not (path / "released.json").exists():
        write_json(path / "released.json", {"status": state, "released_at": time.time()})
    remove_content(path)


@router.post("/{call_id}/ack")
async def acknowledge(call_id: str, body: Acknowledgement):
    # Service authentication is enforced for ALL /document/calls/* routes.
    path = call_path(call_id)
    with locked(path):
        if not (path / "accepted.json").exists():
            raise HTTPException(404, "document call not found")
        accepted = read(path, "accepted.json")
        if accepted["attempt_id"] != body.attempt_id or accepted["digest"] != body.digest:
            raise HTTPException(409, "document acknowledgement identity conflict")
        with (path / "execution.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HTTPException(409, "document execution still owned") from None
            if not (path / "receipt.json").exists():
                raise HTTPException(409, "document stop is unconfirmed")
            release_content(path, "released")
        return status(path)


def retention_pass() -> dict:
    """Bound ended/orphaned content; never evict an executing/unknown attempt."""
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    counts = {"removed": 0, "unconfirmed": 0, "failed": 0}
    now = time.time()
    for path in ROOT.iterdir():
        if not path.is_dir() or len(path.name) != 32:
            continue
        try:
            if uuid.UUID(path.name).hex != path.name:
                continue
            with locked(path), (path / "execution.lock").open("a+b") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                if (path / "released.json").exists():
                    remove_content(path)  # also finishes a crash during deletion
                    continue
                accepted_file = path / "accepted.json"
                receipt_file = path / "receipt.json"
                started = (path / "started.json").exists()
                # Updating a directory/lock mtime must never extend retention.
                files = [p for p in path.iterdir() if p.is_file() and p.name != "execution.lock"]
                since = min((p.stat().st_mtime for p in files), default=path.stat().st_mtime)
                if accepted_file.exists():
                    since = read(path, "accepted.json").get("accepted_at", accepted_file.stat().st_mtime)
                if receipt_file.exists():
                    since = read(path, "receipt.json").get("finished_at", receipt_file.stat().st_mtime)
                if now - since < CONTENT_RETENTION_SECONDS:
                    continue
                if started and not receipt_file.exists():
                    # status can prove loss of the old container, but never of
                    # a guardian in the same process namespace.
                    previous = read(path, "started.json").get("execution_identity")
                    if previous is not None and previous != execution_identity():
                        write_json(receipt_file, {"reason": "service_instance_gone",
                            "local_stopped": True, "reaped": True,
                            "provider_outcome": "unknown" if any(path.glob("provider-pending-*")) else "finished_or_not_started",
                            "finished_at": now})
                    else:
                        counts["unconfirmed"] += 1
                    continue
                if not receipt_file.exists():
                    # Execution lock is ours and no durable start exists.
                    write_json(receipt_file, {"reason": "retention_before_start",
                        "local_stopped": True, "reaped": True,
                        "provider_outcome": "not_started", "finished_at": now})
                if not accepted_file.exists():
                    write_json(accepted_file, {"call_id": path.name, "attempt_id": None,
                        "digest": None, "deadline_at": None, "tombstone": True})
                release_content(path, "expired")
                counts["removed"] += 1
        except Exception as exc:
            counts["failed"] += 1
            logger.error("document retention failed (%s)", type(exc).__name__)
    return counts


async def retention_loop() -> None:
    while True:
        try:
            counts = await asyncio.to_thread(retention_pass)
            if counts["failed"] or counts["unconfirmed"]:
                logger.error("document retention needs intervention: failed=%d unconfirmed=%d",
                             counts["failed"], counts["unconfirmed"])
        except Exception as exc:
            logger.error("document retention unavailable (%s)", type(exc).__name__)
        await asyncio.sleep(60)


@router.on_event("startup")
async def start_retention() -> None:
    global _retention_task
    _retention_task = asyncio.create_task(retention_loop())


@router.on_event("shutdown")
async def stop_retention() -> None:
    if _retention_task is not None:
        _retention_task.cancel()
        await asyncio.gather(_retention_task, return_exceptions=True)
