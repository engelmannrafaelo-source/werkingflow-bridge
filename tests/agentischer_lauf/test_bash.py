"""Synthetic offline acceptance and real container escape probes."""

import asyncio
import os
import socket
from pathlib import Path

import pytest

from src.agentischer_lauf.adapter import RunError
from src.agentischer_lauf.models import Tool
from src.agentischer_lauf.sandbox import Sandbox
from src.agentischer_lauf.http import failure
from src.models import ResearchRequest
from .test_contract import payload, material
from .test_runtime import runtime, reply


def test_bash_is_known_but_unknown_tool_is_422():
    Sandbox.validate_tools([Tool(name="Bash", parameter={})])
    with pytest.raises(RunError) as exc:
        Sandbox.validate_tools([Tool(name="Unrecognized", parameter={})])
    assert failure(exc.value).status_code == 422


@pytest.fixture
def place(tmp_path):
    from src.agentischer_lauf.bash import BashPlace

    image = os.environ.get("ABASH_TEST_IMAGE")
    if not image:
        pytest.fail("ABASH_TEST_IMAGE must name the local computation image for isolation tests")
    return BashPlace(image=image, root=tmp_path, timeout_s=4, work_bytes=8_000_000)


def sandbox(place, tools=("Bash",), material=None, limit=100000):
    return Sandbox(
        material or {},
        [Tool(name=n, parameter={}) for n in tools],
        ["result.txt", "scripts"],
        limit,
        bash=place,
    )


async def call(box, command):
    return await box.acall("Bash", {"command": command})


async def test_calculation_and_continuation(place):
    box = sandbox(place, ("Bash", "Read", "Write", "List"), {"input.txt": b"6"})
    box.call(
        "Write",
        {
            "path": "scripts/calc.py",
            "content": "from pathlib import Path\nPath('result.txt').write_text(str(int(Path('input.txt').read_text()) * 7))\n",
        },
    )
    result = await call(box, "python scripts/calc.py; printf computed")
    assert result == {"exit_code": 0, "stdout": "computed", "stderr": ""}
    assert box.call("Read", {"path": "result.txt"}) == "42"
    await call(box, "printf '!\n' >> result.txt")
    assert box.read("result.txt") == b"42!\n"
    assert sorted(box.artifacts(["scripts", "result.txt"])) == ["result.txt", "scripts/calc.py"]


async def test_unrequested_bash_never_starts(place):
    box = sandbox(place, ("Read",))
    with pytest.raises(RunError, match="werkzeug_fehler"):
        await call(box, "echo escaped")
    assert not list(place.root.iterdir())


@pytest.mark.parametrize(
    "command",
    [
        "cat /host-marker",
        "python -c 'import os; os.setuid(0)'",
        "python -c 'import socket; s=socket.socket(type=socket.SOCK_DGRAM); s.connect((\"192.0.2.1\", 443))'",
        "cat /proc/1/environ",
        "echo overwritten > input.txt",
        "mkdir scripts; mv input.txt scripts/renamed",
        "echo bad > /etc/abash-marker",
        "python -c 'import socket; socket.create_connection((\"example.invalid\", 443), timeout=1)'",
    ],
)
async def test_escape_denied(place, command):
    box = sandbox(place, material={"input.txt": b"immutable"})
    result = await call(box, command)
    assert result["exit_code"] != 0
    assert box.read("input.txt") == b"immutable"


@pytest.mark.parametrize(
    "command",
    [
        "ln -s /etc/passwd result.txt",
        "mkfifo result.txt",
        "printf hidden > undeclared.txt",
        "mkdir scripts; echo x > scripts/a; ln scripts/a scripts/b",
        "python -c 'print(\"x\" * 1000000)'",
    ],
)
async def test_invalid_outputs_fail_closed(place, command):
    box = sandbox(place)
    with pytest.raises(RunError):
        await call(box, command)
    assert not box.files


async def test_timeout_and_daemon_children_removed(place):
    place.timeout_s = 0.7
    box = sandbox(place)
    with pytest.raises(RunError):
        await call(box, "sleep 20")
    result = await call(box, "sleep 20 </dev/null >/dev/null 2>&1 & echo done")
    assert result["stdout"] == "done\n"
    assert not list(place.root.iterdir())


async def test_no_secrets_or_host_processes_and_pid_limit(place, monkeypatch):
    monkeypatch.setenv("ABASH_SYNTHETIC_SECRET", "synthetic-private-value")
    box = sandbox(place)
    result = await call(
        box,
        "python - <<'CODE'\nimport os\nfrom pathlib import Path\nassert 'ABASH_SYNTHETIC_SECRET' not in os.environ\nassert os.getuid() != 0\nassert int(Path('/sys/fs/cgroup/pids.max').read_text()) <= 128\nassert int(Path('/sys/fs/cgroup/memory.max').read_text()) <= 2 * 1024**3\nassert len([p for p in Path('/proc').iterdir() if p.name.isdigit()]) < 20\nprint('isolated')\nCODE",
    )
    assert result["exit_code"] == 0, result
    assert result["stdout"] == "isolated\n"


async def test_runtime_bash_file_tool_allowlist(tmp_path, place):
    body = payload(antwort_im_text=False)
    run = body["agentischer_lauf"]
    run["material"] = [material("input.txt", b"6")]
    run["werkzeuge"] = [{"name": "Bash", "parameter": {}}]
    run["ausgabe"] = {"modus": "datei", "datei": "result.txt", "artefakte": []}
    calls = [{"id": "b1", "name": "Bash", "input": {"command": "echo $((6*7)) > result.txt"}}]
    rt, sdk = runtime(tmp_path, [reply(end="tool_use", calls=calls), reply()])
    rt.bash = place
    result = await rt.execute(ResearchRequest(**body), "synthetic-tenant")
    assert result["status"] == "success", result["fehler_code"]
    assert result["content"] == "42\n"
    assert result["werkzeugaufrufe"]["eintraege"][0]["status"] == "ok"
    assert result["artefakte"][0]["pfad"] == "result.txt"
    assert [t["name"] for t in sdk.calls[0]["tools"]] == ["Bash"]


