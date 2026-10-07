import asyncio
import inspect
import re
import signal
import sys
from pathlib import Path

import httpx
import pytest

from src.erkunder import aufraeumen, platz
from src.erkunder.kind import ALLOWED_TOOLS, DISALLOWED_TOOLS, sdk_options
from src.erkunder.platz import Platz, Schritt, create_app


@pytest.fixture(autouse=True)
def isolated_process_and_tmp_inventory(tmp_path, monkeypatch):
    # Real pidfds/signals, but enumerate only processes created by this test.
    # A full host UID scan would kill unrelated sessions sharing the test UID.
    proc = tmp_path / "proc-inventory"
    proc.mkdir()
    shared_tmp = tmp_path / "container-tmp"
    shared_tmp.mkdir()

    async def stop():
        for marker in tmp_path.rglob("*.pid"):
            pid = marker.read_text().strip()
            if pid:
                entry = proc / pid
                if not entry.is_symlink():
                    entry.symlink_to(Path("/proc") / pid)
        await aufraeumen.stop_uid_processes(proc)

    monkeypatch.setattr(platz, "stop_uid_processes", stop)
    monkeypatch.setattr(
        platz, "clear_owned_tmp", lambda: aufraeumen.clear_owned_tmp(shared_tmp)
    )
    monkeypatch.setattr(platz, "clear_owned_ipc", lambda: None)
    return shared_tmp


@pytest.fixture
def run_space(tmp_path):
    folder = tmp_path / "bericht-123" / "erkunder-1"
    folder.mkdir(parents=True)
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "memory.current").write_text("1048576")
    (cgroup / "memory.events").write_text("oom_kill 0\n")
    return folder, cgroup


def request(folder, **changes):
    data = dict(
        bericht_id="bericht-123",
        schritt="erkunder-1",
        ordner=str(folder),
        prompt="probe",
        timeout_s=3,
        max_turns=7,
        claude_token="test-secret",
    )
    data.update(changes)
    return Schritt(**data)


async def execute(run_space, source, **changes):
    folder, cgroup = run_space
    script = folder.parent.parent / "fake.py"
    script.write_text(
        "import json,sys,os,time\nfrom pathlib import Path\n"
        'b=json.load(sys.stdin)\np=Path(b["ordner"])\n' + source
    )
    service = Platz(folder.parent.parent, cgroup, [sys.executable, str(script)], 0.02)
    await service.start(request(folder, **changes))
    await service.task
    return service, service.states[("bericht-123", "erkunder-1")]


@pytest.mark.asyncio
async def test_timeout_kills_process_group(run_space):
    folder, _ = run_space
    service, state = await execute(
        run_space,
        "pid=os.fork()\n"
        "if pid == 0:\n"
        ' (p/"child.pid").write_text(str(os.getpid()))\n'
        " time.sleep(60)\n"
        "time.sleep(60)\n",
        timeout_s=0.3,
    )
    assert state["meta"]["abbruch_grund"] == "zeit"
    assert service.process.returncode == -signal.SIGKILL
    child = int((folder / "child.pid").read_text())
    # An orphan may briefly remain a zombie awaiting PID 1; it cannot execute.
    stat = Path(f"/proc/{child}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"


@pytest.mark.asyncio
async def test_oom_kill(run_space):
    _, cgroup = run_space
    _, state = await execute(
        run_space,
        f'Path({str(cgroup / "memory.events")!r}).write_text("oom_kill 1\\n")\n'
        "time.sleep(60)\n",
    )
    assert state["meta"]["abbruch_grund"] == "speicher"


@pytest.mark.asyncio
async def test_missing_result(run_space):
    _, state = await execute(run_space, 'print("{}")\n')
    assert state["meta"]["abbruch_grund"] == "cli_fehler: kein ergebnis"


@pytest.mark.asyncio
async def test_child_failure_reason_is_reported_in_step_status(run_space):
    _, state = await execute(
        run_space,
        'print(json.dumps({"fehler": "cli_fehler: SDK-Lauf: MessageParseError"}))\n'
        "sys.exit(1)\n",
    )
    assert state["meta"]["abbruch_grund"] == "cli_fehler: SDK-Lauf: MessageParseError"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["ergebnis.md", "skripte/a.py"])
