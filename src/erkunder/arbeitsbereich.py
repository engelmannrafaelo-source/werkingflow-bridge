"""Copy-only text inputs and a private mount namespace for every SDK start."""

import os
import stat
import tempfile
from pathlib import Path

from src.erkunder.dateien import read_bytes
from src.routing.bild_policy import ImageEgressRefused
from src.routing.text_material import require_text_bytes

# Data interchange and source text, never original document/container formats.
TEXT_SUFFIXES = frozenset(
    {".txt", ".md", ".csv", ".tsv", ".json", ".py", ".yaml", ".yml"}
)
LIMIT = 128 * 1024 * 1024


def text_inventory(root: Path) -> dict[Path, bytes]:
    result = {}
    try:
        if root.is_symlink() or root.resolve() != root.absolute():
            raise ValueError("linked directory")
        if not root.is_dir():
            raise ValueError("missing directory")
        for path in root.rglob("*"):
            if any(part.startswith(".") for part in path.relative_to(root).parts):
                raise ValueError("hidden state is not an input derivation")
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("link or special file")
            if path.suffix.lower() not in TEXT_SUFFIXES:
                raise ValueError("not a text derivation")
            data = read_bytes(path, limit=LIMIT, max_bytes=LIMIT)
            require_text_bytes(data)
            result[path.relative_to(root)] = data
    except (OSError, UnicodeError, ValueError) as exc:
        raise ImageEgressRefused(
            "Erkunder-Arbeitsbereich ist keine reine Textkopie; neu vorbereiten.",
            code="image_tool_access_refused",
        ) from exc
    return result


def fresh_copy(step: Path, destination: Path) -> Path:
    """Validate BOTH old step and input before creating a new, unlinked tree.

    Destination is private place storage, never nested in the source workspace.
    No resumed CLI homes or hidden artifacts are carried into a new process.
    """
    sources = {step.name: text_inventory(step)}
    entry = step.parent / "eingang"
    if entry.exists() or entry.is_symlink():
        sources["eingang"] = text_inventory(entry)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    fresh = Path(tempfile.mkdtemp(prefix="attempt-", dir=destination))
    for name, files in sources.items():
        (fresh / name).mkdir(mode=0o700)
        for relative, data in files.items():
            target = fresh / name / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("xb") as stream:
                stream.write(data)
    text_inventory(fresh)  # complete tree, including all names and magic bytes
    return fresh


def isolated_command(fresh: Path, original_step: Path, command: list[str]) -> list[str]:
    """No bind of /, /arbeit, host homes or any ancestor of the step.

    bwrap is mandatory. Failure to establish mount/PID namespaces is fatal.
    The trusted runtime image is read-only; only this attempt's text copy is rw.
    """
    args = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CAP_SETUID",
        "--cap-add",
        "CAP_SETGID",
        "--cap-add",
        "CAP_CHOWN",
        "--cap-add",
        "CAP_DAC_OVERRIDE",
        "--clearenv",
    ]
    for name in (
        "PATH",
        "LANG",
        "PYTHONPATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    ):
        if name in os.environ:
            args += ["--setenv", name, os.environ[name]]
    for directory in ("/usr", "/lib", "/lib64", "/bin", "/opt", "/app"):
        if Path(directory).exists():
            args += ["--ro-bind", directory, directory]
    args += ["--perms", "0755", "--dir", "/etc"]
    for path in ("/etc/ssl", "/etc/resolv.conf", "/etc/hosts", "/etc/erkunder"):
        if Path(path).exists():
            args += ["--ro-bind", path, path]
    for ancestor in reversed(original_step.parent.parents):
        if ancestor != Path("/"):
            args += ["--perms", "0755", "--dir", str(ancestor)]
    args += [
        "--perms",
        "1777",
        "--tmpfs",
        "/tmp",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--bind",
        str(fresh),
        str(original_step.parent),
        "--chdir",
        str(original_step),
        "--",
        "/app/.venv/bin/python",
        "-m",
        "src.erkunder.unprivileged",
        str(original_step.parent),
        *command,
    ]
    return args
