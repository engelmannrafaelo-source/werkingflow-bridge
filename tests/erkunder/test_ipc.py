"""Host tests use synthetic inventories; never remove the host's IPC objects."""

import ctypes
from unittest.mock import Mock

import pytest

from src.erkunder import ipc


def test_ipc_removes_only_owner_or_creator_objects(tmp_path, monkeypatch):
    shm, mq, inventory = [tmp_path / name for name in ("shm", "mq", "sysvipc")]
    for directory in (shm, mq, inventory):
        directory.mkdir()
    (shm / "secret").write_text("A")
    (mq / "queue").write_text("A")
    uid = ipc.os.getuid()
    for kind, ident in (("shm", "shmid"), ("sem", "semid"), ("msg", "msqid")):
        (inventory / kind).write_text(
            f"{ident} uid cuid\n1 {uid} {uid}\n2 98765 {uid}\n3 98765 98765\n"
        )
    libc = Mock()
    for function in (libc.shmctl, libc.semctl, libc.msgctl):
        function.return_value = 0
    monkeypatch.setattr(ipc.ctypes, "CDLL", lambda *a, **kw: libc)
    ipc.clear_owned_ipc(shm, mq, inventory)
    assert not list(shm.iterdir()) and not list(mq.iterdir())
    for function in (libc.shmctl, libc.semctl, libc.msgctl):
        assert [call.args[0] for call in function.call_args_list] == [1, 2]
    libc.shmctl.return_value = -1
    ctypes.set_errno(1)
    with pytest.raises(PermissionError, match="Platz-IPC"):
        ipc.clear_owned_ipc(shm, mq, inventory)
