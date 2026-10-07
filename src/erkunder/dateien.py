"""Bounded descriptor-based reads across the model/root trust boundary."""

import os
import stat
from pathlib import Path

MAX_FILE_BYTES = 5_000_000


def read_bytes(path: Path, *, limit: int = MAX_FILE_BYTES) -> bytes:
    """Reject links in every component, special files and oversized/growing files."""
    path = path.absolute()
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    finally:
        os.close(directory)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("keine regulaere Datei")
        if info.st_size > MAX_FILE_BYTES:
            raise ValueError("ergebnis zu gross")
        data = source.read(min(limit, MAX_FILE_BYTES) + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("ergebnis zu gross")
        return data


def read_text(path: Path) -> str:
    return read_bytes(path).decode("utf-8")
