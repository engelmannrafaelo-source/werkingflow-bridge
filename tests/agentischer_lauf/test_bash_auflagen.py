"""ABASHR conditions that hold without a Docker daemon (BRUMBAU, 13.10.2026):
parallelism bound (2), SIGINT on PID 1 (3), daemon-side removal (4), and the
CPU/wall race behind the flaky test_res_cpu_loop_times_out_and_cleans."""

import asyncio
import math
import os

import pytest

from src.agentischer_lauf.adapter import RunError
from src.agentischer_lauf.http import failure

IMAGE = "sha256:" + "0" * 64


def place(tmp_path, **kw):
    from src.agentischer_lauf.bash import BashPlace

    return BashPlace(image=IMAGE, root=tmp_path, **kw)


@pytest.mark.parametrize("timeout_s", [0.5, 1, 2, 4, 30, 480])
def test_cpu_limit_stays_above_wall_clock(timeout_s):
    from src.agentischer_lauf.bash_kind import cpu_grenze

    assert cpu_grenze(timeout_s) >= math.ceil(timeout_s) + 1 > timeout_s


async def test_parallel_bound_is_loud_429(tmp_path):
    box = place(tmp_path, max_parallel=1)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(*args):
        started.set()
        await release.wait()
        return {"exit_code": 0}

    box._run = slow
    first = asyncio.create_task(box.run(command="true", material={}, files={}, outputs=[], max_bytes=1))
    await started.wait()
    with pytest.raises(RunError) as caught:
        await box.run(command="true", material={}, files={}, outputs=[], max_bytes=1)
    assert caught.value.code == "lauf_belegt" and caught.value.retryable is True
    assert failure(caught.value).status_code == 429
    release.set()
    assert await first == {"exit_code": 0}
    assert box.belegt == 0


async def test_parallel_slot_is_freed_on_failure(tmp_path):
    box = place(tmp_path, max_parallel=1)

    async def broken(*args):
        raise RunError("sandbox_fehler")

    box._run = broken
    for _ in range(2):
        with pytest.raises(RunError, match="sandbox_fehler"):
            await box.run(command="true", material={}, files={}, outputs=[], max_bytes=1)
    assert box.belegt == 0


@pytest.mark.parametrize("value", [0, 17])
def test_parallel_bound_is_validated(tmp_path, value):
    with pytest.raises(ValueError):
        place(tmp_path, max_parallel=value)


async def test_container_is_created_with_daemon_side_removal(tmp_path):
    box = place(tmp_path)
    calls = []

    async def command(args, **kw):
        calls.append(list(args))
        return (1, b"", b"No such container") if args[0] != "create" else (1, b"", b"")

    box._command = command
    with pytest.raises(RunError, match="sandbox_fehler"):
        await box.run(command="true", material={}, files={}, outputs=[], max_bytes=1)
    assert calls[0][0] == "create" and "--rm" in calls[0]
    assert ["rm", "--force"] == calls[1][:2]
    assert calls[-1][:2] == ["container", "inspect"]


async def test_removal_waits_for_the_daemons_own_removal(tmp_path):
    """--rm races our rm: "already in progress" is no error, but the call
    returns only once the container is gone (nothing outlives it)."""
    box = place(tmp_path)
    inspects = []

    async def command(args, **kw):
        if args[0] == "rm":
            return 1, b"", b"Error: removal of container x is already in progress"
        inspects.append(args)
        return (0, b"id", b"") if len(inspects) < 3 else (1, b"", b"Error: No such container: x")

    box._command = command
    await box._entfernen("x")
    assert len(inspects) == 3


async def test_removal_that_never_finishes_is_loud(tmp_path, monkeypatch):
    box = place(tmp_path)

    async def command(args, **kw):
        return (1, b"", b"removal already in progress") if args[0] == "rm" else (0, b"id", b"")

    monkeypatch.setattr("src.agentischer_lauf.bash.asyncio.sleep", AsyncMockSleep())
    box._command = command
    with pytest.raises(RunError, match="sandbox_fehler"):
        await box._entfernen("x")


async def test_container_name_carries_the_place_prefix(tmp_path):
    """BRUMBAU2R P2-5: a test run names its containers with its own prefix, so
    it finds only its own leftovers on a shared daemon."""
    box = place(tmp_path, name_prefix="agentic-bash-tabc-")
    names = []

    async def command(args, **kw):
        if args[0] == "create":
            names.append(args[args.index("--name") + 1])
            return 1, b"", b"stub"
        if args[0] == "inspect":
            return 1, b"", b"Error: No such container"
        return 0, b"", b""

    box._command = command
    with pytest.raises(RunError):
        await box.run(command="true", material={}, files={}, outputs=[], max_bytes=1000)
    assert len(names) == 1 and names[0].startswith("agentic-bash-tabc-")
    assert len(names[0]) == len("agentic-bash-tabc-") + 32


