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
changed=1
services_to_deploy=(worker1 $ERKUNDER_SERVICES)
prepare_erkunder_deploy fake compose
[[ "${services_to_deploy[*]}" == "worker1 $ERKUNDER_SERVICES" ]]
[[ $(wc -l < "$commands") == 1 ]]
grep -q 'GIT_COMMIT=.*git rev-parse HEAD.*build erkunder' "$commands"
echo 'PASS changed: one labelled build, places before Leitstand'
SERVICES_ARG=(erkunder-platz-2)
services_to_deploy=(erkunder-platz-2)
changed=0
prepare_erkunder_deploy fake compose
[[ "${services_to_deploy[*]}" == "$ERKUNDER_SERVICES" ]]
echo 'PASS explicit place expands to whole release'
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
         'svc_to_varname', 'phase_rollback']
Path(sys.argv[1]).write_text('\n'.join(
    re.search(r'^' + name + r'\(\) *\{(?:[^\n]*\}|.*?^\})', source, re.M | re.S)[0]
    for name in names
))
PY
source "$functions"
rssh() {
    echo "$*" >> "$commands"
    if [[ "$*" == *'docker inspect'* ]]; then echo "${state:-healthy}"; fi
    [[ "$*" != *'build erkunder'* || ${build_rc:-0} == 0 ]]
}
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
[[ $(grep -c 'build erkunder' "$commands") == 4 ]] # two plans + failed build + one rollback
[[ $(grep -c 'GIT_COMMIT=.*git rev-parse HEAD.*up -d' "$commands") == 6 ]]
echo 'PASS command assertions: labelled recreate and exactly one rollback image build'
