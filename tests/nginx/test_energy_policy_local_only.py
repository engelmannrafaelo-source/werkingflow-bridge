"""E1 (06.11.2026): werking-energy may not reach the prod peer via the account picker.

Measured 06.11.2026 21:33Z on the dev LB (ENERGY_SUBSCRIPTION_NO_HOP=1 committed):
an Energy subscription chat call came back with X-Bridge-Served-By prod/… and
funding_policy promotional_only. Cause: on `location ~ ^/v1/(chat/completions|research)`
the operator account picker (pool_router.choose -> bridge_account_selection.select)
restricts itself to local workers only for a real peer policy hop
($bridge_policy_hopped); a picked account on a non-local worker routes to
`bridge_policy_peer`. $llm_backend_pool (the NO_HOP switch) never reaches it.

Fix pinned here: the generator emits `$energy_policy_local_only` ONLY on primary with
ENERGY_SUBSCRIPTION_NO_HOP=1, and the picker treats it like a policy hop.

  * generator/static: committed includes = generator output; the variable exists
    only in primary+switch 1; prod output identical for both switch values;
  * real OpenResty + real Lua (no network): policy stub with an account on a
    NON-local worker ranked first and a local one second —
      primary include before the fix (no map)  -> Energy goes to the peer,
      primary include with the fix             -> Energy local, other apps peer,
      prod include                             -> unchanged (peer for all);
    and with ONLY the non-local account Energy gets no peer but the existing
    no-capacity answer (all_unavail). Skipped without docker / the openresty image.
Contract: docs/contracts/energy-abo-bildweg.md §3.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKER = REPO_ROOT / "docker"
GENERATOR = REPO_ROOT / "scripts" / "generate-bridge-upstreams.sh"
IMAGE = "openresty/openresty:1.27.1.1-alpine"
REGEX_HEAD = "location ~ ^/v1/(chat/completions|research)"
VAR = "energy_policy_local_only"


def _generate(tmp_path: Path, energy: str | None, hop: str | None = None) -> dict[str, str]:
    out = tmp_path / f"gen-{energy}-{hop}"
    out.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if k not in ("PRIMARY_PEER_HOP", "ENERGY_SUBSCRIPTION_NO_HOP")}
    env["UPSTREAMS_OUT_DIR"] = str(out)
    if energy is not None:
        env["ENERGY_SUBSCRIPTION_NO_HOP"] = energy
    if hop is not None:
        env["PRIMARY_PEER_HOP"] = hop
    subprocess.run(["bash", str(GENERATOR), "all"], cwd=REPO_ROOT, env=env,
                   check=True, capture_output=True)
    return {t: (out / f"upstreams-{t}.conf").read_text() for t in ("primary", "prod")}


def _map_block(include: str) -> str:
    m = re.search(r"^map \$http_x_app_id \$" + VAR + r" \{.*?^\}", include, re.S | re.M)
    return m.group(0) if m else ""


@pytest.mark.parametrize("hop", [None, "0", "1"])
def test_variable_only_on_primary_with_switch(tmp_path, hop):
    on, off, unset = (_generate(tmp_path, e, hop) for e in ("1", "0", None))
    assert _map_block(on["primary"]), "switch 1 must emit the picker map on primary"
    for gen in (off, unset):
        assert VAR not in gen["primary"]
        assert gen["primary"] == unset["primary"]
    assert on["prod"] == off["prod"] == unset["prod"]
    assert VAR not in on["prod"]


def test_committed_includes_are_generator_output(tmp_path):
    gen = _generate(tmp_path, "1")
    assert (DOCKER / "upstreams-primary.conf").read_text() == gen["primary"]
    assert (DOCKER / "upstreams-prod.conf").read_text() == gen["prod"]
    assert VAR not in (DOCKER / "nginx.conf").read_text()  # never a shared definition


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE],
                          capture_output=True).returncode == 0


def _policy(with_local: bool) -> str:
    acc = ('"{n}":{{"worker":"{w}","bridge_enabled":true,"bridge_priority":{p},'
           '"bridge_policy_revision":1,"available":true,"cooldown_remaining_s":0,'
           '"effective_cap_tokens":100000,"current_in_flight_tokens":0,'
           '"usage_known":true,"weekly_percent":10}}')
    parts = [acc.format(n="office", w="worker-sahori", p=1)]  # prod worker, ranked first
    if with_local:
        parts.append(acc.format(n="local1", w="worker1", p=2))
    return '{"revision":1,"accounts":{' + ",".join(parts) + "}}"


def _conf(map_block: str, policy: str) -> str:
    locals_ = "\n".join(
        f"  upstream {n} {{ server 127.0.0.1:9001; }}"
        for n in ["claude_workers"] + [f"bridge_policy_worker{i}" for i in range(1, 5)])
    return f"""
