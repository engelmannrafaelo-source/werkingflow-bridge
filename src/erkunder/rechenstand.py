"""Rechenstand einer Gutachtenfassung für den nächsten Schritt.

Rechenstand = `skripte/` samt Ergebnisdateien.
"""

from __future__ import annotations

import os
from pathlib import Path

from .dateien import MAX_FILE_BYTES, read_bytes

MAX_RECHENSTAND_BYTES = 4 * MAX_FILE_BYTES


def rechenstand(directory: Path, ziel: str) -> tuple[dict[str, bytes], list[str]]:
    """Copy every regular file under `directory/skripte` to `ziel + skripte/...`.

    Same bounded, link-free reads as every other step input. Whatever cannot
    travel (missing folder, link, special file, size) comes back as a finding,
    so the reviewer and the open findings name it instead of a silent gap.
    """
    root = directory / "skripte"
    label = f"{directory.name}/skripte/"
    if root.is_symlink() or not root.is_dir():
        return {}, [f"Rechenstand fehlt: {label} ist kein Ordner der Fassung"]
    files: dict[str, bytes] = {}
    findings: list[str] = []
    budget = MAX_RECHENSTAND_BYTES
    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                path = Path(entry.path)
                relative = path.relative_to(directory).as_posix()
                if entry.is_symlink():
                    findings.append(
                        f"Rechenstand nicht übergeben: {relative} (Verknüpfung)"
                    )
                elif entry.is_dir(follow_symlinks=False):
                    # Python's byte cache is derived, not part of the calculation.
                    if entry.name != "__pycache__":
                        pending.append(path)
                elif not entry.is_file(follow_symlinks=False):
                    findings.append(
                        f"Rechenstand nicht übergeben: {relative}"
                        " (keine reguläre Datei)"
                    )
                else:
                    try:
                        data = read_bytes(path)
                    except (OSError, ValueError) as error:
                        findings.append(
                            f"Rechenstand nicht übergeben: {relative} ({error})"
                        )
                        continue
                    if len(data) > budget:
                        findings.append(
                            f"Rechenstand nicht übergeben: {relative}"
                            " (Gesamtgröße überschritten)"
                        )
                        continue
                    budget -= len(data)
                    files[ziel + relative] = data
    if not files and not findings:
        findings.append(f"Rechenstand fehlt: {label} enthält keine Dateien")
    return files, findings
