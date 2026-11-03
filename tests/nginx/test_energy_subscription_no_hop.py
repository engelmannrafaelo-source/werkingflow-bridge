"""ENERGY_SUBSCRIPTION_NO_HOP (KD 03.11.2026, Energy-Abo-Modus): on the dev LB,
werking-energy never rides claude_production when the generator switch is on.
Default off = byte-identical committed files. Generator level, no network, no
docker. Contract: docs/contracts/energy-abo-bildweg.md (Abschnitt LB).

The routing semantics are checked by evaluating the generated nginx map with
nginx's rules (exact keys first, then regexes in order, ``~*`` case-insensitive,
else default) over representative request keys.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "scripts" / "generate-bridge-upstreams.sh"
DOCKER = REPO_ROOT / "docker"
FILES = ("upstreams-primary.conf", "upstreams-prod.conf",
         "worker-map-primary.conf", "worker-map-prod.conf")


def _generate(tmp_path, hop, energy):
    out = tmp_path / f"hop-{hop}-energy-{energy}"
    out.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("PRIMARY_PEER_HOP", "ENERGY_SUBSCRIPTION_NO_HOP")}
    env["UPSTREAMS_OUT_DIR"] = str(out)
    if hop is not None:
        env["PRIMARY_PEER_HOP"] = hop
    if energy is not None:
        env["ENERGY_SUBSCRIPTION_NO_HOP"] = energy
    proc = subprocess.run(["bash", str(GENERATOR), "all"], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return {f: (out / f).read_text() for f in FILES}


@pytest.fixture(scope="module")
def v(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gen")
    return {(h, e): _generate(tmp, h, e) for h in (None, "0", "1") for e in (None, "0", "1")}


def _map(text: str, var: str):
    m = re.search(r'map "?([^"\n{]*?)"? \$' + var + r" \{(.*?)\n\s*\}", text, re.S)
    assert m, var
    default, exact, regexes = None, {}, []
    for line in m.group(2).strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.rstrip(";").split(None, 1)
        key, value = key.strip('"'), value.strip()
        if key == "default":
            default = value
        elif key.startswith("~*"):
            regexes.append((re.compile(key[2:], re.I), value))
        elif key.startswith("~"):
            regexes.append((re.compile(key[1:]), value))
        else:
            exact[key] = value
    return m.group(1), default, exact, regexes


def _resolve(text: str, var: str, **vals) -> str:
    source, default, exact, regexes = _map(text, var)
    key = source
    for name, val in vals.items():
        key = key.replace(f"${name}", val)
    if "$" in key:
        key = re.sub(r"\$[a-z_]+", "", key)  # variables not given are empty
    if key in exact:
        return exact[key]
    for rx, value in regexes:
        if rx.search(key):
            return value
    return default


def _pool(text, hopped="0", prio="", vision="", app=""):
    return _resolve(text, "llm_backend_pool", bridge_hopped=hopped, http_x_priority=prio,
                    http_x_vision_provider=vision, http_x_app_id=app)


def _jobs_first(text, prio="", app=""):
    return _resolve(text, "jobs_peer_first", http_x_priority=prio, http_x_app_id=app, host="bridge")


def test_default_off_is_byte_identical_and_committed(v):
    for hop in (None, "0", "1"):
        assert v[(hop, None)] == v[(hop, "0")]
    for f in FILES:
        assert v[(None, None)][f] == (DOCKER / f).read_text(), f


@pytest.mark.parametrize("hop", [None, "0", "1"])
def test_switch_never_changes_production(v, hop):
    for f in ("upstreams-prod.conf", "worker-map-prod.conf"):
        assert v[(hop, "1")][f] == v[(hop, "0")][f]


@pytest.mark.parametrize("hop", ["1", "0"])
@pytest.mark.parametrize("prio", ["", "production"])
@pytest.mark.parametrize("app", ["werking-energy", "Werking-Energy", " werking-energy "])
def test_on_energy_never_rides_production(v, hop, prio, app):
    on = v[(hop, "1")]["upstreams-primary.conf"]
    assert _pool(on, prio=prio, app=app) == "claude_workers"
    assert _jobs_first(on, prio=prio, app=app) == "0"


@pytest.mark.parametrize("hop", ["1", "0"])
@pytest.mark.parametrize("case", [
    dict(hopped="0", prio="", vision="", app=""),
    dict(hopped="0", prio="", vision="", app="werking-report"),
    dict(hopped="0", prio="production", vision="", app="werking-report"),
    dict(hopped="0", prio="production", vision="gemini", app="werking-report"),
    dict(hopped="0", prio="", vision="gemini", app=""),
    dict(hopped="1", prio="production", vision="", app="werking-report"),
    dict(hopped="0", prio="", vision="", app="werking-energy-x"),
    dict(hopped="0", prio="", vision="", app="not-werking-energy"),
])
def test_on_everyone_else_unchanged(v, hop, case):
    off = v[(hop, "0")]["upstreams-primary.conf"]
    on = v[(hop, "1")]["upstreams-primary.conf"]
    assert _pool(on, **case) == _pool(off, **case), case
    jc = dict(prio=case["prio"], app=case["app"])
    assert _jobs_first(on, **jc) == _jobs_first(off, **jc), case


def test_off_energy_still_hops_with_adr0010(v):
    """Without the switch nothing is special about Energy (ADR-0010 stays)."""
    off = v[("1", "0")]["upstreams-primary.conf"]
    assert _pool(off, app="werking-energy") == "claude_production"


def test_job_polls_of_prod_home_still_reach_peer(v):
    on = v[("1", "1")]["upstreams-primary.conf"]
    off = v[("1", "0")]["upstreams-primary.conf"]
    poll = lambda t: re.search(r"map \"\$bridge_hopped:\$job_home_marker\" \$job_poll_pool \{(.*?)\n\}", t, re.S).group(1)  # noqa: E731
    assert poll(on) == poll(off) and "claude_jobs_peer" in poll(on)


@pytest.mark.parametrize("bad", ["2", "yes", " "])
def test_wrong_value_fails_before_writing(tmp_path, bad):
    out = tmp_path / "bad"
    out.mkdir()
    for f in FILES:
        shutil.copy(DOCKER / f, out / f)
    env = dict(os.environ, UPSTREAMS_OUT_DIR=str(out), ENERGY_SUBSCRIPTION_NO_HOP=bad)
    proc = subprocess.run(["bash", str(GENERATOR), "all"], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True)
    assert proc.returncode != 0 and "ENERGY_SUBSCRIPTION_NO_HOP must be 0 or 1" in proc.stderr
    for f in FILES:
        assert (out / f).read_text() == (DOCKER / f).read_text(), f


# --- location / : $backend_pool in the shared nginx.conf (Zweitprüfung B2) ----

NGINX_CONF = (DOCKER / "nginx.conf").read_text()
_GRID = [dict(hopped=h, prio=p, vision=vi, app=a)
         for h in ("0", "1") for p in ("", "production", "PRODUCTION", "low")
         for vi in ("", "gemini", "GEMINI", "anthropic")
         for a in ("", "werking-energy", "Werking-Energy", "werking-report", "werking-energy-x")]


def _backend_pool(include: str, hopped, prio, vision, app) -> str:
    llm = _pool(include, hopped=hopped, prio=prio, vision=vision, app=app)
    return _resolve(NGINX_CONF, "backend_pool", bridge_hopped=hopped, http_x_priority=prio,
                    http_x_vision_provider=vision, llm_backend_pool=llm, http_x_app_id=app)


def _old_backend_pool(hopped, prio, **_):
    return "claude_production" if f"{hopped}:{prio}" == "0:production" else "claude_workers"


@pytest.mark.parametrize("hop", [None, "0", "1"])
@pytest.mark.parametrize("topology", ["upstreams-primary.conf", "upstreams-prod.conf"])
def test_backend_pool_unchanged_without_switch(v, hop, topology):
    """Switch off (and on the prod LB always): the reworked map answers like
    the former "0:production" -> claude_production for every request shape."""
    include = v[(hop, "0")][topology]
    for case in _GRID:
        assert _backend_pool(include, **case) == _old_backend_pool(**case), case


@pytest.mark.parametrize("hop", ["0", "1"])
def test_backend_pool_switch_on_keeps_energy_local_only(v, hop):
    on = v[(hop, "1")]["upstreams-primary.conf"]
    for case in _GRID:
        want = _old_backend_pool(**case)
        if (case["hopped"] == "0" and case["prio"] == "production"
                and case["app"].lower() == "werking-energy" and case["vision"].lower() != "gemini"):
            want = "claude_workers"
        assert _backend_pool(on, **case) == want, case
