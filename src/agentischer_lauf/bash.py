"""Server-owned computation place using the existing isolated runtime image.

The caller selects Bash, never an image, daemon, mount, environment or resource
profile. A fresh PID/network/IPC namespace belongs to exactly one tool call.
"""

from __future__ import annotations

import asyncio
import base64
import json
import hashlib
from pathlib import Path
import re
import tempfile
import uuid

from .adapter import RunError
from .models import safe_path


class BashPlace:
    def __init__(
        self,
        *,
        image: str,
        root: Path,
        timeout_s=30,
        work_bytes=32_000_000,
        docker="/usr/bin/docker",
        socket="unix:///var/run/docker.sock",
        max_parallel=3,
    ):
        # A reviewed local image, never a mutable tag or an implicit image pull.
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("computation image must be pinned by local image ID")
        if not 0 < timeout_s <= 480 or not 0 < work_bytes <= 256_000_000:
            raise ValueError("invalid computation limits")
        self.image, self.root = image, Path(root).resolve()
        self.timeout_s, self.work_bytes = timeout_s, work_bytes
        self.cli = [docker, "--host", socket]
        if not socket.startswith("unix://"):
            raise ValueError("computation requires a local dedicated Docker daemon")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        # Upper bound of computation containers of this process at once
        # (ABASHR condition 2): N runs otherwise mean N x 2 GiB / 1 CPU. Full
        # is a loud, repeatable 429, not a queue that waits unseen.
        if not 0 < max_parallel <= 16:
            raise ValueError("invalid computation parallelism")
        self.max_parallel = max_parallel
        self.belegt = 0

    async def _command(self, args, *, data=None, timeout=10, limit=1_000_000):
        process = await asyncio.create_subprocess_exec(
            *self.cli,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
        )

        async def read(stream):
            parts, size = [], 0
            while chunk := await stream.read(65536):
                size += len(chunk)
                if size > limit:
                    raise RunError("artefakt_integritaet")
                parts.append(chunk)
            return b"".join(parts)

        async def communicate():
            process.stdin.write(data or b"")
            await process.stdin.drain()
            process.stdin.close()
            readers = [asyncio.create_task(read(s)) for s in (process.stdout, process.stderr)]
            try:
                out, err = await asyncio.gather(*readers)
                await process.wait()
                return process.returncode, out, err
            finally:
                for reader in readers:
                    reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)

        try:
            return await asyncio.wait_for(communicate(), timeout)
        finally:
            if process.returncode is None:
                process.kill()

            # Killing a process does not drain paused asyncio pipe transports.
            # Consume remaining bytes without retaining them before waiting.
            async def discard(stream):
                while await stream.read(65536):
                    pass

            await asyncio.gather(discard(process.stdout), discard(process.stderr))
            await process.wait()

    async def run(self, *, command, material, files, outputs, max_bytes):
        if not isinstance(command, str) or not command.strip() or len(command) > 100000:
            raise RunError("werkzeug_fehler")
        for path in [*material, *files, *outputs]:
            safe_path(path)
        if self.belegt >= self.max_parallel:
            raise RunError("lauf_belegt", retryable=True, retry_after_s=5)
        self.belegt += 1
        try:
            return await self._run(command, material, files, outputs, max_bytes)
        finally:
            self.belegt -= 1

    async def _run(self, command, material, files, outputs, max_bytes):
        with tempfile.TemporaryDirectory(prefix="bash-", dir=self.root) as temporary:
            name = "agentic-bash-" + uuid.uuid4().hex
            args = [
                "create",
                "--name",
                name,
                # Removed by the daemon once its process ends, also when this
                # process dies hard before its own `rm` (ABASHR finding 4).
                "--rm",
                "--pull=never",
                "--interactive",
                "--network=none",
                "--ipc=private",
                "--read-only",
                "--memory=2g",
                "--memory-swap=2g",
                "--pids-limit=128",
                "--cpus=1",
                "--cap-drop=ALL",
                "--cap-add=CHOWN",
                "--cap-add=SETUID",
                "--cap-add=SETGID",
                "--security-opt=no-new-privileges:true",
                "--user=0:0",
                "--tmpfs",
                f"/work:rw,nosuid,nodev,size={self.work_bytes}",
                "--tmpfs",
                f"/scratch:rw,nosuid,nodev,noexec,size={self.work_bytes},mode=0700,uid=1101,gid=1100",
                "--workdir=/work",
                "--env=PYTHONPATH=/app",
                "--entrypoint=/app/.venv/bin/python",
                "--volume",
                f"{Path(__file__).with_name('bash_kind.py')}:/bash_kind.py:ro",
            ]
            for index, (path, content) in enumerate(material.items()):
                source = Path(temporary) / str(index)
                source.write_bytes(content)
                source.chmod(0o444)
                args += ["--volume", f"{source}:/work/{path}:ro"]
            args += [self.image, "/bash_kind.py"]
            payload = json.dumps(
                {
                    "command": command,
                    "material": {p: hashlib.sha256(b).hexdigest() for p, b in material.items()},
                    "outputs": outputs,
                    "files": {p: base64.b64encode(b).decode() for p, b in files.items()},
                    "max_bytes": max_bytes,
                    "timeout_s": self.timeout_s,
                }
            ).encode()
            try:
                code, _, _ = await self._command(args)
                if code:
                    raise RunError("sandbox_fehler")
                code, output, _ = await self._command(
                    ["start", "--attach", "--interactive", name],
                    data=payload,
                    timeout=self.timeout_s + 10,
                    limit=max_bytes * 8 + 1_000_000,
                )
                if code:
                    raise RunError("sandbox_fehler")
                result = json.loads(output)
                if result.get("error"):
                    raise RunError(result["error"])
                return result
            except (OSError, ValueError, TimeoutError):
                raise RunError("sandbox_fehler") from None
            finally:
                # Removing the namespace kills even setsid/double-fork descendants.
                # Shield cleanup from the runtime's overall deadline/cancellation.
                cleanup = asyncio.create_task(self._command(["rm", "--force", name]))
                try:
                    code, _, error = await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    code, _, error = await cleanup
                    if code and b"No such container" not in error:
                        raise RunError("sandbox_fehler") from None
                    raise
                if code and b"No such container" not in error:
                    raise RunError("sandbox_fehler")
