#!/usr/bin/env bash
# BR8: a platform-api is not recreated while jobs depend on it, including jobs
# on the PEER bridge's workers whose budget home is this bridge (BR7, 10.10.2026
# 04:09:19Z: dev deploy recreated the dev platform-api, and a dev-origin research
# job on a prod worker died for good).
set -euo pipefail
cd "$(dirname "$0")/../.."
commands=$(mktemp)
functions=$(mktemp)
trap 'rm -f "$commands" "$functions"' EXIT
info() { :; }
warn() { echo "WARN $*" >> "$commands"; }
error_() { echo "ERROR $*" >> "$commands"; }
sleep() { echo "sleep $*" >> "$commands"; }
source scripts/platform-api-job-gate.sh
HETZNER_HOST=hz SERVER2_HOST=s2 WORKERHOST_HOST=wh
HETZNER_SVC_worker1=w1 HETZNER_SVC_worker2=w2 HETZNER_SVC_worker3=w3 HETZNER_SVC_worker4=w4
WORKERHOST_SVC_worker_sahori=sa WORKERHOST_SVC_worker_kurt=ku
WORKERHOST_SVC_worker_coach=co WORKERHOST_SVC_worker_erk=er
DRY_RUN=false

# --- the probe runs inside a RUNNING worker of the bridge being deployed ---
running=""
rssh() {
    echo "rssh $*" >> "$commands"
    if [[ "$*" == *'.State.Running'* ]]; then
        local c
        for c in $running; do [[ "$*" == *"'$c'"* ]] && { echo true; return 0; }; done
        echo false; return 0
    fi
    if [[ "$*" == *'docker exec -i'* ]]; then
        cat > /dev/null   # the probe script travels over stdin
        return "${probe_rc:-0}"
    fi
}
running="w3" probe_rc=0
platform_api_job_gate_probe $(platform_api_job_gate_probe_site HETZNER)
grep -q "rssh hz docker exec -i 'w3' python3 -" "$commands"
echo 'PASS dev: probe runs in the first running dev worker'
: > "$commands"
running="ku" probe_rc=1
rc=0; platform_api_job_gate_probe $(platform_api_job_gate_probe_site SERVER2) || rc=$?
[[ $rc == 1 ]]
grep -q "rssh wh docker exec -i 'ku' python3 -" "$commands"
echo 'PASS prod: probe runs on the worker host (ADR-0009), not on server2'
running=""
rc=0; platform_api_job_gate_probe $(platform_api_job_gate_probe_site HETZNER) > /dev/null || rc=$?
[[ $rc == 2 ]]
echo 'PASS no running worker: not provable'

# --- decision over probe results ---
results=$(mktemp)
trap 'rm -f "$commands" "$functions" "$results"' EXIT
# Runs in a $(...) subshell inside the gate, so the queue lives in a file.
platform_api_job_gate_probe() {
    local next
    next=$(head -n1 "$results")
    sed -i 1d "$results"
    echo "probe -> $next"
    return "$next"
}
expect() {  # expect <want-rc> <probe results...>
    local want="$1"; shift
    printf '%s\n' "$@" > "$results"
    local rc=0
    platform_api_job_gate HETZNER || rc=$?
    [[ $rc == "$want" ]] || { echo "FAIL want $want got $rc for $*"; exit 1; }
}
: > "$commands"
PLATFORM_API_JOBGATE_WAIT_S=60
expect 0 0
echo 'PASS idle: recreate'
expect 0 1 1 0
[[ $(grep -c '^sleep' "$commands") == 2 ]]
echo 'PASS busy, then idle: waits, then recreates'
PLATFORM_API_JOBGATE_WAIT_S=0
expect 3 1
grep -q 'refusing recreation (platform-api untouched)' "$commands"
echo 'PASS still busy at the deadline: refused'
expect 3 2
echo 'PASS not provable (unreachable peer, timeout, 401): refused'
expect 3 3
grep -q 'PLATFORM_API_JOBGATE_EINFUEHRUNG=1' "$commands"
echo 'PASS endpoint missing (image before BR8): refused, names the switch'
PLATFORM_API_JOBGATE_EINFUEHRUNG=1
expect 0 3
grep -q 'WARN .*WITHOUT seeing those jobs' "$commands"
expect 3 2
echo 'PASS introduction switch accepts ONLY a 404, never an error'
unset PLATFORM_API_JOBGATE_EINFUEHRUNG
PLATFORM_API_JOBGATE_WAIT_S=abc
expect 3 0
PLATFORM_API_JOBGATE_WAIT_S=60
rc=0; platform_api_job_gate SOMEWHERE || rc=$?
[[ $rc == 3 ]]
echo 'PASS bad wait value and unknown prefix are refused'
DRY_RUN=true
expect 0 1
DRY_RUN=false
echo 'PASS dry-run: probes once, never waits'

# --- deploy_server: a refused platform-api is NOT rolled back (no recreation) ---
python3 - "$functions" <<'PY'
import re
import sys
from pathlib import Path
source = Path('scripts/bridge-deploy.sh').read_text()
names = ['dry_rssh', 'service_needs_build', 'deploy_one_service',
         'container_for_svc', 'svc_to_varname', 'deploy_server']
Path(sys.argv[1]).write_text('\n'.join(
    re.search(r'^' + name + r'\(\) *\{(?:[^\n]*\}|.*?^\})', source, re.M | re.S)[0]
    for name in names
))
PY
source "$functions"
step() { :; }
rssh() { echo "rssh $*" >> "$commands"; }
HETZNER_COMPOSE=compose HETZNER_ALL="platform-api worker1"
HETZNER_NEEDS_BUILD="platform-api" HETZNER_DB_CONTAINER=db HETZNER_SVC_platform_api=wt-platform-api
SERVICES_ARG=() REMOTE_REPO=/repo HEALTH_TIMEOUT=1
phase_tooling_freshness_gate() { :; }
phase_prod_order_gate() { :; }
phase_preflight() { :; }
phase_code_update() { ROLLBACK_SHA=synthetic-sha; }
phase_validate() { :; }
phase_reconcile_worker_key() { :; }
phase_migration_gate() { :; }
prepare_erkunder_deploy() { :; }
phase_rollback() { echo "ROLLBACK $*" >> "$commands"; }
platform_api_job_gate() { echo "GATE $*" >> "$commands"; return 3; }
: > "$commands"
if deploy_server hetzner; then echo 'FAIL refused gate allowed deploy'; exit 1; fi
grep -q '^GATE HETZNER$' "$commands"
! grep -q 'up -d' "$commands"
! grep -q '^ROLLBACK' "$commands"
grep -q "git reset --hard 'synthetic-sha'" "$commands"
[[ ${#DEPLOYED_SERVICES[@]} == 0 ]]
echo 'PASS full deploy: refused gate recreates nothing, rolls nothing back, resets code'