async def test_file_size_and_total_size_limits(place):
    box = sandbox(place)
    result = await call(
        box, 'python -c \'from pathlib import Path; Path("result.txt").write_bytes(b"x" * 100001)\''
    )
    assert result["exit_code"] != 0
    assert len(box.read("result.txt")) <= box.max_bytes
    with pytest.raises(RunError):
        await call(
            box,
            "mkdir -p scripts; head -c 60000 /dev/zero > scripts/a; head -c 60000 /dev/zero > scripts/b",
        )


async def test_process_exhaustion_and_setsid_cleanup(place):
    box = sandbox(place)
    result = await call(
        box,
        "python - <<'CODE'\nimport subprocess\nchildren=[]\ntry:\n for _ in range(140):\n  children.append(subprocess.Popen(['sleep','20'], start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))\nexcept OSError:\n print(len(children))\nelse:\n raise AssertionError('PID limit ineffective')\nCODE",
    )
    assert result["exit_code"] == 0, result
    assert 1 <= int(result["stdout"]) < 128
    assert not list(place.root.iterdir())


async def test_cancellation_cleans_container(place):
    box = sandbox(place)
    active = asyncio.create_task(call(box, "sleep 20"))
    await asyncio.sleep(0.8)
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    assert not list(place.root.iterdir())


async def test_deleted_script_not_published_or_cached(tmp_path, place):
    body = payload(antwort_im_text=False)
    run = body["agentischer_lauf"]
    run["werkzeuge"] = [{"name": "Bash", "parameter": {}}]
    run["ausgabe"] = {"modus": "datei", "datei": "result.txt", "artefakte": []}
    calls = [
        {"id": "b1", "name": "Bash", "input": {"command": "echo draft > result.txt"}},
        {"id": "b2", "name": "Bash", "input": {"command": "rm result.txt"}},
    ]
    rt, _ = runtime(tmp_path, [reply(end="tool_use", calls=calls), reply()])
    rt.bash = place
    result = await rt.execute(ResearchRequest(**body), "synthetic-tenant")
    assert result["fehler_code"] == "antwort_fehlt"
    assert result["artefakte"] == []


async def test_network_cannot_reach_host_listener(place):
    # A real host-side socket, no external service or network traffic.
    with socket.socket() as listener:
        listener.bind(("localhost", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        result = await call(
            sandbox(place),
            f"python -c 'import socket; socket.create_connection((\"localhost\", {port}), timeout=1)'",
        )
    assert result["exit_code"] != 0


async def test_host_file_and_process_not_visible(place):
    marker = place.root / "host-only"
    marker.write_text("synthetic-host-only")
    result = await call(sandbox(place), f"cat {marker}")
    assert result["exit_code"] != 0
    result = await call(sandbox(place), f"python -c 'import os; os.kill({os.getpid()}, 0)'")
    assert result["exit_code"] != 0


@pytest.mark.parametrize(
    "args", [{}, {"command": "echo x", "cwd": "/"}, {"command": 3}, {"command": ""}]
)
async def test_bash_arguments_strict(place, args):
    with pytest.raises(RunError, match="werkzeug_fehler"):
        await sandbox(place).acall("Bash", args)


async def test_material_parent_cannot_be_shadowed(place):
    box = sandbox(place, material={"inputs/a": b"immutable"})
    with pytest.raises(RunError):
        await call(
            box, "mkdir scripts; mv inputs scripts/old; mkdir inputs; echo altered > inputs/a"
        )
    assert box.read("inputs/a") == b"immutable"


async def test_independent_review_cannot_inherit_bash(tmp_path, place):
    body = payload(antwort_im_text=False)
    run = body["agentischer_lauf"]
    run["werkzeuge"] = [{"name": "Bash", "parameter": {}}]
    run["ausgabe"] = {"modus": "datei", "datei": "result.txt", "artefakte": []}
    run["pruefung"] = {
        "auftrag": "Check arithmetic",
        "model": "synthetic",
        "werkzeuge": [{"name": "Read", "parameter": {}}],
        "ausgabe": {"modus": "text", "artefakte": []},
        "max_turns": 2,
        "max_tokens": 40,
    }
    main = {"id": "main", "name": "Bash", "input": {"command": "echo 42 > result.txt"}}
    check = {"id": "check", "name": "Bash", "input": {"command": "echo unrequested"}}
    rt, sdk = runtime(
        tmp_path,
        [reply(end="tool_use", calls=[main]), reply(), reply(end="tool_use", calls=[check])],
    )
    rt.bash = place
    result = await rt.execute(ResearchRequest(**body), "synthetic-tenant")
    assert result["pruefung"]["fehler_code"] == "werkzeug_fehler"
    assert [t["name"] for t in sdk.calls[-1]["tools"]] == ["Read"]


async def test_binary_calculation_artifacts(place):
    box = sandbox(place)
    result = await call(
        box,
        "mkdir scripts; python -c 'from pathlib import Path; Path(\"scripts/data.bin\").write_bytes(bytes(range(256)))'",
    )
    assert result["exit_code"] == 0
    assert box.artifacts(["scripts"])["scripts/data.bin"] == bytes(range(256))