async def test_secret_deleted(run_space, name):
    folder, _ = run_space
    _, state = await execute(
        run_space,
        '(p/"skripte").mkdir()\n'
        '(p/"ergebnis.md").write_text("valid")\n'
        f'(p/{name!r}).write_text(b["claude_token"])\nprint("{{}}")\n',
    )
    assert state["meta"]["abbruch_grund"] == "geheimnis_im_ergebnis"
    assert not (folder / name).exists()


@pytest.mark.asyncio
async def test_success_and_second_run(run_space):
    service, state = await execute(
        run_space,
        '(p/"ergebnis.md").write_text("Ergebnis")\n'
        'print(json.dumps({"zuege": 3, "tokens": {"input": 4, "output": 5,'
        '"cache_read": 6,"cache_creation": 7}}))\n',
    )
    assert state["zustand"] == "fertig"
    assert state["meta"]["zuege"] == 3
    assert state["meta"]["tokens"]["cache_read"] == 6
    assert state["meta"]["ram_spitze_mb"] == 1
    await service.start(request(run_space[0]))
    await service.task
    assert service.states[("bericht-123", "erkunder-1")]["zustand"] == "fertig"


def test_sdk_options(run_space):
    from claude_code_sdk import ClaudeCodeOptions

    folder, _ = run_space
    body = request(folder).model_dump()
    body["claude_token"] = "test-secret"
    options = sdk_options(body)
    assert "extra_args" in inspect.signature(ClaudeCodeOptions).parameters
    assert options.extra_args == {"settings": "/etc/erkunder/settings.json"}
    assert options.cwd == str(folder)
    assert options.env["HOME"] == str(folder / ".home")
    assert options.env["TMPDIR"] == str(folder / ".home/tmp")
    assert options.model == "claude-sonnet-5-5"
    assert options.mcp_servers == {}
    assert options.allowed_tools == ALLOWED_TOOLS
    assert not set(ALLOWED_TOOLS) & set(DISALLOWED_TOOLS)
    assert options.max_turns == 7
    assert options.permission_mode == "bypassPermissions"


@pytest.mark.asyncio
async def test_routes_auth_busy_cancel(run_space, monkeypatch):
    folder, cgroup = run_space
    script = folder.parent / "sleep.py"
    script.write_text("import time; time.sleep(60)")
    service = Platz(folder.parent.parent, cgroup, [sys.executable, str(script)], 0.02)
    monkeypatch.setenv("ERKUNDER_INTERNAL_TOKEN", "internal-test")
    app = create_app(service)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        assert (await client.post("/abbrechen")).status_code == 403
        assert (await client.get("/openapi.json")).status_code == 403
        assert (await client.get("/missing")).status_code == 403
        client.headers["X-Erkunder-Intern"] = "internal-test"
        body = request(folder).model_dump(mode="json")
        body["claude_token"] = "test-secret"
        assert (await client.post("/schritt", json=body)).status_code == 200
        assert (await client.post("/schritt", json=body)).status_code == 409
        assert (await client.get("/schritt/unknown/erkunder-1")).status_code == 404
        assert (await client.post("/abbrechen")).json() == {"abgebrochen": True}
        assert (await client.get("/schritt/bericht-123/erkunder-1")).json()[
            "zustand"
        ] == "abbruch"
        assert (await client.post("/abbrechen")).json() == {"abgebrochen": False}


@pytest.mark.asyncio
async def test_internal_credentials_not_in_child_environment(run_space, monkeypatch):
    monkeypatch.setenv("ERKUNDER_INTERNAL_TOKEN", "must-stay-in-parent")
    _, state = await execute(
        run_space,
        'assert "ERKUNDER_INTERNAL_TOKEN" not in os.environ\n'
        '(p/"ergebnis.md").write_text("Ergebnis")\nprint("{}")\n',
    )
    assert state["zustand"] == "fertig"


