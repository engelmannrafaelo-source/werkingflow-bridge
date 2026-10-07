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
