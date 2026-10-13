"""Credential-free child inside the computation place; no model invocation."""

import asyncio
import base64
import json
import hashlib
import math
import os
from pathlib import Path
import resource
import signal
import stat
import sys

from src.erkunder.aufraeumen import stop_uid_processes, reap_children
from src.erkunder.prozessschutz import protect_process


def prepare(body):
    work = Path("/work")
    # Mount parents are initially root-owned. Prepare them before permanently
    # dropping bootstrap UID/capabilities; input mounts themselves stay read-only.
    folders = {work}
    for path in [*body["material"], *body["files"]]:
        parent = (work / path).parent
        while parent != work:
            folders.add(parent)
            parent = parent.parent
    for folder in sorted(folders, key=lambda p: len(p.parts)):
        folder.mkdir(parents=True, exist_ok=True)
    for folder in sorted(folders, key=lambda p: len(p.parts), reverse=True):
        folder.chmod(0o755)
        os.chown(folder, 1101, 1100)
    os.setgroups([])
    os.setgid(1100)
    os.setuid(1101)
    protect_process()
    for folder in folders:
        folder.chmod(0o700)
    for path, encoded in body["files"].items():
        (work / path).write_bytes(base64.b64decode(encoded, validate=True))
    return work


def limits(body):
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (body["max_bytes"], body["max_bytes"]))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    cpu = max(1, math.ceil(body["timeout_s"]))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))


async def run(body):
    work = prepare(body)
    limits(body)
    process = await asyncio.create_subprocess_exec(
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-c",
        body["command"],
        cwd=work,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        env={
            "PATH": "/opt/rechnen/bin:/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HOME": "/scratch",
            "TMPDIR": "/scratch",
            "MPLCONFIGDIR": "/scratch/mpl",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    total = 0

    async def bounded(stream):
        nonlocal total
        parts = []
        while chunk := await stream.read(65536):
            total += len(chunk)
            if total > body["max_bytes"]:
                raise ValueError("output limit")
            parts.append(chunk)
        return b"".join(parts).decode("utf-8")

    async def collect():
        readers = [asyncio.create_task(bounded(s)) for s in (process.stdout, process.stderr)]
        try:
            out, err = await asyncio.gather(*readers)
            await process.wait()
            return out, err
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

    try:
        stdout, stderr = await asyncio.wait_for(collect(), body["timeout_s"])
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await stop_uid_processes()

        async def discard(stream):
            while await stream.read(65536):
                pass

        await asyncio.gather(discard(process.stdout), discard(process.stderr))
        await process.wait()
        await reap_children()
    # Parent directory moves must not shadow a read-only input mount.
    for path, expected in body["material"].items():
        target = work / path
        if not stat.S_ISREG(target.lstat().st_mode):
            raise ValueError("material identity")
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise ValueError("material identity")
    files, size, count = {}, 0, 0
    pending = [work]
    while pending:
        folder = pending.pop()
        for entry in folder.iterdir():
            count += 1
            if count > 4096:
                raise ValueError("file count")
            path = entry.relative_to(work).as_posix()
            info = entry.lstat()
            if path in body["material"]:
                continue  # kernel-enforced read-only bind mount
            if stat.S_ISDIR(info.st_mode):
                pending.append(entry)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("non-regular output")
            if not any(path == p or path.startswith(p + "/") for p in body["outputs"]):
                raise ValueError("undeclared output")
            if info.st_size > body["max_bytes"] - size:
                raise ValueError("artifact limit")
            content = entry.read_bytes()  # all producers killed/reaped above
            size += len(content)
            if size > body["max_bytes"]:
                raise ValueError("artifact limit")
            files[path] = base64.b64encode(content).decode()
    return {"exit_code": process.returncode, "stdout": stdout, "stderr": stderr, "files": files}


if __name__ == "__main__":
    try:
        result = asyncio.run(run(json.load(sys.stdin)))
    except TimeoutError:
        result = {"error": "grenze_zeit"}
    except Exception:
        result = {"error": "sandbox_fehler"}
    print(json.dumps(result))
