import os

import pytest

from src.erkunder.dateien import MAX_FILE_BYTES, read_bytes, read_text


@pytest.mark.parametrize("kind", ["symlink", "parent-link", "fifo", "large"])
def test_untrusted_files_rejected(tmp_path, kind):
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "ergebnis.md").write_text("other report")
    directory = tmp_path / "step"
    if kind == "parent-link":
        directory.symlink_to(outside, target_is_directory=True)
    else:
        directory.mkdir()
    path = directory / "ergebnis.md"
    if kind == "symlink":
        path.symlink_to(outside / "ergebnis.md")
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "large":
        with path.open("wb") as file:
            file.truncate(MAX_FILE_BYTES + 1)
    with pytest.raises((OSError, ValueError)):
        read_text(path)
    assert (outside / "ergebnis.md").read_text() == "other report"


def test_opened_descriptor_survives_path_swap(tmp_path, monkeypatch):
    path = tmp_path / "ergebnis.md"
    path.write_text("own report")
    outside = tmp_path / "private"
    outside.write_text("other report")
    fstat = os.fstat

    def swap(fd):
        path.unlink()
        path.symlink_to(outside)
        return fstat(fd)

    monkeypatch.setattr(os, "fstat", swap)
    assert read_text(path) == "own report"


def test_growth_after_fstat_is_bounded(tmp_path, monkeypatch):
    path = tmp_path / "ergebnis.md"
    path.write_bytes(b"x")
    fstat = os.fstat

    def grow(fd):
        info = fstat(fd)
        with path.open("ab") as file:
            file.truncate(MAX_FILE_BYTES + 1)
        return info

    monkeypatch.setattr(os, "fstat", grow)
    with pytest.raises(ValueError, match="zu gross"):
        read_bytes(path)


def test_search_only_parents_in_unprivileged_process(tmp_path):
    # A subprocess makes the permission boundary real, rather than mocking open.
    import subprocess
    import sys

    if os.geteuid() == 0:
        pytest.skip("Root bypasses DAC; use the container probe for root-owned 0711")
    directory = tmp_path / "arbeit" / "bericht"
    directory.mkdir(parents=True)
    path = directory / "ergebnis.md"
    path.write_text("synthetic result")
    directory.chmod(0o111)
    directory.parent.chmod(0o111)
    try:
        result = subprocess.run(
            [sys.executable, "-c", (
                "from pathlib import Path; import os, sys; "
                "from src.erkunder.dateien import read_text; "
                "assert os.geteuid() != 0; "
                "assert read_text(Path(sys.argv[1])) == 'synthetic result'"
            ), str(path)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
    finally:
        directory.parent.chmod(0o700)
        directory.chmod(0o700)