@pytest.mark.asyncio
async def test_result_symlink_rejected_without_deleting_target(run_space):
    folder, _ = run_space
    outside = folder.parent.parent / "outside.md"
    outside.write_text("test-secret")
    _, state = await execute(
        run_space, f'(p/"ergebnis.md").symlink_to({str(outside)!r})\nprint("{{}}")\n'
    )
    assert state["meta"]["abbruch_grund"] == "cli_fehler: Ergebnis-Symlink"
    assert outside.read_text() == "test-secret"


def test_token_chunk_boundary(tmp_path):
    from src.erkunder.platz import contains_secret

    path = tmp_path / "large.py"
    path.write_bytes(b" " * (64 * 1024 - 3) + b"test-secret")
    assert contains_secret(path, b"test-secret")


@pytest.mark.parametrize("end", ["success", "timeout", "error"])
async def test_setsid_child_and_tmp_do_not_survive(
    run_space, isolated_process_and_tmp_inventory, end
):
    folder, _ = run_space
    shared = isolated_process_and_tmp_inventory / "leftover"
    shared.write_text("old report")
    _, state = await execute(
        run_space,
        'Path(os.environ["TMPDIR"], "private").write_text("customer data")\n'
        "pid=os.fork()\n"
        "if pid == 0:\n"
        " os.setsid()\n"
        " os.close(0); os.close(1); os.close(2)\n"
        ' (p/"escaped.pid").write_text(str(os.getpid()))\n'
        " time.sleep(60)\n"
        " os._exit(0)\n"
        'while not (p/"escaped.pid").exists(): time.sleep(0.01)\n'
        '(p/"ergebnis.md").write_text("valid")\n'
        + {
            "success": 'print("{}")\n',
            "timeout": "time.sleep(60)\n",
            "error": 'raise RuntimeError("synthetic")\n',
        }[end],
        timeout_s=0.5,
    )
    assert state["zustand"] == ("fertig" if end == "success" else "abbruch")
    pid = (folder / "escaped.pid").read_text()
    status = Path("/proc") / pid / "status"
    assert not status.exists() or "Z (zombie)" in status.read_text()
    assert not (folder / ".home/tmp/private").exists()
    assert not shared.exists()


async def test_cleanup_failure_blocks_reuse(run_space, monkeypatch, caplog):
    def fail():
        raise OSError("must not log customer data")

    monkeypatch.setattr(platz, "clear_owned_tmp", fail)
    service, state = await execute(
        run_space, '(p/"ergebnis.md").write_text("valid")\nprint("{}")\n'
    )
    assert state["fehler"] == "cli_fehler: Platz-Aufraeumen"
    assert "OSError" in caplog.text
    assert "customer data" not in caplog.text
    with pytest.raises(platz.HTTPException) as start_error:
        await service.start(request(run_space[0]))
    assert start_error.value.status_code == 503
    with pytest.raises(platz.HTTPException) as abort_error:
        await service.abort()
    assert abort_error.value.status_code == 503
    monkeypatch.setenv("ERKUNDER_INTERNAL_TOKEN", "synthetic")
    app = create_app(service)
    # Shutdown also remains red: no silent healing of a failed cleanup.
    with pytest.raises(platz.HTTPException):
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/__bereitschaft__")).status_code == 403
            client.headers["X-Erkunder-Intern"] = "synthetic"
            response = await client.get("/__bereitschaft__")
            assert response.status_code == 503
            assert response.json() == {"detail": "Platz-Aufraeumen fehlgeschlagen"}
            response = await client.get("/schritt/bericht-123/erkunder-1")
            assert response.status_code == 200
            assert response.json()["zustand"] == "abbruch"


@pytest.mark.parametrize("kind", ["fifo", "large"])
async def test_invalid_result_rejected(run_space, kind):
    from src.erkunder.dateien import MAX_FILE_BYTES

    source = (
        'os.mkfifo(p/"ergebnis.md")\n'
        if kind == "fifo"
        else (
            'with (p/"ergebnis.md").open("wb") as f:\n'
            f" f.truncate({MAX_FILE_BYTES + 1})\n"
        )
    )
    _, state = await execute(run_space, source + 'print("{}")\n')
    assert state["zustand"] == "abbruch"
    assert "ergebnis-pfad oder groesse" in state["fehler"]


