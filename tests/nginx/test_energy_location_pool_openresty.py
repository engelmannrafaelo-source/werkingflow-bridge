"""Real-OpenResty proof for the `location /` pool (Energy-Abo-Modus, B2).

Evaluates, inside an OpenResty container WITHOUT network, the pool maps of the
shared docker/nginx.conf together with each generated include, and compares
the effective `location /` pool with the map of develop 69155c0 (old:
"0:production" -> claude_production, nginx string match = case-insensitive):

* switch 0, primary (PRIMARY_PEER_HOP 1 and 0) and the production include:
  identical for every request in the grid (X-Priority incl. Production,
  PRODUCTION, production:x; X-App-ID variants; X-Vision-Provider; hop);
* switch 1: identical except werking-energy (case-insensitive) with an old
  claude_production, which becomes claude_workers.

Skipped without docker or without the openresty image.
"""
from __future__ import annotations

import itertools
import os
import re
import shutil
import subprocess

import pytest

from test_energy_subscription_no_hop import DOCKER, GENERATOR, REPO_ROOT  # same directory

IMAGE = "openresty/openresty:1.27.1.1-alpine"
PRIOS = ["", "production", "Production", "PRODUCTION", "production:x", "low"]
APPS = ["", "werking-energy", "Werking-Energy", "WERKING-ENERGY", "werking-report", "werking-energy-x"]
VISION = ["", "gemini", "GEMINI"]
HOPS = ["", "1"]


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not _docker_ok(), reason="docker/openresty image not available")


def _maps(text: str, names) -> str:
    out = []
    for name in names:
        m = re.search(r'\n\s*map [^\n]*\$' + name + r' \{.*?\n\s*\}', text, re.S)
        assert m, name
        out.append(m.group(0))
    return "\n".join(out)


def _conf(include: str) -> str:
    nginx = (DOCKER / "nginx.conf").read_text()
    shared = _maps(nginx, ["bridge_hopped", "backend_pool", "backend_pool_location"])
    inc = _maps(include, ["llm_backend_pool", "jobs_peer_first"])
    return f"""
events {{}}
http {{
  access_log off;
{shared}
{inc}
  map "$bridge_hopped:$http_x_priority" $old_backend_pool {{
      "0:production"  claude_production;
      default         claude_workers;
  }}
  server {{
    listen 8080;
    location / {{ return 200 "$backend_pool_location|$old_backend_pool\\n"; }}
  }}
}}
"""


def _generate(tmp_path, hop, energy):
    out = tmp_path / f"{hop}-{energy}"
    out.mkdir(exist_ok=True)
    env = dict(os.environ, UPSTREAMS_OUT_DIR=str(out), PRIMARY_PEER_HOP=hop, ENERGY_SUBSCRIPTION_NO_HOP=energy)
    subprocess.run(["bash", str(GENERATOR), "all"], cwd=REPO_ROOT, env=env, check=True, capture_output=True)
    return {f: (out / f).read_text() for f in ("upstreams-primary.conf", "upstreams-prod.conf")}


def _run(tmp_path, include: str):
    import tempfile
    work = __import__("pathlib").Path(tempfile.mkdtemp(dir=tmp_path))
    (work / "t.conf").write_text(_conf(include))
    cases = list(itertools.product(HOPS, PRIOS, VISION, APPS))
    script = ["mkdir -p /tmp/logs", "openresty -t -c /w/t.conf -p /tmp >/dev/null 2>&1 || exit 7",
              "openresty -c /w/t.conf -p /tmp", "sleep 0.3"]
    for hop, prio, vis, app in cases:
        hdr = []
        for name, val in (("X-Bridge-Hop", hop), ("X-Priority", prio), ("X-Vision-Provider", vis), ("X-App-ID", app)):
            if val:
                hdr.append(f"--header '{name}: {val}'")
        script.append(f"wget -qO- {' '.join(hdr)} http://127.0.0.1:8080/")
    r = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{work}:/w:ro", IMAGE,
                        "sh", "-c", "\n".join(script)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    lines = r.stdout.strip().splitlines()
    assert len(lines) == len(cases), r.stdout[-500:]
    return [(c, *line.split("|")) for c, line in zip(cases, lines)]


@pytest.fixture(scope="module")
def tmp(tmp_path_factory):
    return tmp_path_factory.mktemp("or")


@pytest.mark.parametrize("hop", ["1", "0"])
def test_switch_off_identical_to_old_map(tmp, hop):
    gen = _generate(tmp, hop, "0")
    for topo in ("upstreams-primary.conf", "upstreams-prod.conf"):
        for case, new, old in _run(tmp, gen[topo]):
            assert new == old, (topo, case)


@pytest.mark.parametrize("hop", ["1", "0"])
def test_switch_on_only_energy_differs(tmp, hop):
    gen = _generate(tmp, hop, "1")
    for case, new, old in _run(tmp, gen["upstreams-primary.conf"]):
        energy = case[3].strip().lower() == "werking-energy"
        want = "claude_workers" if (energy and old == "claude_production") else old
        assert new == want, case
    for case, new, old in _run(tmp, gen["upstreams-prod.conf"]):
        assert new == old, ("prod", case)