env BRIDGE_WORKERS=worker1,worker2,worker3,worker4;
env BRIDGE_ACCOUNT_POLICY_URL=http://127.0.0.1:9003;
env BRIDGE_ACCOUNT_POLICY_TOKEN=probe;
env METRICS_READER_TARGET;
events {{}}
http {{
  access_log off;
  lua_package_path "/w/lua/?.lua;/w/lua-vendor/?.lua;;";
  lua_shared_dict pool_state 1m;
  map $http_x_bridge_policy_hop $bridge_policy_hopped {{ default 0; }}
{map_block}
{locals_}
  upstream bridge_policy_peer {{ server 127.0.0.1:9002; }}
  server {{ listen 127.0.0.1:9001; location / {{ return 200 "local\\n"; }} }}
  server {{ listen 127.0.0.1:9002; location / {{ return 200 "peer\\n"; }} }}
  server {{ listen 127.0.0.1:9003; location = /runtime {{ return 200 '{policy}'; }} }}
  server {{
    listen 8080;
    {REGEX_HEAD} {{
      set $target_worker "pending";
      set $x_pool_decision "";
      set $pool_exhausted "";
      set $affinity_upstream claude_workers;
      set $x_cache_affinity "";
      access_by_lua_block {{
        require("pool_router").choose({{ overflow_capable = false }})
        if ngx.var.target_worker == "unavailable" then
          ngx.say("unavailable:" .. ngx.var.x_pool_decision)
          return ngx.exit(200)
        end
      }}
      proxy_pass http://$affinity_upstream;
    }}
  }}
}}
"""


APPS = ["werking-energy", "WERKING-Energy", "werking-energy-x", "werking-report", ""]


def _run(tmp_path: Path, conf: str) -> list[str]:
    work = Path(tempfile.mkdtemp(dir=tmp_path))
    (work / "t.conf").write_text(conf)
    shutil.copytree(DOCKER / "lua", work / "lua")
    shutil.copytree(DOCKER / "lua-vendor", work / "lua-vendor")
    work.chmod(0o755)  # nginx workers run as nobody and must traverse /w
    script = ["mkdir -p /tmp/logs",
              "openresty -t -c /w/t.conf -p /tmp >/dev/null 2>&1 || { openresty -t -c /w/t.conf -p /tmp; exit 7; }",
              "openresty -c /w/t.conf -p /tmp", "sleep 0.3"]
    for app in APPS:
        hdr = f"--header 'X-App-ID: {app}'" if app else ""
        script.append(f"wget -qO- {hdr} --post-data='{{}}' "
                      f"'http://127.0.0.1:8080/v1/chat/completions' || echo ERR")
    script.append("grep -c 'unknown variable\\|\\[error\\]' /tmp/logs/error.log || true")
    r = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{work}:/w:ro",
                        IMAGE, "sh", "-c", "\n".join(script)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    got = r.stdout.strip().splitlines()
    assert len(got) == len(APPS) + 1, r.stdout
    assert got[-1] == "0", "error.log carries errors: " + r.stdout
    return got[:-1]


PEER_ALL = ["peer"] * len(APPS)


@pytest.mark.skipif(not _docker_ok(), reason="docker/openresty image not available")
@pytest.mark.parametrize("variant,with_local,want", [
    # before the fix (primary include without the map) = the 21:33Z incident
    ("primary-before", True, PEER_ALL),
    ("primary", True, ["local", "local", "peer", "peer", "peer"]),
    ("prod", True, PEER_ALL),
    # no local account released: Energy gets the existing no-capacity answer, never the peer
    ("primary", False, ["unavailable:all_unavail", "unavailable:all_unavail", "peer", "peer", "peer"]),
    ("prod", False, PEER_ALL),
])
def test_real_picker_keeps_energy_local(tmp_path, variant, with_local, want):
    if variant == "prod":
        block = _map_block((DOCKER / "upstreams-prod.conf").read_text())
        assert block == ""
    elif variant == "primary-before":
        block = ""
    else:
        block = _map_block((DOCKER / "upstreams-primary.conf").read_text())
        assert block
    got = _run(tmp_path, _conf(block, _policy(with_local)))
    assert got == want, (variant, with_local, got)
