"""ABASHR: independent escape probes against the Bash computation place.

Synthetic data only, no model calls. Every probe runs a real short-lived
container through BashPlace/Sandbox, exactly as the runtime would.
"""

import asyncio
import os
import socket
import subprocess
import uuid

import pytest

from src.agentischer_lauf.adapter import RunError
from src.agentischer_lauf.http import failure
from src.agentischer_lauf.models import Tool
from src.agentischer_lauf.sandbox import Sandbox

# Every container of this test run carries its own prefix, so leftover() sees
# only this run's containers, never those of another session on the same
# daemon (BRUMBAU2R P2-5).
LAUF = "agentic-bash-t" + uuid.uuid4().hex[:12] + "-"


@pytest.fixture
def place(tmp_path):
    from src.agentischer_lauf.bash import BashPlace

    image = os.environ.get("ABASH_TEST_IMAGE")
    if not image:
        pytest.fail("ABASH_TEST_IMAGE required")
    return BashPlace(image=image, root=tmp_path, timeout_s=6, work_bytes=8_000_000,
                     name_prefix=LAUF)


def box(place, material=None, tools=("Bash",), limit=200_000):
    return Sandbox(
        material or {},
        [Tool(name=n, parameter={}) for n in tools],
        ["result.txt", "scripts"],
        limit,
        bash=place,
    )


async def sh(b, command):
    return await b.acall("Bash", {"command": command})


def leftover():
    out = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "name=" + LAUF],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    return out


def host_ips():
    out = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split()
    return [ip for ip in out if ":" not in ip][:4]


# ---------------------------------------------------------------- 1 Netz
async def test_net_only_loopback_and_no_raw_socket(place):
    r = await sh(box(place), (
        "python - <<'C'\n"
        "import socket\n"
        "ifs=[l.split(':')[0].strip() for l in open('/proc/net/dev').read().splitlines()[2:]]\n"
        "assert ifs==['lo'], ifs\n"
        "try:\n socket.socket(socket.AF_PACKET, socket.SOCK_RAW)\nexcept PermissionError: pass\n"
        "else: raise SystemExit('raw socket allowed')\n"
        "try:\n socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)\nexcept PermissionError: pass\n"
        "else: raise SystemExit('icmp raw allowed')\n"
        "print('ok')\nC"))
    assert r["exit_code"] == 0, r
    assert r["stdout"] == "ok\n"


async def test_net_host_listener_on_all_host_ips_unreachable(place):
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        targets = host_ips() + ["172.17.0.1", "169.254.169.254", "1.1.1.1"]
        code = "; ".join(
            f"python -c 'import socket; socket.create_connection((\"{ip}\", {port if ip != '1.1.1.1' else 443}), timeout=1)' && echo REACHED {ip}"
            for ip in targets
        )
        r = await sh(box(place), code + "; true")
    assert "REACHED" not in r["stdout"], r


async def test_net_host_abstract_unix_socket_unreachable(place):
    name = "\0abashr-" + uuid.uuid4().hex
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(name)
        listener.listen(1)
        r = await sh(box(place), (
            f"python -c 'import socket; s=socket.socket(socket.AF_UNIX); s.connect(\"\\0{name[1:]}\")' && echo REACHED"
        ))
    assert "REACHED" not in r["stdout"], r
    assert r["exit_code"] != 0


async def test_net_dns_fails(place):
    r = await sh(box(place), "python -c 'import socket; socket.getaddrinfo(\"github.com\", 443)' && echo RESOLVED")
    assert "RESOLVED" not in r["stdout"]


# ---------------------------------------------------------------- 2 Lesen
@pytest.mark.parametrize("path", [
    "/proc/1/environ", "/proc/1/mem", "/proc/1/maps", "/etc/shadow", "/proc/kcore",
    "/var/run/docker.sock", "/run/docker.sock", "/root/.ssh/id_rsa", "/root/.claude/.credentials.json",
])
async def test_read_foreign_paths_denied(place, path):
    r = await sh(box(place), f"head -c 16 {path} >/dev/null && echo READ")
    assert "READ" not in r["stdout"], (path, r)


async def test_read_host_place_root_and_bridge_cwd_invisible(place):
    secret = place.root / "host-secret.txt"
    secret.write_text("synthetic-host-secret")
    r = await sh(box(place), (
        f"cat {secret} 2>/dev/null; cat {os.getcwd()}/pyproject.toml 2>/dev/null | head -1;"
        "grep -rls synthetic-host-secret /work /scratch /tmp /root /home /mnt /srv /var 2>/dev/null | head -3; echo END"))
    assert "synthetic-host-secret" not in r["stdout"], r
    assert "[tool" not in r["stdout"], r


