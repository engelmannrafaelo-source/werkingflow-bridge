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
    if [[ "$1" == exec ]]; then return "${probe_rc:-0}"; fi
    echo "true ${health:-healthy} ${label:-$(git rev-parse HEAD)}"
}
export -f docker
health=healthy missing=false label=$(git rev-parse HEAD)
export health missing label
erkunder_unchanged fake
echo 'PASS actual image label + unchanged git trees'
export probe_rc=1
if erkunder_unchanged fake; then echo 'FAIL cached health accepted'; exit 1; fi
export probe_rc=0
echo 'PASS live place probe failure overrides cached Docker healthy'
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
         'svc_to_varname', 'phase_rollback', 'deployed_bridge_id',
         'deploy_server']
Path(sys.argv[1]).write_text('\n'.join(
    re.search(r'^' + name + r'\(\) *\{(?:[^\n]*\}|.*?^\})', source, re.M | re.S)[0]
    for name in names
))
PY
source "$functions"
# deploy_server asks platform_api_job_gate_needed before Phase 4 (BR8b). The
# real helper decides; the explicit lists here contain no platform-api.
source scripts/platform-api-job-gate.sh
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

# Execute the real introduction shell, with Docker/SSH replaced by local probes.
source scripts/erkunder-deploy.sh
REMOTE_REPO=$PWD
rssh() { echo "protocol-probe" >> "$commands"; return "${protocol_rc:-3}"; }
rssh_run() { bash -euo pipefail -s; }
docker() {
    echo "$*" >> "$commands"
    case "$1" in
        inspect) echo synthetic-old-image ;;
        exec) cat >/dev/null; return "${pre_rc:-0}" ;;
        run) cat >/dev/null; return "${post_rc:-0}" ;;
        compose)
            if [[ "$*" == *' stop erkunder' ]]; then return "${stop_rc:-0}"; fi
            if [[ "$*" == *' start erkunder' ]]; then return "${start_rc:-0}"; fi
            ;;
        *) return 99 ;;
    esac
}
export -f docker
export commands pre_rc=0 post_rc=0 stop_rc=0 start_rc=0
export ERKUNDER_DEPLOY_EINFUEHRUNG=1
DRY_RUN=false protocol_rc=3
: > "$commands"
erkunder_wait_idle fake compose
[[ $(grep -c 'stop erkunder' "$commands") == 1 ]]
grep -q -- '--network none --volumes-from docker-erkunder-1:ro --entrypoint python synthetic-old-image' "$commands"
! grep -q 'start erkunder' "$commands"
echo 'PASS legacy + idle + switch: pre-check, stop, read-only post-check'

pre_rc=1
: > "$commands"
if erkunder_wait_idle fake compose; then echo 'FAIL busy legacy'; exit 1; fi
! grep -Eq 'stop erkunder|start erkunder|^run ' "$commands"
echo 'PASS legacy busy: nothing stopped'

pre_rc=0 post_rc=1
: > "$commands"
if erkunder_wait_idle fake compose; then echo 'FAIL post-stop race'; exit 1; fi
grep -q 'stop erkunder' "$commands"
grep -q 'start erkunder' "$commands"
! grep -q ' up ' "$commands"
echo 'PASS post-stop race/probe failure: old Leitstand restarted, no rollout'

post_rc=0 stop_rc=1
: > "$commands"
if erkunder_wait_idle fake compose; then echo 'FAIL ambiguous stop'; exit 1; fi
grep -q 'start erkunder' "$commands"
! grep -q '^run ' "$commands"
echo 'PASS ambiguous stop: recovery attempted'

stop_rc=0 post_rc=1 start_rc=1
: > "$commands"
if erkunder_wait_idle fake compose; then echo 'FAIL recovery failure'; exit 1; else rc=$?; fi
[[ "$rc" == 2 ]]
echo 'PASS recovery failure remains CRITICAL'
post_rc=0 start_rc=0

protocol_rc=0
: > "$commands"
erkunder_wait_idle fake compose
[[ $(wc -l < "$commands") == 1 ]]
echo 'PASS new protocol + switch: normal gate, no introduction'

protocol_rc=3 ERKUNDER_DEPLOY_EINFUEHRUNG=0
: > "$commands"
if output=$(erkunder_wait_idle fake compose 2>&1); then echo 'FAIL legacy without switch'; exit 1; fi
[[ "$output" == *'ERKUNDER_DEPLOY_EINFUEHRUNG=1'* ]]
[[ $(wc -l < "$commands") == 1 ]]
echo 'PASS legacy without switch: FAIL with actionable hint'

protocol_rc=1 ERKUNDER_DEPLOY_EINFUEHRUNG=1
: > "$commands"
if erkunder_wait_idle fake compose; then echo 'FAIL transport/auth/protocol failure bypass'; exit 1; fi
[[ $(wc -l < "$commands") == 1 ]]
echo 'PASS other protocol failures never introduce'

SERVICES_ARG=(worker1 nginx)
services_to_deploy=(worker1 nginx)
: > "$commands"
prepare_erkunder_deploy fake compose
[[ "${services_to_deploy[*]}" == 'worker1 nginx' && ! -s "$commands" ]]
echo 'PASS explicit non-Erkunder list unaffected, no probe or build'

# Finish a full non-Erkunder deploy with synthetic side effects. Suppress only
# the unrelated local Infisical source; every external phase is stubbed.
source() { [[ "$1" == /root/.infisical/infisical-api.sh ]] || builtin source "$@"; }
HETZNER_ALL="worker1 nginx $ERKUNDER_SERVICES"
HETZNER_SVC_worker1=synthetic-worker HETZNER_SVC_nginx=synthetic-nginx
DEPLOYED_SHA_FILE=/synthetic/deployed-sha
phase_smoke_test() { :; }
phase_distribution_test() { :; }
phase_access_canary() { :; }
phase_worker_config_selftest() { :; }
write_release_manifest() { :; }
measure_pool_before_deploy() { :; }
record_deploy_proof() { echo "proof $2 $3" >> "$commands"; }
deploy_one_service() { echo "deploy $3" >> "$commands"; }
rssh() { echo "remote $*" >> "$commands"; echo synthetic; }
: > "$commands"
deploy_server hetzner
[[ "${DEPLOYED_SERVICES[*]}" == 'worker1 nginx' ]]
[[ $(grep -c '^deploy ' "$commands") == 2 ]]
! grep -Eq 'protocol-probe|stop erkunder|build erkunder|--legacy-idle' "$commands"
grep -q '^proof hetzner synthetic' "$commands"
echo 'PASS full Bridge deploy with explicit non-Erkunder list reaches SUCCESS'
