import json
import subprocess
from pathlib import Path

from src.erkunder.arbeitsbereich import fresh_copy, isolated_command

root = Path("/probe")
step = root / "report" / "step"
step.mkdir(parents=True)
(step / "auftrag.md").write_text("Compute 1+1")
(step.parent / "eingang").mkdir()
(step.parent / "eingang" / "data.csv").write_text("a,b\n1,2\n")
fresh = fresh_copy(step, root / "attempts")
(step / "original.pdf").write_bytes(b"%PDF-1.4 original")
(step / "linked.pdf").symlink_to(step / "original.pdf")
code = r"""
import os, json, subprocess
from pathlib import Path

paths = {
    p: Path(p).exists()
    for p in [
        "original.pdf",
        "linked.pdf",
        "../step/original.pdf",
        "/probe/report/step/original.pdf",
        "/var/run/docker.sock",
    ]
}
status = dict(
    line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines()
)
print(
    json.dumps(
        {
            "uid": os.getuid(),
            "apparmor": Path("/proc/self/attr/current").read_text().strip(),
            "caps": {
                k: status[k].strip()
                for k in [
                    "CapEff",
                    "CapPrm",
                    "CapBnd",
                    "NoNewPrivs",
                    "Seccomp",
                    "Seccomp_filters",
                ]
            },
            "originals": paths,
            "csv": Path("../eingang/data.csv").read_text(),
        }
    ),
    flush=True,
)
home = Path.cwd() / ".home"
home.mkdir()
env = {**os.environ, "HOME": str(home), "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
try:
    result = subprocess.run(
        [
            "/usr/local/bin/claude",
            "-p",
            "Compute 1+1",
            "--permission-mode",
            "bypassPermissions",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    print(
        json.dumps(
            {
                "cli_returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
    )
except subprocess.TimeoutExpired:
    print(json.dumps({"cli_timeout": True}))
"""
result = subprocess.run(
    isolated_command(fresh, step, ["/app/.venv/bin/python", "-c", code]),
    capture_output=True,
    text=True,
)
print(
    json.dumps(
        {"exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    )
)
assert result.returncode == 0, result.stderr
lines = [json.loads(line) for line in result.stdout.splitlines()]
identity, cli = lines
assert identity["uid"] == 1101, identity
assert identity["caps"]["CapEff"] == identity["caps"]["CapPrm"] == "0000000000000000"
assert identity["caps"]["NoNewPrivs"] == "1"
assert identity["caps"]["Seccomp"] == "2"
assert int(identity["caps"]["Seccomp_filters"]) >= 2
assert not any(identity["originals"].values())
assert cli.get("cli_returncode") == 1 and "Not logged in" in cli.get("stdout", ""), cli
assert "cannot be used with root" not in cli.get("stderr", ""), cli

assert "erkunder-supervisor" in identity["apparmor"], identity
