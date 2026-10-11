"""Mediated file sandbox. No host paths, processes, network or credentials.

Materials are immutable bytes; only explicit output paths are writable. Bash
and external tools require a separately isolated adapter and are unavailable.
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
    def __init__(self, material, tools, outputs, max_bytes):
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
            if tool.name not in ("Read", "Write", "List") or tool.parameter != {}:
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
