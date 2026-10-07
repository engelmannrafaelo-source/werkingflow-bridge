#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
source scripts/erkunder-deploy.sh
info() { :; }
error_() { echo "$*" >&2; }
REMOTE_REPO=/synthetic/repo
DRY_RUN=false
SERVICES_ARG=()
commands=$(mktemp)
trap 'rm -f "$commands"' EXIT
dry_rssh() { echo "$*" >> "$commands"; return "${build_rc:-0}"; }
erkunder_unchanged() { return "${changed:-0}"; }
services_to_deploy=(worker1 $ERKUNDER_SERVICES)
prepare_erkunder_deploy fake compose
[[ "${services_to_deploy[*]}" == worker1 && ! -s "$commands" ]]
echo 'PASS unchanged: no build, recreation or health wait'
erkunder_wait_idle() { return "${gate_rc:-0}"; }
changed=1
services_to_deploy=(worker1 $ERKUNDER_SERVICES)
prepare_erkunder_deploy fake compose
[[ "${services_to_deploy[*]}" == "$ERKUNDER_SERVICES worker1" ]]
[[ $(wc -l < "$commands") == 1 ]]
grep -q 'GIT_COMMIT=.*git rev-parse HEAD.*build erkunder' "$commands"
echo 'PASS changed: one labelled build, places before Leitstand'
SERVICES_ARG=(erkunder-platz-2)
services_to_deploy=(erkunder-platz-2)
changed=0
prepare_erkunder_deploy fake compose
[[ "${services_to_deploy[*]}" == "$ERKUNDER_SERVICES" ]]
echo 'PASS explicit place expands to whole release'
gate_rc=1
if prepare_erkunder_deploy fake compose; then
    echo 'FAIL busy report gate swallowed'; exit 1
fi
! grep -Eq ' stop | up |restart' "$commands"
echo 'PASS report gate failure: FAIL before any container mutation'
gate_rc=0
build_rc=1
if prepare_erkunder_deploy fake compose; then
    echo 'FAIL build failure swallowed'; exit 1
fi
echo 'PASS build failure fails loud'
# Exercise the actual provenance decision against synthetic docker/git output.
source scripts/erkunder-deploy.sh
rssh_run() { bash -euo pipefail -s; }
REMOTE_REPO=$PWD
docker() {
    [[ ${missing:-false} == false ]] || return 1
    echo "true ${health:-healthy} ${label:-$(git rev-parse HEAD)}"
}
export -f docker
health=healthy missing=false label=$(git rev-parse HEAD)
export health missing label
erkunder_unchanged fake
echo 'PASS actual image label + unchanged git trees'
health=unhealthy
if erkunder_unchanged fake; then exit 1; fi
health=healthy label=unknown
if erkunder_unchanged fake; then exit 1; fi
label=$(git rev-parse HEAD) missing=true
if erkunder_unchanged fake; then exit 1; fi
echo 'PASS unhealthy, unknown provenance and missing service cannot skip'
# Load only production functions; never execute the CLI's lock/SSH entry point.
functions=$(mktemp)
trap 'rm -f "$commands" "$functions"' EXIT
python3 - "$functions" <<'PY'
import re
import sys
from pathlib import Path
source = Path('scripts/bridge-deploy.sh').read_text()
names = ['log', 'info', 'warn', 'error_', 'step', 'dry_rssh',
         'service_needs_build', 'deploy_one_service', 'container_for_svc',
         'svc_to_varname', 'phase_rollback', 'deploy_server']
Path(sys.argv[1]).write_text('\n'.join(
    re.search(r'^' + name + r'\(\) *\{(?:[^\n]*\}|.*?^\})', source, re.M | re.S)[0]
    for name in names
))
PY
source "$functions"
rssh() {
    echo "$*" >> "$commands"
    if [[ "$*" == *'.State.Running'* ]]; then
        echo true
    elif [[ "$*" == *'docker inspect'* ]]; then echo "${state:-healthy}"; fi
    [[ "$*" != *'build erkunder'* || ${build_rc:-0} == 0 ]]
}
erkunder_wait_idle() { return "${gate_rc:-0}"; }
DRY_RUN=true HEALTH_TIMEOUT=1 ROLLBACK_HEALTH_TIMEOUT=1
build_rc=0
for svc in $ERKUNDER_SERVICES; do
    deploy_one_service fake compose "$svc" "docker-$svc-1" ''
done
echo 'PASS dry-run: admission stop, five SHA-tagged recreations and health checks planned'
DRY_RUN=false state=unhealthy
if deploy_one_service fake compose erkunder docker-erkunder-1 ''; then
    echo 'FAIL unhealthy accepted'; exit 1
fi
echo 'PASS actual deploy health failure fails loud'
# Verify rollback from a version lacking Docker healthchecks uses the probe.
state=none
HETZNER_HOST=fake
for svc in $ERKUNDER_SERVICES; do
    printf -v "HETZNER_SVC_${svc//-/_}" '%s' "docker-$svc-1"
done
erkunder_legacy_health() { echo healthy; }
phase_rollback fake compose synthetic-sha '' $ERKUNDER_SERVICES
echo 'PASS rollback: shared image rebuilt, SHA used on up, legacy health probed'
[[ $(grep -c 'build erkunder' "$commands") == 5 ]] # three plans + failed build + one rollback
[[ $(grep -c 'GIT_COMMIT=.*git rev-parse HEAD.*up -d' "$commands") == 6 ]]
echo 'PASS command assertions: labelled recreate and exactly one rollback image build'

# Full orchestration must abort before ANY service, including unrelated workers.
HETZNER_COMPOSE=compose HETZNER_ALL="worker1 $ERKUNDER_SERVICES"
HETZNER_NEEDS_BUILD='' HETZNER_DB_CONTAINER=synthetic-db
SERVICES_ARG=()
phase_tooling_freshness_gate() { :; }
phase_prod_order_gate() { :; }
phase_preflight() { :; }
phase_code_update() { ROLLBACK_SHA=synthetic-sha; }
phase_validate() { :; }
phase_reconcile_worker_key() { :; }
phase_migration_gate() { :; }
erkunder_unchanged() { return 1; }
erkunder_wait_idle() { return 1; }
: > "$commands"
if deploy_server hetzner; then echo 'FAIL busy report allowed deploy'; exit 1; fi
! grep -Eq ' stop | up |restart' "$commands"
[[ ${#DEPLOYED_SERVICES[@]} == 0 ]]
echo 'PASS full deploy: report gate failure resets code, stops nothing, no rollback/SUCCESS'
# A live report on the new release is protected during rollback as well.
: > "$commands"
if phase_rollback fake compose synthetic-sha '' $ERKUNDER_SERVICES; then
    echo 'FAIL busy report allowed rollback'; exit 1
fi
! grep -Eq 'reset --hard| stop | up |restart' "$commands"
echo 'PASS rollback: busy report stops rollback before reset or container stop'
