"""Protected execution checkpoints, not a second job queue.

A per-run flock prevents overlapping execution across workers sharing this
volume. A released lock with a started checkpoint means uncertain execution:
we return the recorded failure, never replay a possibly completed model call.
"""

import base64
import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

from .adapter import RunError
from .sandbox import canonical, sha


class Storage:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink() or root.stat().st_mode & 0o077:
            raise RunError("sandbox_fehler")

    def location(self, tenant, run_id):
        directory = self.root / sha(canonical([tenant, run_id]))
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_mode & 0o077:
            raise RunError("sandbox_fehler")
        return directory

    @contextmanager
    def lock(self, tenant, run_id):
        directory = self.location(tenant, run_id)
        fd = os.open(directory / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RunError("lauf_belegt", retryable=True, retry_after_s=1) from None
            yield directory
        finally:
            os.close(fd)

    @staticmethod
    def load(directory):
        path = directory / "state.json"
        if not path.exists():
            return None
        return json.loads(path.read_bytes())

    @staticmethod
    def save(directory, state):
        path = directory / "state.new"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(canonical(state))
            out.flush()
            os.fsync(out.fileno())
        os.replace(path, directory / "state.json")
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def archive(self, directory, attempt, state):
        """Keep prior usage, artifacts and diagnosis before accepting a retry.

        Called under the run lock. Repeating after a crash before state replacement
        writes the same checkpoint; it never loses the previous attempt.
        """
        archive = directory / "attempts" / str(attempt)
        archive.parent.mkdir(mode=0o700, exist_ok=True)
        archive.mkdir(mode=0o700, exist_ok=True)
        self.save(archive, state)

    def reference(self, tenant, run_id, phase, name):
        # Opaque resource id. Retrieval re-derives tenant scope; no host path.
        return (
            "/v1/research/"
            + run_id
            + "/content?lauf_artifact="
            + sha(canonical([tenant, run_id, phase, name]))
        )

    def artifact(self, tenant, run_id, reference):
        state = self.load(self.location(tenant, run_id))
        if not state:
            raise RunError("antwort_fehlt")
        for phase in ("hauptlauf", "pruefung"):
            if self.reference(tenant, run_id, phase, "diagnose").rsplit("=", 1)[-1] == reference:
                return canonical({"result": state["result"], "transcript": state["transcript"]})
        for item in state.get("blobs", {}).values():
            if item["ref"].rsplit("=", 1)[-1] == reference:
                data = base64.b64decode(item["data"], validate=True)
                if sha(data) != item["sha256"] or len(data) != item["bytes"]:
                    raise RunError("artefakt_integritaet")
                return data
        raise RunError("antwort_fehlt")
