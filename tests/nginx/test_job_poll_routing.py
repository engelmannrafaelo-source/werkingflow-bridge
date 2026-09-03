"""ADR-0012 in nginx: the poll follows the job's marker, in both directions.

These assertions are about the GENERATED includes, because that is where the
per-bridge half of the routing lives (docker/nginx.conf is topology-agnostic
by ADR-0006 and must stay so). What matters:

  * both bridges route their OWN marker to a local-only pool,
  * both bridges forward the PEER's marker to the peer — the mechanism is
    symmetric, which is the whole point of keeping the overflow (Rafael,
    2026-09-03: all eight workers stay usable),
  * an unmarked id keeps the pre-ADR routing, so the LB half and the app half
    can be deployed in either order without killing jobs in flight, and
  * a poll that already crossed once is never forwarded again.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "scripts" / "generate-bridge-upstreams.sh"
INCLUDES = {
    "primary": REPO_ROOT / "docker" / "upstreams-primary.conf",
    "production": REPO_ROOT / "docker" / "upstreams-prod.conf",
}
# Which marker each topology considers "the other bridge".
PEER = {"primary": "prod", "production": "dev"}


@pytest.fixture(scope="module")
def generated():
    """Run the generator and read what it wrote — the committed includes are
    generated artefacts, so the test must prove the GENERATOR produces this,
    not that somebody hand-edited a file that says DO NOT EDIT."""
    subprocess.run(
        ["bash", str(GENERATOR), "all"], cwd=REPO_ROOT, check=True,
        capture_output=True, text=True,
    )
    return {k: v.read_text() for k, v in INCLUDES.items()}


@pytest.mark.parametrize("topology", ["primary", "production"])
def test_committed_include_matches_the_generator(generated, topology):
    """Guards against a hand-edit landing in the repo (bridge-parity-check
    compares the CONTAINER against these files, so a drifted file would be
    'verified' on both bridges)."""
    assert INCLUDES[topology].read_text() == generated[topology]


@pytest.mark.parametrize("topology", ["primary", "production"])
def test_home_pool_has_no_cross_bridge_backup(generated, topology):
    """A poll for OUR job must never fail over to the peer: the peer holds a
    different store (ADR-0009), so asking it is guaranteed to be wrong — it
    would rebuild the bug from the other side."""
    block = re.search(
        r"upstream claude_jobs_home \{(.*?)\n\}", generated[topology], re.S
    )
    assert block, "claude_jobs_home upstream missing"
    body = block.group(1)
    assert "BRIDGE_BACKUP_HOST" not in body
    assert "backup" not in body


@pytest.mark.parametrize("topology", ["primary", "production"])
def test_peer_pool_points_at_the_other_bridge(generated, topology):
    block = re.search(
        r"upstream claude_jobs_peer \{(.*?)\n\}", generated[topology], re.S
    )
    assert block, "claude_jobs_peer upstream missing"
    assert "${BRIDGE_BACKUP_HOST}:8000" in block.group(1)


def _poll_map(text: str) -> dict[str, str]:
    block = re.search(
        r'map "\$bridge_hopped:\$job_home_marker" \$job_poll_pool \{(.*?)\n\}',
        text, re.S,
    )
    assert block, "$job_poll_pool map missing"
    entries = {}
    for line in block.group(1).strip().splitlines():
        key, value = line.strip().rstrip(";").split(None, 1)
        entries[key.strip('"')] = value.strip()
    return entries


@pytest.mark.parametrize("topology", ["primary", "production"])
def test_own_marker_routes_home_hopped_or_not(generated, topology):
    entries = _poll_map(generated[topology])
    assert entries["0:${BRIDGE_ID}"] == "claude_jobs_home"
    assert entries["1:${BRIDGE_ID}"] == "claude_jobs_home"


@pytest.mark.parametrize("topology", ["primary", "production"])
def test_peer_marker_is_forwarded_once_and_only_once(generated, topology):
    entries = _poll_map(generated[topology])
    peer = PEER[topology]
    assert entries[f"0:{peer}"] == "claude_jobs_peer", "must follow the job across"
    assert entries[f"1:{peer}"] == "claude_jobs_home", (
        "a poll that already hopped must NOT be forwarded again — that is the "
        "ADR-0010 one-hop loop guard; the local worker answers 421 instead"
    )


@pytest.mark.parametrize("topology", ["primary", "production"])
def test_unmarked_id_keeps_the_pre_adr_routing(generated, topology):
    """The transition rule. Without it, deploying the LB before the app would
    reject every job id in flight — a total outage at cutover."""
    assert _poll_map(generated[topology])["default"] == "$llm_backend_pool"


def test_the_two_bridges_are_mirror_images(generated):
    """dev→prod and prod→dev must be the same mechanism, not two special
    cases: Rafael's decision was to KEEP the overflow (all eight workers
    usable), which only works if a job can be followed in either direction."""
    prim, prod = _poll_map(generated["primary"]), _poll_map(generated["production"])
    assert prim.keys() ^ prod.keys() == {"0:prod", "1:prod", "0:dev", "1:dev"}
    assert prim["0:prod"] == prod["0:dev"] == "claude_jobs_peer"
    assert prim["1:prod"] == prod["1:dev"] == "claude_jobs_home"


def test_nginx_conf_routes_polls_by_marker():
    """The shared config must consume $job_poll_pool — riding
    $llm_backend_pool is what sent polls to the pool's first server regardless
    of where the row was written."""
    conf = (REPO_ROOT / "docker" / "nginx.conf").read_text()
    location = re.search(r"location ~ \^/v1/jobs/ \{(.*?)\n        \}", conf, re.S)
    assert location, "job-poll location missing"
    assert "proxy_pass http://$job_poll_pool;" in location.group(1)
    assert "proxy_pass http://$llm_backend_pool;" not in location.group(1)


def test_nginx_conf_stays_topology_agnostic():
    """ADR-0006 B/C: the per-bridge pools belong in the generated include,
    never in the shared file.

    Comments are stripped before scanning — nginx.conf explains WHERE the
    pools come from and must be allowed to name them in prose. Reading a
    doc-comment as config is its own recurring bug class (a comment blocked a
    deploy on 2026-09-03); a scanner that cannot tell them apart earns
    false findings, not safety."""
    lines = [
        line for line in (REPO_ROOT / "docker" / "nginx.conf").read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    code = "\n".join(lines)
    assert "claude_jobs_home" not in code
    assert "claude_jobs_peer" not in code
