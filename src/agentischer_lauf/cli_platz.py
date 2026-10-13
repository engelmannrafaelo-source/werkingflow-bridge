"""One isolated CLI run on the existing place isolation, without app vocabulary.

The CLI drives its own tool loop. This runner only prepares a fresh run folder,
starts the CLI in its own process group with a minimal environment, hands every
stream-json line to an observer which may stop the run, and afterwards reads
the declared outputs through the bounded descriptor reads of the place.

Layout is the one the place hook already enforces (``/arbeit/<lauf>/<cwd>``,
read-only ``eingang`` beside the working directory), so the same
``/etc/erkunder/settings.json`` hook applies unchanged.

``dedicated_uid=True`` adds the place sweep (all processes of this UID, adopted
children, owned /tmp and IPC). Only valid where the UID belongs to the place
alone, as in the place container; elsewhere the process group is the boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.erkunder.aufraeumen import (
    clear_owned_tmp,
    reap_children,
    remove_tree,
    require_proc_children,
    stop_uid_processes,
)
from src.erkunder.dateien import read_bytes
from src.erkunder.ipc import clear_owned_ipc
from src.erkunder.kind import DISALLOWED_TOOLS
from src.erkunder.platz import contains_secret

from .adapter import RunError
from .models import safe_path

# Same pass-through set as the place; nothing else of the host environment.
DURCHGEREICHT = {
    "PATH",
    "LANG",
    "LC_ALL",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
}
ZEILE_MAX = 16_000_000
# Whole stream kept per run (ACLIR P3): lines are held until the run ends, so
# without a total a run could gather 16 MB lines until its time limit — past
# the 2 GB per process the server allows. Beyond it the run ends loudly.
STROM_MAX = 64_000_000


@dataclass
class CliAuftrag:
    schluessel: str
    prompt: str
    systemtext: str
    model: str
    max_turns: int
    max_tokens: int
    werkzeuge: list[str]
    material: dict[str, bytes]
    ausgaben: list[str]
    zeit_s: float
    zugang: str = field(repr=False)


@dataclass
class CliAusgang:
    zeilen: list[dict]
    returncode: int | None
    dateien: dict[str, bytes]


class CliPlatz:
    def __init__(
        self,
        root: Path,
        *,
        befehl: list[str],
        settings: Path | None = Path("/etc/erkunder/settings.json"),
        zugang_variable: str = "CLAUDE_CODE_OAUTH_TOKEN",
        zusatz_env: dict[str, str] | None = None,
        dedicated_uid: bool = False,
        max_bytes: int = 5_000_000,
        max_strom_bytes: int = STROM_MAX,
    ):
        self.root = Path(root)
        self.befehl = list(befehl)
        self.settings = settings
        self.zugang_variable = zugang_variable
        self.zusatz_env = dict(zusatz_env or {})
        self.dedicated_uid = dedicated_uid
        self.max_bytes = max_bytes
        if not 0 < max_strom_bytes <= STROM_MAX:
            raise ValueError("invalid stream limit")
        self.max_strom_bytes = max_strom_bytes
        self.lock = asyncio.Lock()

    def bereit(self) -> None:
        """Fail before admission when the isolation this runner promises is absent."""
        if self.settings is not None and not self.settings.is_file():
            raise RunError("faehigkeit_nicht_verfuegbar")
        if not self.root.is_dir() or self.root.is_symlink():
            raise RunError("faehigkeit_nicht_verfuegbar")
        if self.dedicated_uid:
            try:
                require_proc_children()
            except RuntimeError:
                raise RunError("faehigkeit_nicht_verfuegbar") from None

    def befehlszeile(self, auftrag: CliAuftrag) -> list[str]:
        tools = ",".join(auftrag.werkzeuge)
        line = self.befehl + [
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            # message_start / message_delta carry the usage of every answer.
            "--include-partial-messages",
            "--max-turns",
            str(auftrag.max_turns),
            "--model",
            auftrag.model,
            # --tools is the available set; allowed/denied only add permissions.
            "--tools",
            tools,
            "--allowedTools",
            tools,
            "--disallowedTools",
            ",".join(t for t in DISALLOWED_TOOLS if t not in auftrag.werkzeuge),
            "--permission-mode",
            "bypassPermissions",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
        ]
        if self.settings is not None:
            line += ["--settings", str(self.settings)]
        if auftrag.systemtext:
            line += ["--append-system-prompt", auftrag.systemtext]
        return line

    def umgebung(self, auftrag: CliAuftrag, home: Path) -> dict[str, str]:
        return {
            **{k: v for k, v in os.environ.items() if k in DURCHGEREICHT},
            **self.zusatz_env,
            "HOME": str(home),
            "TMPDIR": str(home / "tmp"),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(auftrag.max_tokens),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            # A few transport retries; a long overload ends as kapazitaet_belegt
            # instead of eating the whole time limit in backoff.
            "CLAUDE_CODE_MAX_RETRIES": "2",
            self.zugang_variable: auftrag.zugang,
        }

    def vorbereiten(self, auftrag: CliAuftrag) -> tuple[Path, Path, Path]:
        safe_path(auftrag.schluessel)
        if "/" in auftrag.schluessel:
            raise RunError("sandbox_fehler")
        ordner = self.root / auftrag.schluessel
        ordner.mkdir(mode=0o700)
        eingang, arbeit = ordner / "eingang", ordner / "lauf"
        eingang.mkdir(mode=0o700)
        arbeit.mkdir(mode=0o700)
        for ziel, data in auftrag.material.items():
            path = eingang / safe_path(ziel)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
            with os.fdopen(fd, "wb") as out:
                out.write(data)
        # Material stays immutable for the model: no write bit on any entry.
        for directory, _, _ in os.walk(eingang, topdown=False):
            os.chmod(directory, 0o500)
        home = arbeit / ".home"
        (home / "tmp").mkdir(mode=0o700, parents=True)
        (home / ".claude").mkdir(mode=0o700)
        return ordner, arbeit, home

    async def lauf(self, auftrag: CliAuftrag, beobachter) -> CliAusgang:
        """Run once. ``beobachter(line)`` raising RunError stops the run loudly."""
        async with self.lock:
            return await self._lauf(auftrag, beobachter)

    async def _lauf(self, auftrag, beobachter):
        self.bereit()
        ordner, arbeit, home = self.vorbereiten(auftrag)
        try:
            process = None
            zeilen: list[dict] = []
            try:
                process = await asyncio.create_subprocess_exec(
                    *self.befehlszeile(auftrag),
                    cwd=arbeit,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                    env=self.umgebung(auftrag, home),
                    limit=ZEILE_MAX,
                )
                process.stdin.write(auftrag.prompt.encode())
                await process.stdin.drain()
                process.stdin.close()
                ende = time.monotonic() + auftrag.zeit_s
                strom_bytes = 0
                while True:
                    rest = ende - time.monotonic()
                    if rest <= 0:
                        raise RunError("grenze_zeit")
                    try:
                        raw = await asyncio.wait_for(process.stdout.readline(), rest)
                    except asyncio.TimeoutError:
                        raise RunError("grenze_zeit") from None
                    except ValueError:
                        raise RunError("protokoll_unvollstaendig") from None
                    if not raw:
                        break
                    strom_bytes += len(raw)
                    if strom_bytes > self.max_strom_bytes:
                        raise RunError("protokoll_unvollstaendig")
                    try:
                        line = json.loads(raw)
                    except ValueError:
                        raise RunError("protokoll_unvollstaendig") from None
                    if not isinstance(line, dict):
                        raise RunError("protokoll_unvollstaendig")
                    zeilen.append(line)
                    beobachter(line)
            finally:
                # Also on a limit stop: nothing of the run may outlive it.
                await self.beenden(process)
            return CliAusgang(zeilen, process.returncode, self.ausgaben(arbeit, auftrag))
        finally:
            remove_tree(ordner)

    async def beenden(self, process) -> None:
        if process is not None and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # The process group already exited.
        if self.dedicated_uid:
            await stop_uid_processes()
        if process is not None:
            await process.wait()
        if self.dedicated_uid:
            await reap_children()
            clear_owned_tmp()
            clear_owned_ipc()

    def ausgaben(self, arbeit: Path, auftrag: CliAuftrag) -> dict[str, bytes]:
        """Declared outputs only; links, special files and secrets fail loudly."""
        token = auftrag.zugang.encode()
        found: dict[str, bytes] = {}
        for target in auftrag.ausgaben:
            path = arbeit / safe_path(target)
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                continue  # Absence is judged by the run contract (antwort_fehlt).
            if stat.S_ISDIR(info.st_mode):
                files = []
                for directory, dirs, names in os.walk(path):
                    for name in dirs + names:
                        mode = os.lstat(Path(directory) / name).st_mode
                        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                            raise RunError("artefakt_integritaet")
                    files += [Path(directory) / name for name in names]
            elif stat.S_ISREG(info.st_mode):
                files = [path]
            else:
                raise RunError("artefakt_integritaet")
            for file in files:
                try:
                    data = read_bytes(file, limit=self.max_bytes)
                    if contains_secret(file, token):
                        raise RunError("sandbox_fehler")
                except (OSError, ValueError):
                    raise RunError("artefakt_integritaet") from None
                found[file.relative_to(arbeit).as_posix()] = data
        if sum(len(v) for v in found.values()) > self.max_bytes:
            raise RunError("artefakt_integritaet")
        return found
