"""Run actual deploy orchestration with inert stubs; no hosts or credentials."""
import os
from pathlib import Path
import re
import subprocess
import pytest
from scripts import bridge_smoke as smoke

ROOT = Path(__file__).parents[1]


def function(text, name):
    return re.search(r"^" + name + r"\(\) \{.*?^}", text, re.M | re.S).group()


@pytest.mark.parametrize("target,services,worker", [
    ("hetzner", "nginx", False), ("hetzner", "platform-api", False),
    ("server2", "", False), ("server2", "platform-api", False),
    ("hetzner", "worker1", False), ("hetzner", "worker1 worker2 worker3", False),
    ("hetzner", "worker1 worker2 worker3 worker4", True), ("hetzner", "", True),
])
@pytest.mark.parametrize("smoke_failure", [False, True])
def test_deploy_service_subset(tmp_path, target, services, worker, smoke_failure):
    result, output = _deploy(tmp_path, target, services,
                             "echo SMOKE_FAIL: stub; return 1" if smoke_failure
                             else "echo SMOKE_OK: stub")
    assert "FORBIDDEN" not in output
    assert result.returncode == (1 if smoke_failure else 0 if worker else 3), output
    assert ("ROLLBACK" in output) == smoke_failure, output
    assert ("--expected-git-sha " + "a" * 40 in output) == worker, output
    # Proof per worker through the direct route whenever a SHA is expected.
    assert ("--version-workers worker1,worker2,worker3,worker4" in output) == worker, output
    assert ("--version-unproven" in output) != worker, output
    if not worker:
        assert "SMOKE_VERSION_UNPROVEN:" in output
        assert "worker SHA not proven" in output
        assert "/stub/proof &&" not in output
        if "worker" in services:
            # A pool subset keeps old images answering claude_version (CX4N2R).
            assert "keep their old image in the claude_version pool" in output
        if not smoke_failure:
            assert "finished: DEPLOYED, NOT rolled back — but UNPROVEN" in output
            assert "finished successfully" not in output


def test_version_workers_come_from_the_host_service_list(tmp_path):
    """BRUMBAU2R P2-4: the workers named for proof per worker are the host's
    configured workers, not a fixed worker1-4."""
    config = 'HETZNER_ALL="platform-api nginx worker-a worker-b erkunder"'
    result, output = _deploy(tmp_path, "hetzner", "worker-a worker-b",
                             "echo SMOKE_OK: stub", config=config)
    assert "FORBIDDEN" not in output
    assert result.returncode == 0, output
    assert re.search(r"--version-workers worker-a,worker-b$", output, re.M), output
    assert "worker1" not in output


def test_config_workers_name_each_targets_own_workers():
    text = (ROOT / "scripts/bridge-deploy.sh").read_text()
    constants = "\n".join(re.findall(
        r"^(?:HETZNER|SERVER2|WORKERHOST)_ALL=.*$", text, re.M))
    script = f"""ERKUNDER_SERVICES="erkunder-platz-1 erkunder"
{constants}
error_() {{ echo "ERROR $*"; }}
{function(text, "config_workers")}
for t in hetzner server2 prod-workers; do echo "$t=[$(config_workers $t)]"; done
config_workers nirgends || echo "rc=$?"
"""
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         timeout=10, env={"PATH": os.defpath}).stdout
    assert "hetzner=[worker1 worker2 worker3 worker4]" in out
    assert "server2=[]" in out
    assert "prod-workers=[worker-sahori worker-kurt worker-coach worker-erk]" in out
    assert "ERROR config_workers: unknown deploy target 'nirgends'" in out
    assert "rc=1" in out


def test_smoke_without_proof_per_worker_is_unproven_not_a_pass(tmp_path):
    """BRUMBAU2R P2-4: the smoke's own 'no proof per worker' state makes the
    run UNPROVEN (exit 3), never 'finished successfully'."""
    result, output = _deploy(
        tmp_path, "hetzner", "worker1 worker2 worker3 worker4",
        "echo 'SMOKE_VERSION_PER_WORKER_UNPROVEN: no proof per worker'")
    assert "FORBIDDEN" not in output
    assert result.returncode == 3, output
    assert "version UNPROVEN — no proof per worker" in output
    assert "finished successfully" not in output
    assert "/stub/proof &&" not in output