async def test_read_no_credentials_in_image_or_env(place, monkeypatch):
    monkeypatch.setenv("ABASHR_SYNTHETIC_TOKEN", "abashr-synthetic-token")
    r = await sh(box(place), (
        "env; echo ---; "
        "find / -xdev \\( -name '*.pem' -o -name '*.key' -o -name '.credentials*' -o -name '.env' "
        "-o -name 'id_rsa*' -o -name '.netrc' -o -name 'config.json' \\) -readable 2>/dev/null "
        "| grep -v -e /usr/ -e site-packages -e /proc/ -e /etc/ssl/certs/ | head -20"))
    assert "abashr-synthetic-token" not in r["stdout"]
    env = r["stdout"].split("---")[0]
    for marker in ("TOKEN", "SECRET", "KEY", "PASSWORD", "ANTHROPIC", "DOCKER_HOST", "PROXY"):
        assert marker not in env.upper(), env
    assert r["stdout"].split("---")[1].strip() == "", r["stdout"]


# ---------------------------------------------------------------- 3 Schreiben
@pytest.mark.parametrize("target", [
    "/app/abashr", "/etc/abashr", "/usr/local/bin/abashr", "/bash_kind.py", "/abashr",
    "/proc/sys/kernel/hostname", "/sys/fs/cgroup/abashr", "/sys/kernel/abashr",
])
async def test_write_outside_place_denied(place, target):
    r = await sh(box(place), f"echo x > {target} && echo WROTE")
    assert "WROTE" not in r["stdout"], (target, r)


async def test_write_cannot_mount_or_chmod_material(place):
    b = box(place, material={"in/a.txt": b"immutable"})
    r = await sh(b, (
        "mount -t tmpfs none /work/in 2>/dev/null && echo MOUNTED;"
        "chmod 666 in/a.txt 2>/dev/null && echo CHMOD;"
        "echo y >> in/a.txt 2>/dev/null && echo APPENDED;"
        "rm -f in/a.txt 2>/dev/null; test -f in/a.txt || echo REMOVED; true"))
    assert not any(w in r["stdout"] for w in ("MOUNTED", "CHMOD", "APPENDED", "REMOVED")), r
    assert b.read("in/a.txt") == b"immutable"


async def test_write_nothing_lands_on_host_root(place):
    before = sorted(p.name for p in place.root.iterdir())
    r = await sh(box(place), "for d in / /tmp /scratch /dev/shm; do echo x > $d/abashr-probe 2>/dev/null; done; true")
    assert r["exit_code"] == 0
    with pytest.raises(RunError):  # undeclared file in the place: fail closed
        await sh(box(place), "echo x > /work/abashr-probe")
    assert sorted(p.name for p in place.root.iterdir()) == before
    assert not list(place.root.rglob("abashr-probe"))


# ---------------------------------------------------------------- 4 Prozesse
async def test_proc_host_invisible_and_caps_empty(place):
    host_pid = os.getpid()
    r = await sh(box(place), (
        "python - <<'C'\n"
        "import os\n"
        f"try:\n os.kill({host_pid}, 0)\nexcept ProcessLookupError: pass\nexcept PermissionError: pass\n"
        "else: raise SystemExit('host pid signalable')\n"
        "pids=[p for p in os.listdir('/proc') if p.isdigit()]\n"
        "assert len(pids) < 10, pids\n"
        "st=dict(l.split(':',1) for l in open('/proc/self/status').read().splitlines())\n"
        "print(st['CapEff'].strip(), st['CapPrm'].strip(), st['CapBnd'].strip(), st['NoNewPrivs'].strip(), st['Seccomp'].strip())\n"
        "C"))
    assert r["exit_code"] == 0, r
    eff, prm, bnd, nnp, secc = r["stdout"].split()
    assert int(eff, 16) == 0 and int(prm, 16) == 0
    assert nnp == "1" and secc == "2"
    # Recorded, not asserted: bounding set keeps CHOWN/SETUID/SETGID (bootstrap).
    print("CapBnd", bnd)


@pytest.mark.parametrize("command", ["kill -STOP 1", "kill -TERM 1", "kill -INT 1", "kill -9 -1", "kill -9 1"])
async def test_proc_signals_to_pid1_fail_closed(place, command):
    b = box(place)
    try:
        r = await sh(b, command + "; sleep 0.2; echo survived > result.txt")
        outcome = "result"
    except RunError as exc:
        outcome = exc.code  # fail closed: no partial snapshot accepted
        assert b.files == {}
    print("SIGNAL", command, outcome)
    assert leftover() == []
    assert (await sh(b, "echo second"))["stdout"] == "second\n"


async def test_proc_cannot_trace_pid1(place):
    r = await sh(box(place), "python -c 'open(\"/proc/1/mem\",\"rb\").read(1)' 2>/dev/null && echo TRACED;"
                 "cat /proc/1/fd/0 2>/dev/null && echo FD; ls /proc/1/root/ 2>/dev/null && echo ROOT; true")
    for w in ("TRACED", "FD", "ROOT"):
        assert w not in r["stdout"], r


