"""PID 1 owns adopted children; asyncio owns the direct child's exit status."""

from unittest.mock import AsyncMock, Mock

import pytest

from src.erkunder import aufraeumen


@pytest.fixture
def pid_one(monkeypatch):
    monkeypatch.setattr(aufraeumen.os, "getpid", lambda: 1)
    pause = AsyncMock()
    monkeypatch.setattr(aufraeumen.asyncio, "sleep", pause)
    return pause


async def test_reap_drains_all_children_and_waits_for_pending_exit(
    monkeypatch, pid_one
):
    wait = Mock(side_effect=[(41, 0), (42, 9), (0, 0), (43, 0), ChildProcessError()])
    monkeypatch.setattr(aufraeumen.os, "waitpid", wait)
    await aufraeumen.reap_children()
    assert wait.call_count == 5
    assert all(call.args == (-1, aufraeumen.os.WNOHANG) for call in wait.call_args_list)
    pid_one.assert_awaited_once_with(0.01)


async def test_reap_does_not_touch_children_outside_pid_namespace(monkeypatch):
    monkeypatch.setattr(aufraeumen.os, "getpid", lambda: 100)
    wait = Mock(side_effect=AssertionError("unrelated child"))
    monkeypatch.setattr(aufraeumen.os, "waitpid", wait)
    await aufraeumen.reap_children()
    wait.assert_not_called()


async def test_reap_pending_child_fails_loud_after_deadline(monkeypatch, pid_one):
    monkeypatch.setattr(aufraeumen.os, "waitpid", Mock(return_value=(0, 0)))
    with pytest.raises(RuntimeError, match="nicht erntbare Kinder"):
        await aufraeumen.reap_children()
    assert pid_one.await_count == 100


async def test_reap_unexpected_os_error_is_not_hidden(monkeypatch, pid_one):
    monkeypatch.setattr(aufraeumen.os, "waitpid", Mock(side_effect=PermissionError()))
    with pytest.raises(PermissionError):
        await aufraeumen.reap_children()


def test_running_reap_excludes_watcher_and_drains_thread_adoptees(
    monkeypatch, pid_one, tmp_path
):
    tasks = tmp_path / "self/task"
    for tid, children in [("1", "41 42"), ("9", "43 44")]:
        thread = tasks / tid
        thread.mkdir(parents=True)
        (thread / "children").write_text(children)
    wait = Mock(side_effect=[(42, 0), (0, 0), ChildProcessError()])
    monkeypatch.setattr(aufraeumen.os, "waitpid", wait)
    aufraeumen.reap_adopted_children(41, tmp_path)
    assert {call.args[0] for call in wait.call_args_list} == {42, 43, 44}
    assert all(call.args[1] == aufraeumen.os.WNOHANG for call in wait.call_args_list)


def test_running_reap_is_disabled_on_host(monkeypatch, tmp_path):
    monkeypatch.setattr(aufraeumen.os, "getpid", lambda: 100)
    wait = Mock(side_effect=AssertionError("unrelated child"))
    monkeypatch.setattr(aufraeumen.os, "waitpid", wait)
    aufraeumen.reap_adopted_children(41, tmp_path)
    wait.assert_not_called()


def test_running_reap_fails_loud(monkeypatch, pid_one, tmp_path):
    task = tmp_path / "self/task/1"
    task.mkdir(parents=True)
    (task / "children").write_text("42")
    monkeypatch.setattr(aufraeumen.os, "waitpid", Mock(side_effect=PermissionError()))
    with pytest.raises(PermissionError):
        aufraeumen.reap_adopted_children(41, tmp_path)


@pytest.mark.parametrize("missing", [False, True])
def test_startup_requires_readable_children(monkeypatch, tmp_path, missing):
    monkeypatch.setattr(aufraeumen.threading, "get_native_id", lambda: 17)
    task = tmp_path / "self/task/17"
    task.mkdir(parents=True)
    if missing:
        with pytest.raises(
            RuntimeError, match="Platz nicht bereit.*CONFIG_PROC_CHILDREN"
        ):
            aufraeumen.require_proc_children(tmp_path)
    else:
        (task / "children").write_text("")
        aufraeumen.require_proc_children(tmp_path)


def test_running_reap_missing_children_fails_loud(pid_one, tmp_path):
    (tmp_path / "self/task/1").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="Platz-Ernte.*CONFIG_PROC_CHILDREN"):
        aufraeumen.reap_adopted_children(41, tmp_path)


def test_running_reap_tolerates_exited_thread(monkeypatch, pid_one, tmp_path):
    tasks = tmp_path / "self/task"
    tasks.mkdir(parents=True)
    path_type = type(tmp_path)
    original = path_type.iterdir
    monkeypatch.setattr(
        path_type, "iterdir",
        lambda path: iter([tasks / "9"]) if path == tasks else original(path),
    )
    wait = Mock(side_effect=AssertionError("exited thread has no children"))
    monkeypatch.setattr(aufraeumen.os, "waitpid", wait)
    aufraeumen.reap_adopted_children(41, tmp_path)
    wait.assert_not_called()


@pytest.mark.parametrize("error", [ProcessLookupError, PermissionError, OSError])
def test_running_reap_read_error(monkeypatch, pid_one, tmp_path, error):
    task = tmp_path / "self/task/1"
    task.mkdir(parents=True)
    monkeypatch.setattr(type(tmp_path), "read_text", Mock(side_effect=error()))
    if error is ProcessLookupError:
        aufraeumen.reap_adopted_children(41, tmp_path)
    else:
        with pytest.raises(error):
            aufraeumen.reap_adopted_children(41, tmp_path)