@pytest.mark.parametrize("prefix", ["", "bash-", "agentic-bash-X", "agentic-bash-a/b",
                                    "agentic-bash-" + "a" * 41])
def test_container_prefix_must_stay_in_the_bash_namespace(tmp_path, prefix):
    with pytest.raises(ValueError):
        place(tmp_path, name_prefix=prefix)


def test_default_prefix_is_unchanged(tmp_path):
    assert place(tmp_path).name_prefix == "agentic-bash-"


class AsyncMockSleep:
    async def __call__(self, *_):
        return None


async def test_sigint_to_pid1_is_ignored(tmp_path):
    """Real container (ABASH_TEST_IMAGE): `kill -INT 1` leaves the call an
    ordinary result instead of sandbox_fehler."""
    from src.agentischer_lauf.bash import BashPlace

    image = os.environ.get("ABASH_TEST_IMAGE")
    if not image:
        pytest.fail("ABASH_TEST_IMAGE must name the local computation image for isolation tests")
    box = BashPlace(image=image, root=tmp_path, timeout_s=4, work_bytes=8_000_000)
    result = await box.run(command="kill -INT 1; sleep 0.2; echo survived > result.txt",
                           material={}, files={}, outputs=["result.txt"], max_bytes=100000)
    assert result["exit_code"] == 0
    assert "result.txt" in result["files"]


# --- BRUMBAU3R P3-R2-3: Created leftovers of hard-killed runs ----------------


def daemon_stub(container, rm_code=0):
    """container: id -> (name, status, seconds ago). Records rm calls."""
    from datetime import datetime, timedelta, timezone

    entfernt = []

    async def command(args, **kw):
        if args[0] == "ps":
            assert "status=created" in args and "name=agentic-bash-tabc-" in args
            return 0, "\n".join(container).encode(), b""
        if args[:2] == ["container", "inspect"] and "{{.Id}}" not in args[3]:
            name, status, vor = container[args[-1]]
            erstellt = (datetime.now(timezone.utc) - timedelta(seconds=vor)).strftime(
                "%Y-%m-%dT%H:%M:%S.123456789Z")
            return 0, f'"/{name}" "{status}" "{erstellt}"\n'.encode(), b""
        if args[0] == "rm":
            entfernt.append(args[-1])
            return rm_code, b"", b"" if not rm_code else b"Error: daemon says no"
        if args[:2] == ["container", "inspect"]:
            return 1, b"", b"Error: No such container"
        raise AssertionError(args)

    return command, entfernt


async def test_cleanup_removes_only_old_created_containers_of_the_prefix(tmp_path):
    box = place(tmp_path, name_prefix="agentic-bash-tabc-")
    box._command, entfernt = daemon_stub({
        "a": ("agentic-bash-tabc-alt", "created", 600),
        "b": ("agentic-bash-tabc-jung", "created", 2),
        "c": ("agentic-bash-tabc-laeuft", "running", 600),
        "d": ("x-agentic-bash-tabc-fremd", "created", 600),
    })
    assert await box.aufraeumen() == ["agentic-bash-tabc-alt"]
    assert entfernt == ["agentic-bash-tabc-alt"]


async def test_cleanup_skips_a_container_gone_meanwhile(tmp_path):
    box = place(tmp_path, name_prefix="agentic-bash-tabc-")
    command, entfernt = daemon_stub({"a": ("agentic-bash-tabc-alt", "created", 600)})

    async def weg(args, **kw):
        if args[:2] == ["container", "inspect"] and args[-1] == "a":
            return 1, b"", b"Error: No such object: a"
        return await command(args, **kw)

    box._command = weg
    assert await box.aufraeumen() == [] and entfernt == []


async def test_cleanup_that_fails_is_loud(tmp_path):
    box = place(tmp_path, name_prefix="agentic-bash-tabc-")
    box._command, _ = daemon_stub({"a": ("agentic-bash-tabc-alt", "created", 600)}, rm_code=1)
    with pytest.raises(RunError, match="sandbox_fehler"):
        await box.aufraeumen()


async def test_cleanup_with_unreadable_container_is_loud(tmp_path):
    box = place(tmp_path, name_prefix="agentic-bash-tabc-")
    command, entfernt = daemon_stub({"a": ("agentic-bash-tabc-alt", "created", 600)})

    async def kaputt(args, **kw):
        if args[:2] == ["container", "inspect"]:
            return 1, b"", b"Error: daemon not reachable"
        return await command(args, **kw)

    box._command = kaputt
    with pytest.raises(RunError, match="sandbox_fehler"):
        await box.aufraeumen()
    assert entfernt == []
