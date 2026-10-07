import inspect
import signal
import sys
from pathlib import Path

import httpx
import pytest
from claude_code_sdk import ClaudeCodeOptions

from src.erkunder.kind import ALLOWED_TOOLS, DISALLOWED_TOOLS, sdk_options
from src.erkunder.platz import Platz, Schritt, create_app


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
    folder, _ = run_space
    body = request(folder).model_dump()
    body["claude_token"] = "test-secret"
    options = sdk_options(body)
    assert "extra_args" in inspect.signature(ClaudeCodeOptions).parameters
    assert options.extra_args == {"settings": "/etc/erkunder/settings.json"}
    assert options.cwd == str(folder)
    assert options.env["HOME"] == str(folder / ".home")
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
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
    ) as client:
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