def _deploy(tmp_path, target, services, smoke_body, config=""):
    text = (ROOT / "scripts/bridge-deploy.sh").read_text()
    names = ["deploy_server", "phase_smoke_test", "record_deploy_proof",
             "config_workers"]
    definitions = "\n".join(function(text, name) for name in names)
    # Load service lists from the real CLI, never its host config or entry point.
    constants = "\n".join(re.findall(
        r"^(?:HETZNER|SERVER2|WORKERHOST)_(?:ALL|NEEDS_BUILD)=.*$", text, re.M))
    stubs = "\n".join(name + "() { :; }" for name in [
        "phase_tooling_freshness_gate", "phase_prod_order_gate", "phase_preflight",
        "phase_code_update", "phase_validate", "phase_reconcile_worker_key",
        "phase_migration_gate", "prepare_erkunder_deploy", "measure_pool_before_deploy",
        "deploy_one_service", "phase_distribution_test", "phase_access_canary",
        "check_pool_router_state", "phase_worker_config_selftest", "write_release_manifest",
        "source",
    ])
    final = text[text.rindex('if (( ${#SMOKE_UNPROVEN[@]} > 0 )); then'):]
    script = f"""set -eo pipefail
ERKUNDER_SERVICES=erkunder
{constants}
{config}
{definitions}
{stubs}
info() {{ echo "$*"; }}
warn() {{ echo "WARN $*"; }}
error_() {{ echo "ERROR $*"; }}
step() {{ echo "$*"; }}
platform_api_job_gate_needed() {{ return 1; }}
container_for_svc() {{ echo stub-container; }}
deployed_bridge_id() {{ echo stub; }}
phase_rollback() {{ echo ROLLBACK; }}
ssh() {{ echo FORBIDDEN; exit 97; }}
curl() {{ echo FORBIDDEN; exit 97; }}
docker() {{ echo FORBIDDEN; exit 97; }}
rssh() {{
    [[ "$1" == stub.invalid ]] || {{ echo FORBIDDEN >&2; exit 97; }}
    echo "RSSH $*" >&2
    echo {'a' * 40}
}}
python3() {{
    echo "PROBE_ARGS $*"
    {smoke_body}
}}
HETZNER_HOST=stub.invalid
SERVER2_HOST=stub.invalid
REMOTE_REPO=/stub
HETZNER_SVC_nginx=stub
SERVER2_SVC_nginx=stub
DRY_RUN=false
DEPLOYED_SHA_FILE=/stub/proof
SMOKE_UNPROVEN_FILE=/stub/unproven
SMOKE_UNPROVEN_PROBES=
SMOKE_UNPROVEN=()
SERVICES_ARG=({services})
deploy_server {target} || exit $?
{final}
"""
    # Any real IP in the executable harness aborts before bash can run.
    assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", script)
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                            timeout=10, env={"PATH": os.defpath, "HOME": str(tmp_path)})
    output = result.stdout + result.stderr
    return result, output


def test_unproven_skips_only_version(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(smoke, "resolve_api_key", lambda: "stub")
    for probe in smoke.PROBES:
        def run(ctx, probe=probe):
            called.append(probe.name)
            assert probe.name != "version", "version must not request a remote route"
            return smoke.ProbeResult(probe.name, probe.endpoint, True, "stub")
        monkeypatch.setattr(probe, "fn", run)
    import sys
    monkeypatch.setattr(sys, "argv", ["smoke", "--base-url", "http://stub.invalid",
                                   "--version-unproven", "no workers built"])
    with pytest.raises(SystemExit) as result:
        smoke.main()
    assert result.value.code == 0
    assert set(called) == {p.name for p in smoke.PROBES if "hetzner" in p.profiles and p.name != "version"}
    assert "SMOKE_VERSION_UNPROVEN: worker SHA not proven: no workers built" in capsys.readouterr().out
