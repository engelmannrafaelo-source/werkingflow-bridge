import os

import pytest

from src.erkunder.arbeitsbereich import fresh_copy, isolated_command, text_inventory
from src.routing.bild_policy import ImageEgressRefused


def prepared(tmp_path):
    step = tmp_path / "report" / "step"
    step.mkdir(parents=True)
    entry = step.parent / "eingang"
    entry.mkdir()
    (entry / "readings.csv").write_text("timestamp,value\n0,1\n")
    (step / "auftrag.md").write_text("Analyse the measurements.")
    return step


@pytest.mark.parametrize(
    "case", ["pdf", "disguised", "symlink", "hardlink", "unknown", "parent"]
)
def test_whole_tree_rejects_originals_and_links(tmp_path, case):
    step = prepared(tmp_path)
    raw = b"%PDF-1.4\n1 0 obj <</Subtype /Image>> endobj\n%%EOF"
    outside = tmp_path / "original.pdf"
    outside.write_bytes(raw)
    if case == "symlink":
        (step / "data.txt").symlink_to(outside)
    elif case == "hardlink":
        os.link(outside, step / "data.txt")
    elif case == "unknown":
        (step / "artifact.dat").write_text("text")
    elif case == "parent":
        (step / "up").symlink_to(step.parent, target_is_directory=True)
    else:
        (step / ("old.pdf" if case == "pdf" else "old.txt")).write_bytes(raw)
    with pytest.raises(ImageEgressRefused):
        fresh_copy(step, tmp_path / "attempts")
    assert outside.read_bytes() == raw  # nothing deleted


def test_every_start_is_a_new_independent_copy(tmp_path):
    step = prepared(tmp_path)
    first = fresh_copy(step, tmp_path / "attempts")
    second = fresh_copy(step, tmp_path / "attempts")
    assert first != second
    for relative in text_inventory(first):
        target = first / relative
        assert target.stat().st_nlink == 1
        original = step.parent / relative
        assert target.stat().st_ino != original.stat().st_ino
    command = isolated_command(first, step, ["python", "-m", "src.erkunder.kind"])
    assert command[command.index("--bind") + 1 : command.index("--bind") + 3] == [
        str(first),
        str(step.parent),
    ]
    assert str(step) not in command[: command.index("--chdir")]
    assert "--unshare-pid" in command
