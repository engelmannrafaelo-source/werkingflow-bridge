"""Mediated files and explicitly provisioned isolated computation.

Materials are immutable bytes; only explicit output paths are published. Bash
requires a server-owned isolated computation place and an explicit tool grant.
"""

import hashlib
import json
from .adapter import RunError
from .models import safe_path


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


class Sandbox:
    def __init__(self, material, tools, outputs, max_bytes, *, bash=None):
        self.bash = bash
        self.material = dict(material)
        self.files = {}
        self.tools = {t.name: t for t in tools}
        self.outputs = outputs
        self.max_bytes = max_bytes
        self.validate_tools(tools)

    @staticmethod
    def validate_tools(tools):
        if len({t.name for t in tools}) != len(tools):
            raise RunError("auftrag_ungueltig")
        for tool in tools:
            if tool.name not in ("Read", "Write", "List", "Bash") or tool.parameter != {}:
                raise RunError("faehigkeit_nicht_verfuegbar")

    def read(self, path):
        safe_path(path)
        if path in self.files:
            return self.files[path]
        if path in self.material:
            return self.material[path]
        raise RunError("antwort_fehlt")

    def call(self, name, args):
        if name not in self.tools or not isinstance(args, dict):
            raise RunError("werkzeug_fehler")
        if name == "Bash":
            raise RunError("werkzeug_fehler")  # use the cancellable async entry
        if name == "List":
            if args:
                raise RunError("werkzeug_fehler")
            return sorted(set(self.material) | set(self.files))
        if set(args) != ({"path", "content"} if name == "Write" else {"path"}):
            raise RunError("werkzeug_fehler")
        path = safe_path(args["path"])
        if name == "Read":
            return self.read(path).decode("utf-8")
        if (
            not isinstance(args["content"], str)
            or path in self.material
            or any(path.startswith(p + "/") or p.startswith(path + "/") for p in self.material)
            or not any(path == p or path.startswith(p + "/") for p in self.outputs)
        ):
            raise RunError("werkzeug_fehler")
        data = args["content"].encode()
        if sum(len(v) for k, v in self.files.items() if k != path) + len(data) > self.max_bytes:
            raise RunError("artefakt_integritaet")
        if any(
            p != path and (p.startswith(path + "/") or path.startswith(p + "/")) for p in self.files
        ):
            raise RunError("werkzeug_fehler")
        self.files[path] = data
        return {"path": path, "sha256": sha(data), "bytes": len(data)}

    async def acall(self, name, args):
        if name != "Bash":
            return self.call(name, args)
        if name not in self.tools or not isinstance(args, dict) or set(args) != {"command"}:
            raise RunError("werkzeug_fehler")
        if self.bash is None:
            raise RunError("faehigkeit_nicht_verfuegbar")
        result = await self.bash.run(
            command=args["command"],
            material=self.material,
            files=self.files,
            outputs=self.outputs,
            max_bytes=self.max_bytes,
        )
        # Revalidate the whole returned snapshot at the mediated boundary. Bash
        # permission includes calculation files, but never undeclared artifacts.
        import base64

        candidate = Sandbox(self.material, [], self.outputs, self.max_bytes)
        for path, encoded in result["files"].items():
            data = base64.b64decode(encoded, validate=True)
            safe_path(path)
            if (
                path in self.material
                or any(path.startswith(p + "/") or p.startswith(path + "/") for p in self.material)
                or not any(path == p or path.startswith(p + "/") for p in self.outputs)
                or sum(map(len, candidate.files.values())) + len(data) > self.max_bytes
            ):
                raise RunError("artefakt_integritaet")
            candidate.files[path] = data
        self.files = candidate.files
        return {key: result[key] for key in ("exit_code", "stdout", "stderr")}

    def artifacts(self, targets):
        selected = {}
        for target in targets:
            matches = {
                p: b for p, b in self.files.items() if p == target or p.startswith(target + "/")
            }
            if not matches:
                raise RunError("antwort_fehlt")
            selected.update(matches)
        return selected
