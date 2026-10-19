"""Tiny post-mount launcher: drop identity and install a child syscall filter."""

import ctypes
import errno
import os
import sys
from pathlib import Path


def launch(root: Path, command: list[str]) -> None:
    if os.getuid() != 0 or not command:
        raise RuntimeError("namespace launcher requires setup identity and command")
    # The mount exposes only the validated attempt copy. No original is chowned.
    for path in [*root.rglob("*"), root]:
        os.chown(path, 1101, 1100, follow_symlinks=False)
    os.setgroups([])
    os.setgid(1100)
    os.setuid(1101)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "no_new_privs")
    status = Path("/proc/self/status").read_text()
    for name in ("CapEff", "CapPrm"):
        value = next(
            line.split()[1]
            for line in status.splitlines()
            if line.startswith(name + ":")
        )
        if int(value, 16):
            raise RuntimeError("child retained capabilities")
    seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = seccomp.seccomp_init(0x7FFF0000)  # inherited supervisor allowlist + deny
    if not ctx:
        raise RuntimeError("seccomp_init failed")
    for name in (
        "mount",
        "umount2",
        "pivot_root",
        "unshare",
        "setns",
        "ptrace",
        "process_vm_readv",
        "process_vm_writev",
        "bpf",
        "perf_event_open",
        "keyctl",
        "add_key",
        "request_key",
        "open_by_handle_at",
        "fsopen",
        "fsconfig",
        "fsmount",
        "move_mount",
        "mount_setattr",
    ):
        number = seccomp.seccomp_syscall_resolve_name(name.encode())
        if (
            number >= 0
            and seccomp.seccomp_rule_add(ctx, 0x00050000 | errno.EPERM, number, 0) != 0
        ):
            raise RuntimeError("seccomp rule failed: " + name)

    class ArgCompare(ctypes.Structure):
        _fields_ = [
            ("arg", ctypes.c_uint),
            ("op", ctypes.c_uint),
            ("mask", ctypes.c_uint64),
            ("value", ctypes.c_uint64),
        ]

    seccomp.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(ArgCompare),
    ]
    clone = seccomp.seccomp_syscall_resolve_name(b"clone")
    for flag in (
        0x20000,
        0x2000000,
        0x4000000,
        0x8000000,
        0x10000000,
        0x20000000,
        0x40000000,
    ):
        comparison = ArgCompare(0, 7, flag, flag)  # SCMP_CMP_MASKED_EQ
        if (
            seccomp.seccomp_rule_add_array(
                ctx, 0x00050000 | errno.EPERM, clone, 1, ctypes.byref(comparison)
            )
            != 0
        ):
            raise RuntimeError("clone namespace filter failed")
    clone3 = seccomp.seccomp_syscall_resolve_name(b"clone3")
    if (
        clone3 >= 0
        and seccomp.seccomp_rule_add(ctx, 0x00050000 | errno.ENOSYS, clone3, 0) != 0
    ):
        raise RuntimeError("clone3 filter failed")
    if seccomp.seccomp_load(ctx) != 0:
        raise RuntimeError("seccomp_load failed")
    seccomp.seccomp_release(ctx)
    os.execv(command[0], command)


if __name__ == "__main__":
    launch(Path(sys.argv[1]), sys.argv[2:])
