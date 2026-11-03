"""DEV-KEIN-PROD-HOP (KD-Entscheid 03.11.2026): the dev bridge does not send
unprioritised LLM traffic to the prod bridge any more — generator level, no
network, no docker. The real-nginx proof is test_dev_no_prod_hop_e2e.sh.

Pinned here:
  * the committed primary include is the PRIMARY_PEER_HOP=0 variant (optional),
  * the production include is byte-identical for every value of the switch,
  * PRIMARY_PEER_HOP=1 restores exactly the ADR-0010 routing,
  * the cross-host entries left on primary are an explicit, closed list — a new
    hop to ${BRIDGE_BACKUP_HOST} cannot appear without a red test,
  * a wrong switch value fails loud BEFORE any committed file is truncated.
Contract: docs/contracts/dev-kein-prod-hop.md.
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


def _generate(tmp_path: pathlib.Path, hop: str | None, target: str = "all"):
    out = tmp_path / f"hop-{hop}"
    out.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "PRIMARY_PEER_HOP"}
    env["UPSTREAMS_OUT_DIR"] = str(out)
    if hop is not None:
        env["PRIMARY_PEER_HOP"] = hop
    proc = subprocess.run(["bash", str(GENERATOR), target], cwd=REPO_ROOT,
                          env=env, capture_output=True, text=True)
    return proc, out


@pytest.fixture(scope="module")
def variants(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gen")
    result = {}
    for hop in (None, "0", "1"):
        proc, out = _generate(tmp, hop)
        assert proc.returncode == 0, proc.stderr
        result[hop] = {f: (out / f).read_text() for f in FILES}
    return result


def _block(text: str, head: str) -> str:
    m = re.search(re.escape(head) + r" \{(.*?)\n\}", text, re.S)
    assert m, f"{head} missing"
    return m.group(1)


def _map(text: str, head: str) -> dict[str, str]:
    entries = {}
    for line in _block(text, head).strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.rstrip(";").split(None, 1)
        entries[key.strip('"')] = value.strip()
    return entries


LLM_MAP_OFF = 'map "$bridge_hopped:$http_x_priority:$http_x_vision_provider" $llm_backend_pool'
LLM_MAP_ON = 'map "$bridge_hopped:$http_x_vision_provider" $llm_backend_pool'


def test_default_is_adr0010_and_committed(variants):
    """Without any setting the dev bridge follows ADR-0010 (Rafael 31.08.2026,
    peer-first) — and that is what is committed, i.e. what a dev LB deploy
    rolls out. The no-hop variant (0) is opt-in only."""
    assert variants[None] == variants["1"]
    committed = (DOCKER / "upstreams-primary.conf").read_text()
    if "ENERGY_SUBSCRIPTION_NO_HOP=1" in committed:
        # Schalterfenster Energy-Abo (03.11.2026): the committed include is the
        # NO_HOP=1 variant of the ADR-0010 default; the switch is checked in
        # test_energy_subscription_no_hop.py.
        return
    for f in FILES:
        assert (DOCKER / f).read_text() == variants[None][f], f


@pytest.mark.parametrize("f", ["upstreams-prod.conf", "worker-map-prod.conf",
                               "worker-map-primary.conf"])
def test_switch_never_changes_production(variants, f):
    """Prod stays byte-identical whatever the switch says (KD: prod untouched)."""
    assert variants["0"][f] == variants["1"][f]


def test_off_llm_pool_is_local_except_x_priority(variants):
    entries = _map(variants["0"]["upstreams-primary.conf"], LLM_MAP_OFF)
    assert entries == {
        "default": "claude_workers",
        "~*^0:production:gemini$": "claude_workers",
        "~*^0:production:": "claude_production",
    }


def test_off_keyed_jobs_live_here_unless_x_priority(variants):
    text = variants["0"]["upstreams-primary.conf"]
    assert _map(text, "map $http_x_priority $jobs_peer_first") == {
        "default": "0", "production": "1"}
    # nginx.conf names the block statically (@keyed_jobs_peer) — it must exist.
    assert "${BRIDGE_BACKUP_HOST}:8000" in _block(text, "upstream claude_jobs_keyed_peer")


def test_off_polls_of_prod_home_jobs_still_reach_the_peer(variants):
    """Jobs in flight with marker 'prod' must not die at the cutover."""
    entries = _map(variants["0"]["upstreams-primary.conf"],
                   'map "$bridge_hopped:$job_home_marker" $job_poll_pool')
    assert entries["0:prod"] == "claude_jobs_peer"
    assert entries["1:prod"] == "claude_jobs_home"
    assert entries["default"] == "$llm_backend_pool"   # = local now


def test_on_restores_adr_0010(variants):
    text = variants["1"]["upstreams-primary.conf"]
    assert _map(text, LLM_MAP_ON)["default"] == "claude_production"
    assert _map(text, "map $host $jobs_peer_first") == {"default": "1"}
    assert LLM_MAP_OFF not in text


def test_off_cross_host_entries_are_a_closed_list(variants):
    """Every upstream that can reach ${BRIDGE_BACKUP_HOST} on primary, named.
    Adding one is a decision for docs/contracts/dev-kein-prod-hop.md, not a
    side effect."""
    text = variants["0"]["upstreams-primary.conf"]
    with_peer = sorted(
        name for name, body in re.findall(r"upstream (\S+) \{(.*?)\n\}", text, re.S)
        if "BRIDGE_BACKUP_HOST" in body
    )
    assert with_peer == ["claude_jobs_keyed_peer", "claude_jobs_peer", "claude_production"]
    # claude_production: still remote-first, only for X-Priority: production.
    prod_pool = _block(text, "upstream claude_production")
    assert prod_pool.strip().splitlines()[0].strip().startswith("server ${BRIDGE_BACKUP_HOST}:8000")
    # The local pools have no cross-host entry at all.
    for name in ("claude_workers", "claude_version", "claude_jobs_home",
                 "claude_jobs_keyed_local"):
        assert "BRIDGE_BACKUP_HOST" not in _block(text, f"upstream {name}")


def test_shared_nginx_conf_backup_host_uses_are_known():
    """docker/nginx.conf is shared with prod and unchanged by this switch;
    its non-comment uses of ${BRIDGE_BACKUP_HOST} are sender trust (inbound)
    and the /health/production probe — none consumes an account."""
    uses = [
        line.strip() for line in (DOCKER / "nginx.conf").read_text().splitlines()
        if "BRIDGE_BACKUP_HOST" in line and not line.lstrip().startswith("#")
    ]
    assert uses == [
        "${BRIDGE_BACKUP_HOST} 1;",                                  # geo $sender_is_peer
        "${BRIDGE_BACKUP_HOST} 1;",                                  # geo $origin_sender_trusted
        "proxy_pass http://${BRIDGE_BACKUP_HOST}:8000/health;",     # /health/production
    ]


@pytest.mark.parametrize("bad", ["2", "yes", "on", " "])
def test_wrong_value_fails_before_writing(tmp_path, bad):
    out = tmp_path / f"hop-{bad}"
    out.mkdir()
    for f in FILES:
        shutil.copy(DOCKER / f, out / f)
    env = dict(os.environ, UPSTREAMS_OUT_DIR=str(out), PRIMARY_PEER_HOP=bad)
    proc = subprocess.run(["bash", str(GENERATOR), "all"], cwd=REPO_ROOT,
                          env=env, capture_output=True, text=True)
    assert proc.returncode != 0
    assert "PRIMARY_PEER_HOP must be 0 or 1" in proc.stderr
    for f in FILES:   # nothing truncated
        assert (out / f).read_text() == (DOCKER / f).read_text(), f