@pytest.mark.parametrize(
    "encoding",
    ["base64", "base64-prefix-1", "base64-prefix-2", "hex", "HEX", "url", "url-all"],
)
@pytest.mark.parametrize("name", ["ergebnis.md", "skripte/a.py"])
async def test_encoded_secret_deleted(run_space, encoding, name):
    import base64
    from urllib.parse import quote

    token = "Synthetic-Token/a+b=c?Xy"
    variants = {
        "base64": base64.b64encode(token.encode()).decode(),
        "base64-prefix-1": base64.b64encode(("x" + token + "z").encode()).decode(),
        "base64-prefix-2": base64.b64encode(("xy" + token + "z").encode()).decode(),
        "hex": token.encode().hex(),
        "HEX": token.encode().hex().upper(),
        "url": quote(token, safe=""),
        "url-lower": re.sub(
            r"%[0-9A-F]{2}", lambda m: m[0].lower(), quote(token, safe="")
        ),
        "url-all": "".join(f"%{b:02x}" for b in token.encode()),
    }
    folder, _ = run_space
    _, state = await execute(
        run_space,
        '(p/"skripte").mkdir()\n'
        '(p/"ergebnis.md").write_text("valid")\n'
        f'(p/{name!r}).write_text({variants[encoding]!r})\nprint("{{}}")\n',
        claude_token=token,
    )
    assert state["fehler"] == "geheimnis_im_ergebnis"
    assert not (folder / name).exists()


async def test_cancelled_task_cleans_and_reports_abort(run_space):
    folder, cgroup = run_space
    script = folder.parent / "cancel.py"
    script.write_text(
        "import json,os,sys,time\nfrom pathlib import Path\n"
        'p=Path(json.load(sys.stdin)["ordner"])\n'
        'Path(os.environ["TMPDIR"], "private").write_text("data")\n'
        '(p/"ready.pid").write_text(str(os.getpid()))\n'
        "time.sleep(60)\n"
    )
    service = Platz(folder.parent.parent, cgroup, [sys.executable, str(script)], 0.02)
    await service.start(request(folder))
    async with asyncio.timeout(3):
        while not (folder / "ready.pid").exists():
            await asyncio.sleep(0.01)
    service.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await service.task
    assert service.states[("bericht-123", "erkunder-1")]["zustand"] == "abbruch"
    assert service.process.returncode == -signal.SIGKILL
    assert not (folder / ".home/tmp/private").exists()


@pytest.mark.parametrize("end", ["success", "timeout", "error", "cancel"])
async def test_reaping_follows_subprocess_communication(run_space, monkeypatch, end):
    folder, cgroup = run_space
    script = folder.parent / "reap-order.py"
    script.write_text(
        "import json,os,sys,time\nfrom pathlib import Path\n"
        'p=Path(json.load(sys.stdin)["ordner"])\n'
        '(p/"ready.pid").write_text(str(os.getpid()))\n'
        '(p/"ergebnis.md").write_text("valid")\n'
        + {
            "success": 'print("{}")\n',
            "error": "sys.exit(7)\n",
            "timeout": "time.sleep(60)\n",
            "cancel": "time.sleep(60)\n",
        }[end]
    )
    service = Platz(folder.parent.parent, cgroup, [sys.executable, str(script)], 0.02)
    communicated = False
    original = asyncio.subprocess.Process.communicate

    async def communicate(process, *args):
        nonlocal communicated
        result = await original(process, *args)
        communicated = True
        return result

    reaped = False

    async def reap():
        nonlocal reaped
        assert communicated
        assert service.process.returncode == {
            "success": 0, "error": 7, "timeout": -9, "cancel": -9
        }[end]
        reaped = True

    monkeypatch.setattr(asyncio.subprocess.Process, "communicate", communicate)
    monkeypatch.setattr(platz, "reap_children", reap)
    await service.start(request(folder, timeout_s=0.3))
    if end == "cancel":
        async with asyncio.timeout(3):
            while not (folder / "ready.pid").exists():
                await asyncio.sleep(0.01)
        service.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await service.task
    else:
        await service.task
    assert reaped
    assert not service.cleanup_failed


