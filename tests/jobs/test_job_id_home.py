"""ADR-0012: the job id names the bridge whose store holds the job.

Two things are pinned here and nothing else belongs in this file:

  1. the GRAMMAR (what is a job id, what is a legacy id, what is junk), and
  2. that nginx and Python read that grammar THE SAME WAY. The router in
     docker/nginx.conf and the minter in src/jobs/job_id.py are two
     implementations of one format; the day they disagree, polls are routed to
     a bridge that does not have the row and the answer is a 404 for a healthy
     job — the exact failure this ADR removes.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from src.jobs.job_id import (
    NGINX_JOB_MARKER_REGEX,
    JobHomeUnconfigured,
    JobIdMalformed,
    home_bridge_id,
    is_legacy,
    new_job_id,
    parse_home,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
NGINX_CONF = REPO_ROOT / "docker" / "nginx.conf"

HEX32 = "0123456789abcdef" * 2


# ── Grammar ─────────────────────────────────────────────────────────────────

def test_tagged_id_yields_its_home():
    assert parse_home(f"job_prod_{HEX32}") == "prod"
    assert parse_home(f"job_dev_{HEX32}") == "dev"
    # Markers may carry hyphens (the deploy probe uses "validate-probe").
    assert parse_home(f"job_validate-probe_{HEX32}") == "validate-probe"


def test_legacy_id_is_recognised_not_rejected():
    """Pre-ADR-0012 ids must stay answerable during the rollout window."""
    assert parse_home(f"job_{HEX32}") is None
    assert is_legacy(f"job_{HEX32}") is True
    assert is_legacy(f"job_dev_{HEX32}") is False


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "job_",
        "deadbeef",
        "job_dev_",                      # marker, no id
        f"job_{HEX32[:31]}",             # 31 hex — truncated
        f"job_dev_{HEX32}extra",         # trailing junk
        f"job_DEV_{HEX32}",              # uppercase marker
        f"job_de_v_{HEX32}",             # underscore inside the marker
        f"job_dev_{HEX32.upper()}",      # uppercase hex
        "job_dev_" + "z" * 32,           # not hex
        "../../etc/passwd",
    ],
)
def test_malformed_ids_fail_loud(bad):
    """Junk is a broken id, never a missing one — it must not reach the 404
    path that tells a caller its job expired."""
    with pytest.raises(JobIdMalformed):
        parse_home(bad)


def test_non_string_fails_loud():
    with pytest.raises(JobIdMalformed):
        parse_home(None)  # type: ignore[arg-type]


# ── Minting ─────────────────────────────────────────────────────────────────

def test_new_job_id_carries_this_bridge(monkeypatch):
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "prod")
    job_id = new_job_id()
    assert job_id.startswith("job_prod_")
    assert parse_home(job_id) == "prod"


def test_new_job_id_is_unique(monkeypatch):
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "dev")
    assert new_job_id() != new_job_id()


def test_home_id_is_normalised(monkeypatch):
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "  PROD \n")
    assert home_bridge_id() == "prod"


def test_missing_home_id_fails_closed(monkeypatch):
    """No silent untagged fallback: an unroutable id would look like success
    and reproduce the cross-bridge 404 invisibly (ADR-0011 point-5 polarity)."""
    monkeypatch.delenv("BRIDGE_ORIGIN_ID", raising=False)
    with pytest.raises(JobHomeUnconfigured):
        home_bridge_id()
    with pytest.raises(JobHomeUnconfigured):
        new_job_id()


# "Dev" is deliberately absent: case is NORMALISED, not rejected (see
# test_home_id_is_normalised). Only values that cannot become a valid marker
# by trimming and lowercasing belong here.
@pytest.mark.parametrize("bad", ["dev_two", "a" * 17, "-dev", "dev bridge", "dev."])
def test_unusable_home_id_fails_closed(monkeypatch, bad):
    """A marker nginx's map regex could not match (or could split differently)
    must be refused where it is configured, not where it is polled."""
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", bad)
    with pytest.raises(JobHomeUnconfigured):
        home_bridge_id()


# ── nginx ⇄ Python: one grammar, two readers ────────────────────────────────

def test_nginx_conf_carries_the_marker_regex_verbatim():
    """docker/nginx.conf must contain the regex src/jobs/job_id.py publishes.

    A copy that drifts is the whole hazard: nginx would route on one grammar
    while workers mint another, and the mismatch shows up as a 404 in
    production, not as a failing test."""
    conf = NGINX_CONF.read_text()
    assert NGINX_JOB_MARKER_REGEX in conf, (
        "docker/nginx.conf no longer contains src.jobs.job_id."
        "NGINX_JOB_MARKER_REGEX verbatim — the router and the id minter have "
        f"drifted. Expected to find: {NGINX_JOB_MARKER_REGEX}"
    )


def _nginx_marker(uri: str) -> str | None:
    """What nginx's map would put in $job_home_marker for this URI.

    PCRE names groups `(?<x>…)`, Python `(?P<x>…)` — same regex, one character
    of dialect. Everything else is compared as-is on purpose.
    """
    pattern = NGINX_JOB_MARKER_REGEX.replace("(?<", "(?P<")
    m = re.match(pattern, uri)
    return m.group("jhm") if m else None


@pytest.mark.parametrize(
    "job_id",
    [
        f"job_prod_{HEX32}",
        f"job_dev_{HEX32}",
        f"job_validate-probe_{HEX32}",
        f"job_{HEX32}",                  # legacy → no marker on either side
        f"job_dev_{HEX32}extra",         # junk → no marker on either side
        "job_dev_nothex",
    ],
)
def test_nginx_regex_and_python_parser_agree(job_id):
    """The marker nginx extracts is exactly the home Python reads — including
    'nothing' for legacy and malformed ids, where nginx must fall back to the
    old routing and let the worker answer."""
    try:
        python_home = parse_home(job_id)
    except JobIdMalformed:
        python_home = None
    assert _nginx_marker(f"/v1/jobs/{job_id}") == python_home


def test_nginx_regex_ignores_other_job_subpaths():
    """Anchored on purpose: an unknown /v1/jobs/… subpath added later must fall
    through to the pre-ADR routing instead of being routed on a half-parsed
    marker."""
    assert _nginx_marker(f"/v1/jobs/job_dev_{HEX32}/cancel") is None
    assert _nginx_marker("/v1/jobs/health") is None