async def test_proc_cannot_create_namespaces(place):
    r = await sh(box(place), (
        "unshare -Ur true 2>/dev/null && echo USERNS;"
        "unshare -n true 2>/dev/null && echo NETNS;"
        "python -c 'import os; os.setuid(0)' 2>/dev/null && echo ROOT;"
        "su -c true 2>/dev/null </dev/null && echo SU; true"))
    for w in ("USERNS", "NETNS", "ROOT", "SU"):
        assert w not in r["stdout"], r


# ---------------------------------------------------------------- 5 Ressourcen
async def test_res_fork_bomb_bounded_and_cleaned(place):
    b = box(place)
    try:
        await sh(b, "f(){ f|f & }; f; sleep 3; echo done")
    except RunError:
        pass
    assert leftover() == []
    assert (await sh(b, "echo after"))["stdout"] == "after\n"


async def test_res_memory_bomb_bounded(place):
    b = box(place)
    try:
        r = await sh(b, "python -c 'x=bytearray(3*1024**3); print(len(x))'")
        assert r["exit_code"] != 0
    except RunError:
        pass
    assert leftover() == []


@pytest.mark.parametrize("target", ["/work/scripts/big", "/scratch/big", "/dev/shm/big"])
async def test_res_disk_bounded(place, target):
    r = await sh(box(place), f"mkdir -p /work/scripts; head -c 300000000 /dev/zero > {target}; echo rc=$?; rm -f {target}")
    assert "rc=0" not in r["stdout"], r


async def test_res_cpu_loop_times_out_and_cleans(place):
    place.timeout_s = 2
    with pytest.raises(RunError):
        await sh(box(place), "while :; do :; done")
    assert leftover() == []


async def test_res_inode_flood_fails_closed(place):
    with pytest.raises(RunError):
        await sh(box(place), "mkdir -p scripts; cd scripts; for i in $(seq 5000); do : > f$i; done")
    assert leftover() == []


async def test_res_parallel_calls_are_independent(place):
    b1, b2 = box(place), box(place)
    r = await asyncio.gather(sh(b1, "echo a > result.txt"), sh(b2, "cat result.txt 2>/dev/null || echo none"))
    assert r[1]["stdout"] == "none\n"


# ---------------------------------------------------------------- 6 Docker
async def test_docker_socket_absent_inside(place):
    r = await sh(box(place), (
        "find / -xdev -type s 2>/dev/null | head; ls -la /var/run /run 2>/dev/null | grep -i docker;"
        "command -v docker; echo END"))
    assert "docker" not in r["stdout"].lower(), r


def test_docker_remote_daemon_and_mutable_tag_rejected(tmp_path):
    from src.agentischer_lauf.bash import BashPlace

    image = os.environ["ABASH_TEST_IMAGE"]
    with pytest.raises(ValueError):
        BashPlace(image=image, root=tmp_path, socket="tcp://127.0.0.1:2375")
    with pytest.raises(ValueError):
        BashPlace(image="bridge-erkunder:latest", root=tmp_path)


@pytest.mark.parametrize("path", ["a:/var/run/docker.sock", "x:rw", "../a", "/etc/passwd"])
async def test_docker_volume_injection_via_material_path_rejected(place, path):
    with pytest.raises((RunError, ValueError)):
        await place.run(command="true", material={path: b"x"}, files={}, outputs=[], max_bytes=1000)
    assert leftover() == []


# ---------------------------------------------------------------- Freigabe
@pytest.mark.parametrize("name", ["bash", "BASH", "Bash ", "Shell", "Exec", "Terminal", "Python", "Computer"])
def test_grant_other_names_are_422(name):
    with pytest.raises(RunError) as exc:
        Sandbox.validate_tools([Tool(name=name, parameter={})])
    assert failure(exc.value).status_code == 422


def test_grant_bash_with_parameter_is_422():
    with pytest.raises(RunError) as exc:
        Sandbox.validate_tools([Tool(name="Bash", parameter={"image": "x"})])
    assert failure(exc.value).status_code == 422


@pytest.mark.parametrize("name", ["Bash", "bash", "Read"])
async def test_grant_without_bash_never_starts(place, name):
    b = box(place, tools=("Read", "Write", "List"))
    with pytest.raises(RunError):
        await b.acall(name if name != "Read" else "Bash", {"command": "echo x"})
    with pytest.raises(RunError):
        b.call("Bash", {"command": "echo x"})
    assert leftover() == [] and not list(place.root.iterdir())


async def test_grant_bash_args_cannot_carry_extras(place):
    for args in ({"command": "true", "image": "alpine"}, {"command": "true", "network": "host"}, ["true"]):
        with pytest.raises(RunError):
            await box(place).acall("Bash", args)
