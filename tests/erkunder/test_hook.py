import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "docker/erkunder/erkunder-hook"
allowed = runpy.run_path(str(HOOK))["allowed"]


@pytest.fixture
def tree(tmp_path):
    cwd = tmp_path / "bericht-123" / "erkunder-1"
    cwd.mkdir(parents=True)
    (cwd.parent / "eingang").mkdir()
    return cwd, tmp_path


def check(tree, tool, path, **fields):
    cwd, root = tree
    key = "file_path" if tool in {"Read", "Write", "Edit"} else "path"
    return allowed(
        {"cwd": str(cwd), "tool_name": tool, "tool_input": {key: path, **fields}}, root
    )


@pytest.mark.parametrize("tool", ["Read", "Write", "Edit", "Glob", "Grep", "LS"])
def test_own_folder(tree, tool):
    assert check(tree, tool, "local.py")


@pytest.mark.parametrize("tool", ["Read", "Glob", "Grep", "LS"])
def test_read_entrance(tree, tool):
    assert check(tree, tool, "../eingang/daten.csv")


@pytest.mark.parametrize("tool", ["Write", "Edit"])
def test_cannot_write_entrance(tree, tool):
    assert not check(tree, tool, "../eingang/daten.csv")


@pytest.mark.parametrize("path", ["../lauf-2", "/app", "/etc/shadow"])
def test_escape(tree, path):
    assert not check(tree, "Read", path)


def test_symlink_escape(tree):
    cwd, root = tree
    (cwd / "link").symlink_to(root)
    assert not check(tree, "Read", "link/secrets")
    assert not check(tree, "Write", "link/new")
    assert not check(tree, "Glob", ".", pattern="link/*")


def test_glob_pattern_escape(tree):
    assert not check(tree, "Glob", ".", pattern="../lauf-2/*")


def test_denial_explanation():
    event = {
        "cwd": "/arbeit/bericht-123/erkunder-1",
        "tool_name": "Read",
        "tool_input": {"file_path": "/etc/shadow"},
    }
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        check=True,
    )
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert (
        output["permissionDecisionReason"]
        == "Pfad liegt außerhalb deines Arbeitsordners"
    )
