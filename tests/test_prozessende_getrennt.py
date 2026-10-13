"""BRUMBAU2R P2-5: the *_prozessende modules leave a mixed run loudly and run
on their own (tests/conftest.py, scripts/test-prozessende.sh)."""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
MODUL = "tests/unit/test_cli_frist_prozessende.py"
ANDERES = "tests/test_version_delivery.py"


def sammle(*pfade):
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider",
         *pfade],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
        env={k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")},
    )
    return out.stdout + out.stderr


def gesammelt(out):
    treffer = re.search(
        r"(\d+)(?:/(\d+))? tests? collected(?: \((\d+) deselected\))?", out
    )
    assert treffer, out
    return int(treffer.group(1)), int(treffer.group(3) or 0)


def test_mixed_run_leaves_prozessende_out_loudly():
    allein, _ = gesammelt(sammle(MODUL))
    anderes, _ = gesammelt(sammle(ANDERES))
    out = sammle(MODUL, ANDERES)
    assert gesammelt(out) == (anderes, allein)
    assert "PROZESSENDE NICHT GELAUFEN" in out and "scripts/test-prozessende.sh" in out
    assert "test_cli_frist_prozessende.py" in out


def test_prozessende_alone_is_collected():
    out = sammle(MODUL)
    assert gesammelt(out)[0] > 0 and gesammelt(out)[1] == 0
    assert "PROZESSENDE NICHT GELAUFEN" not in out


def test_runner_finds_every_prozessende_module():
    """The runner's globs cover every module the conftest leaves out."""
    text = (ROOT / "scripts/test-prozessende.sh").read_text()
    muster = re.findall(r"(tests/\S*\*_prozessende\.py)", text)
    module = list(ROOT.glob("tests/**/*_prozessende.py"))
    assert module and muster
    for modul in module:
        rel = modul.relative_to(ROOT)
        assert any(
            rel.match(m) and len(rel.parts) == len(Path(m).parts) for m in muster
        ), rel
