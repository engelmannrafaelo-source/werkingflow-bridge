"""Remove step-owned IPC only inside a dedicated place IPC namespace.

No new privileges/user namespaces are granted to model tools. The place kills
and reaps its children before calling this; no producer can race the inventory.
"""

import ctypes
import os
from pathlib import Path

from .aufraeumen import clear_owned_tmp


def clear_owned_ipc(
    shm: Path = Path("/dev/shm"),
    mq: Path = Path("/dev/mqueue"),
    sysvipc: Path = Path("/proc/sysvipc"),
) -> None:
    clear_owned_tmp(shm)
    clear_owned_tmp(mq)  # unlink on mqueuefs is mq_unlink(3).
    libc = ctypes.CDLL(None, use_errno=True)
    uid = str(os.getuid())
    for kind, id_column, function in (
        ("shm", "shmid", "shmctl"),
        ("msg", "msqid", "msgctl"),
        ("sem", "semid", "semctl"),
    ):
        lines = (sysvipc / kind).read_text().splitlines()
        columns = lines[0].split()
        for line in lines[1:]:
            row = dict(zip(columns, line.split(), strict=True))
            if uid not in (row["uid"], row["cuid"]):
                continue
            ident = int(row[id_column])
            # IPC_RMID=0; semctl additionally takes a semnum before cmd.
            args = (ident, 0, 0) if kind == "sem" else (ident, 0, None)
            if getattr(libc, function)(*args) != 0:
                error = ctypes.get_errno()
                raise OSError(error, f"Platz-IPC: {function} fehlgeschlagen")
