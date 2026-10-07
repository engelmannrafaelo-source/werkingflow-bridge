"""Linux process-memory boundary between the place server and same-UID agents."""

import ctypes
import os
import sys

PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3


def protect_process() -> None:
    """Deny ptrace/proc memory access before accepting work; fail closed.

    No capabilities or filesystem writes are needed. Removing an environment
    variable alone does not erase the initial environment exposed by /proc.
    The server must not exec or change credentials after this call: Linux can
    reset dumpability on those transitions. Children receive a separate,
    explicitly allowlisted environment when exec'd by Platz.
    """
    if sys.platform != "linux":
        raise RuntimeError("Platz-Prozessschutz benoetigt Linux")
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    prctl.restype = ctypes.c_int
    if prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "PR_SET_DUMPABLE fehlgeschlagen: " + os.strerror(error))
    if prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise RuntimeError("Platz-Prozessschutz nicht wirksam")