@pytest.mark.parametrize("double_cancel", [False, True])
async def test_cancel_during_communication_await(run_space, monkeypatch, double_cancel):
    folder, cgroup = run_space
    service = Platz(
        folder.parent.parent, cgroup,
        [sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(60)"],
        0.01,
    )
    communication_task = None
    original_communicate = asyncio.subprocess.Process.communicate
    original_stop = platz.stop_uid_processes
    cleanup_entered = asyncio.Event()
    stopped = asyncio.Event()
    calls = 0

    async def communicate(process, *args):
        nonlocal communication_task
        communication_task = asyncio.current_task()
        result = await original_communicate(process, *args)
        await asyncio.Event().wait()  # Hold pipe completion beyond timeout cleanup.
        return result

    async def stop():
        nonlocal calls
        calls += 1
        await original_stop()
        if calls == 1:
            stopped.set()
        elif double_cancel:
            cleanup_entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(asyncio.subprocess.Process, "communicate", communicate)
    monkeypatch.setattr(platz, "stop_uid_processes", stop)
    await service.start(request(folder, timeout_s=0.1))
    async with asyncio.timeout(3):
        await stopped.wait()
        # Specifically await communication, not asyncio.wait's monitoring future.
        while service.task._fut_waiter is not communication_task:
            await asyncio.sleep(0)
        service.task.cancel()
        if double_cancel:
            await cleanup_entered.wait()
            service.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await service.task
    assert communication_task.cancelled()
    # Double cancellation intentionally interrupts cleanup before process.wait().
    # SIGKILL delivery and asyncio's child-watcher callback are asynchronous;
    # cancellation of communicate() does not imply a settled returncode.
    async with asyncio.timeout(3):
        await service.process.wait()
    assert service.process.returncode == -signal.SIGKILL
    if double_cancel:
        assert service.cleanup_failed
        state = service.states[("bericht-123", "erkunder-1")]
        assert state["zustand"] == "abbruch"
        assert state["fehler"] == "cli_fehler: Platz-Aufraeumen"
        with pytest.raises(platz.HTTPException) as error:
            await service.start(request(folder))
        assert error.value.status_code == 503
    else:
        assert not service.cleanup_failed
        assert service.states[("bericht-123", "erkunder-1")]["zustand"] == "abbruch"
        assert not (folder / ".home").exists()


async def test_missing_proc_children_prevents_readiness(monkeypatch):
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path.name == "children":
            raise FileNotFoundError(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setenv("ERKUNDER_INTERNAL_TOKEN", "synthetic-token")
    app = create_app()
    with pytest.raises(RuntimeError, match="Platz nicht bereit.*CONFIG_PROC_CHILDREN"):
        async with app.router.lifespan_context(app):
            pytest.fail("Platz meldet ohne proc children bereit")


async def test_ipc_cleanup_failure_blocks_next_report(run_space, monkeypatch):
    folder, cgroup = run_space

    def fail():
        raise PermissionError("synthetic IPC cleanup failure")

    monkeypatch.setattr(platz, "clear_owned_ipc", fail)
    service = Platz(root=folder.parent.parent, cgroup=cgroup,
                    command=[sys.executable, "-c", "print('{}')"])
    await service.start(request(folder))
    await service.task
    assert service.cleanup_failed
    assert service.states[("bericht-123", "erkunder-1")]["fehler"] == (
        "cli_fehler: Platz-Aufraeumen"
    )
    with pytest.raises(platz.HTTPException) as error:
        await service.start(request(folder))
    assert error.value.status_code == 503


async def test_execution_error_logged_without_secret(run_space, caplog, monkeypatch):
    folder, cgroup = run_space
    service = Platz(folder.parent.parent, cgroup)

    def broken_memory():
        raise PermissionError("test-secret must not appear in logs")

    monkeypatch.setattr(service, "memory", broken_memory)
    await service.start(request(folder))
    await service.task
    assert "bericht_id=bericht-123 schritt=erkunder-1" in caplog.text
    assert "ausfuehrung=fehlgeschlagen fehler=PermissionError" in caplog.text
    assert "test-secret" not in caplog.text
    assert service.states[("bericht-123", "erkunder-1")]["zustand"] == "abbruch"
