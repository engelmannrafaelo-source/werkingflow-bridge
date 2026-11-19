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
# `! grep` never trips set -e, so absence is asserted explicitly.
absent() { if grep -q "$1" "$commands"; then echo "FAIL unexpected: $1"; exit 1; fi; }
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
site_probe() { local s; s=$(platform_api_job_gate_probe_site "$1"); platform_api_job_gate_probe ${s%% *} "$2" ${s#* }; }
running="w3" probe_rc=0
site_probe HETZNER ""
grep -q "rssh hz docker exec -i 'w3' python3 - $" "$commands"
echo 'PASS dev: probe runs in the first running dev worker'
: > "$commands"
running="ku" probe_rc=1
rc=0; site_probe SERVER2 "" || rc=$?
[[ $rc == 1 ]]
grep -q "rssh wh docker exec -i 'ku' python3 -" "$commands"
echo 'PASS prod: probe runs on the worker host (ADR-0009), not on server2'
running=""
rc=0; site_probe HETZNER "" > /dev/null || rc=$?
[[ $rc == 2 ]]
echo 'PASS no running worker: not provable'
: > "$commands"
running="w1" probe_rc=4
rc=0; site_probe HETZNER "--peer-remote prod" || rc=$?
[[ $rc == 4 ]]
grep -q "rssh hz docker exec -i 'w1' python3 - --peer-remote prod$" "$commands"
echo 'PASS P1: the local probe is told which peer is asked from the peer side'

# --- P1: remote (peer-side) probe, exact host/container binding, fail-closed ---
remote_host="prod-workers-1" remote_running="ku" remote_rc=0 ssh_fail="" fremd_name=""
rssh() {
    echo "rssh $*" >> "$commands"
    [[ -n "$ssh_fail" ]] && return 255
    if [[ "$2" == hostname ]]; then echo "$remote_host"; return 0; fi
    if [[ "$*" == *'{{.Name}} {{.State.Running}}'* ]]; then
        local c
        [[ -n "$fremd_name" ]] && { echo "$fremd_name true"; return 0; }
        for c in $remote_running; do [[ "$*" == *"'$c'"* ]] && { echo "/$c true"; return 0; }; done
        echo "/x false"; return 0
    fi
    if [[ "$*" == *'docker exec -i'* ]]; then cat > /dev/null; return "$remote_rc"; fi
}
rp() { local rc=0; platform_api_job_gate_remote_probe prod dev wh sa ku co er > /dev/null || rc=$?; echo "$rc"; }
: > "$commands"
[[ $(rp) == 0 ]]
grep -q "rssh wh timeout 60 docker exec -i 'ku' python3 - --eigen 'prod' --heimat 'dev'" "$commands"
absent 'FEDERATION_TOKEN'
absent 'BRIDGE_SERVICE_TOKEN'
echo 'PASS P1 remote: asks in the first running prod worker, origin args only, no credential in argv'
if ( PLATFORM_API_JOBGATE_REMOTE_HOSTNAME=x ) 2>/dev/null; then echo 'FAIL hostname overridable'; exit 1; fi
[[ "$PLATFORM_API_JOBGATE_REMOTE_HOSTNAME" == prod-workers-1 ]]
echo 'PASS P1 remote: expected host name is fixed (readonly)'
remote_rc=1; [[ $(rp) == 1 ]]; echo 'PASS P1 remote: busy stays busy'
for bad in 2 3 4 124 125 126 127 137 255; do remote_rc=$bad; [[ $(rp) == 2 ]] || { echo "FAIL remote rc $bad"; exit 1; }; done
remote_rc=0
echo 'PASS P1 remote: every other probe rc (timeout 124, docker 125-127, kill, ssh) is an error'
remote_host="Production-Bridge"; [[ $(rp) == 2 ]]; remote_host="prod-workers-1"
echo 'PASS P1 remote: wrong host name is an error'
remote_running="wt-worker-host-kurtX"; [[ $(rp) == 2 ]]; remote_running="ku"
echo 'PASS P1 remote: only the exact container names count'
fremd_name="/wt-worker-host-kurt-alt"; [[ $(rp) == 2 ]]; fremd_name=""
echo 'PASS P1 remote: docker answering with ANOTHER container name (prefix/ID match) is an error'
remote_running=""; [[ $(rp) == 2 ]]; remote_running="ku"
echo 'PASS P1 remote: no running container is an error'
ssh_fail=1; [[ $(rp) == 2 ]]; ssh_fail=""
echo 'PASS P1 remote: ssh failure is an error'
rc=0; platform_api_job_gate_remote_probe prod dev wh "ku;rm" > /dev/null || rc=$?; [[ $rc == 2 ]]
echo 'PASS P1 remote: malformed container name is refused before ssh'

# --- A-GP-3: local deadline around every remote call ---
rc=0; mit_frist 1 command sleep 5 || rc=$?; [[ $rc == 124 ]]
rc=0; mit_frist 5 bash -c 'exit 3' || rc=$?; [[ $rc == 3 ]]
[[ $(printf 'durch\n' | mit_frist 5 cat) == durch ]]
# A function must NOT resume after the deadline (dcb97ae: subshell and children end in one kill).
weiter=$(mktemp -u); f_weiter() { command sleep 5; : > "$weiter"; }
rc=0; mit_frist 1 f_weiter || rc=$?; [[ $rc == 124 ]]; command sleep 1; [[ ! -e "$weiter" ]]
echo 'PASS A-GP-3: mit_frist ends a hanging call with 124 (no resume after the deadline), keeps rc and stdin otherwise'
haenge=""
rssh_alt=$(declare -f rssh)
rssh() { echo "rssh $*" >> "$commands"; [[ "$*" == *"$haenge"* && -n "$haenge" ]] && command sleep 30; [[ "$2" == hostname ]] && { echo prod-workers-1; return 0; }
         [[ "$*" == *'{{.Name}}'* ]] && { echo "/ku true"; return 0; }; [[ "$*" == *'docker exec -i'* ]] && { cat > /dev/null; return 0; }; }
PLATFORM_API_JOBGATE_SSH_FRIST_S=1
for h in hostname "docker inspect" "docker exec"; do
    haenge="$h"; t0=$SECONDS; [[ $(rp) == 2 ]] || { echo "FAIL hang in $h not an error"; exit 1; }; (( SECONDS - t0 < 10 )) || { echo "FAIL hang in $h not bounded"; exit 1; }
done
haenge=""; [[ $(rp) == 0 ]]
PLATFORM_API_JOBGATE_SSH_FRIST_S=90
eval "$rssh_alt"
echo 'PASS A-GP-3: a hanging hostname/inspect/exec is bounded locally and refused'

# --- P1: combined round (local probe + remote probe) ---
platform_api_job_gate_probe() { echo "PROBE $*" >> "$commands"; return "$local_rc"; }
platform_api_job_gate_remote_probe() { echo "REMOTE $*" >> "$commands"; return "$remote_rc"; }
round() { local rc=0; local_rc=$1 remote_rc=$2; platform_api_job_gate_round "$3" > /dev/null || rc=$?; echo "$rc"; }
chk() { local got; got=$(round "$1" "$2" "$3"); [[ "$got" == "$4" ]] || { echo "FAIL round $*: got $got"; exit 1; }; }
#    local remote prefix  want
chk  4 0 HETZNER 0
chk  4 1 HETZNER 1
chk  4 2 HETZNER 2
chk  1 0 HETZNER 1
chk  1 2 HETZNER 2
chk  2 0 HETZNER 2
chk  3 0 HETZNER 3
chk  3 1 HETZNER 1
chk  0 0 HETZNER 2
chk  255 0 HETZNER 2
chk  4 0 SERVER2 2
chk  0 0 SERVER2 0
chk  1 0 SERVER2 1
: > "$commands"; round 4 0 HETZNER > /dev/null
grep -q '^REMOTE prod dev wh sa ku co er$' "$commands"
grep -q '^PROBE hz --peer-remote prod w1 w2 w3 w4$' "$commands"
: > "$commands"; round 0 0 SERVER2 > /dev/null
absent '^REMOTE'
grep -q '^PROBE wh  sa ku co er$' "$commands"
echo 'PASS P1 round: release only with local 4 (peer pending) AND remote idle; errors win; server2 unchanged'

# --- decision over probe results ---
results=$(mktemp)
trap 'rm -f "$commands" "$functions" "$results"' EXIT
# Runs in a $(...) subshell inside the gate, so the queue lives in a file.
# Since P1 the gate asks platform_api_job_gate_round (local + remote combined).
platform_api_job_gate_round() {
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
# HEALTH_TIMEOUT 30, not 1 (A-R2-1): with 1 s a second rollover between deadline and first check skipped the
# health loop entirely -> 'Health timeout', no NACHHER, silent abort (1/11 under load). The rssh stub answers
# 'healthy' at once, so nothing waits; the deploy itself uses 360 s.
SERVICES_ARG=() REMOTE_REPO=/repo HEALTH_TIMEOUT=30
phase_tooling_freshness_gate() { :; }
phase_prod_order_gate() { :; }
phase_preflight() { :; }
phase_code_update() { ROLLBACK_SHA=synthetic-sha; }
phase_validate() { :; }
phase_reconcile_worker_key() { :; }
phase_reconcile_account_policy() { :; }   # Attrappe seit 573edfe (keep-host Account-Policy); Test brach vor P1 hier ab
log_sichern() { :; }   # Attrappe (Test war vor P1 veraltet)
LOG_SICHERUNG_RC=4   # wie scripts/log-sicherung.sh:38
log_sicherung_vorpruefung() { :; }   # Attrappe (Test war vor P1 veraltet)
phase_migration_gate() { :; }
prepare_erkunder_deploy() { :; }
phase_rollback() { echo "ROLLBACK $*" >> "$commands"; }
platform_api_job_gate() { echo "GATE $*" >> "$commands"; return 3; }
: > "$commands"
if deploy_server hetzner; then echo 'FAIL refused gate allowed deploy'; exit 1; fi
grep -q '^GATE HETZNER$' "$commands"
absent 'up -d'
absent '^ROLLBACK'
grep -q "git reset --hard 'synthetic-sha'" "$commands"
[[ ${#DEPLOYED_SERVICES[@]} == 0 ]]
echo 'PASS full deploy: refused gate recreates nothing, rolls nothing back, resets code'

# --- server2 (BR8R M1): postgres-prod is the platform-api's database and is
# recreated FIRST. The gate must run before it, otherwise a refusal leaves one
# DB gap and the rollback recreates postgres-prod a second time. ---
SERVER2_COMPOSE=compose2 SERVER2_ALL="postgres-prod platform-api nginx"
SERVER2_NEEDS_BUILD="platform-api nginx" SERVER2_DB_CONTAINER=db2
SERVER2_SVC_postgres_prod=bridge-postgres-prod SERVER2_SVC_platform_api=bridge-platform-api
SERVER2_SVC_nginx=bridge-nginx
: > "$commands"
if deploy_server server2; then echo 'FAIL refused gate allowed server2 deploy'; exit 1; fi
grep -q '^GATE SERVER2$' "$commands"
absent 'up -d'
absent '^ROLLBACK'
grep -q "git reset --hard 'synthetic-sha'" "$commands"
echo 'PASS server2 full deploy: refused gate recreates neither postgres-prod nor platform-api, rolls nothing back'

# Gate passes: it runs exactly once, before the first recreation.
platform_api_job_gate() { echo "GATE $*" >> "$commands"; return 0; }
rssh() {
    echo "rssh $*" >> "$commands"
    [[ "$*" == *'.State.Health.Status'* ]] && echo healthy
    return 0
}
deployed_bridge_id() { :; }
platform_api_job_gate_nachher() { echo "NACHHER $*" >> "$commands"; }
phase_smoke_test() { :; }
phase_distribution_test() { :; }
phase_access_canary() { :; }
phase_worker_config_selftest() { :; }
check_pool_router_state() { :; }
write_release_manifest() { :; }
: > "$commands"
deploy_server server2 > /dev/null 2>&1 || true
[[ $(grep -c '^GATE' "$commands") == 1 ]]
first_gate=$(grep -n '^GATE SERVER2$' "$commands" | cut -d: -f1)
first_up=$(grep -n 'up -d' "$commands" | head -n1 | cut -d: -f1)
[[ -n "$first_up" && $first_gate -lt $first_up ]]
grep 'up -d' "$commands" | head -n1 | grep -q 'postgres-prod'
echo 'PASS server2 gate passes: asked once, before postgres-prod is recreated'
[[ $(grep -c '^NACHHER SERVER2$' "$commands") == 1 ]]
nachher=$(grep -n '^NACHHER' "$commands" | cut -d: -f1)
pa_up=$(grep -n 'up -d.*platform-api' "$commands" | head -n1 | cut -d: -f1)
[[ -n "$pa_up" && $nachher -gt $pa_up ]]
echo 'PASS P1/BR7: read-only after-record exactly once, after the platform-api recreation'

# Only the database on server2 also needs the gate; nginx alone does not.
platform_api_job_gate() { echo "GATE $*" >> "$commands"; return 3; }
SERVICES_ARG=(postgres-prod)
: > "$commands"
if deploy_server server2 > /dev/null 2>&1; then echo 'FAIL db-only deploy passed a refused gate'; exit 1; fi
grep -q '^GATE SERVER2$' "$commands"
absent 'up -d'
SERVICES_ARG=(nginx)
: > "$commands"
deploy_server server2 > /dev/null 2>&1 || true
absent '^GATE'
SERVICES_ARG=()
echo 'PASS server2: postgres-prod alone is gated, nginx alone is not'

# --- A-GP-1: second gate round right before the platform-api recreation (hetzner) ---
gate_runden=$(mktemp)
HETZNER_SVC_nginx=nginx HETZNER_SVC_worker1=w1
SERVICES_ARG=(platform-api)   # Worker liefen sonst in die 900-s-ready_for_shutdown-Schleife (sleep gemockt)
measure_pool_before_deploy() { :; }
lb_nach_workerwechsel() { echo "LB $*" >> "$commands"; return 0; }
platform_api_job_gate() { echo "GATE $*" >> "$commands"; local n; n=$(head -n1 "$gate_runden"); sed -i 1d "$gate_runden"; return "${n:-3}"; }
printf '0\n3\n' > "$gate_runden"
: > "$commands"
rc=0; deploy_server hetzner > /dev/null 2>&1 || rc=$?
[[ $rc != 0 ]]
[[ $(grep -c '^GATE HETZNER$' "$commands") == 2 ]]
grep -q 'build --no-cache platform-api' "$commands"
absent 'up -d --no-deps --force-recreate platform-api'
absent '^ROLLBACK'
grep -q "git reset --hard 'synthetic-sha'" "$commands"
first_build=$(grep -n 'build --no-cache platform-api' "$commands" | head -n1 | cut -d: -f1)
second_gate=$(grep -n '^GATE HETZNER$' "$commands" | sed -n 2p | cut -d: -f1)
(( second_gate > first_build ))
echo 'PASS A-GP-1: second round after the build refuses -> no recreation, no rollback recreation, code reset'
printf '0\n0\n' > "$gate_runden"
: > "$commands"
deploy_server hetzner > /dev/null 2>&1 || true
[[ $(grep -c '^GATE HETZNER$' "$commands") == 2 ]]
second_gate=$(grep -n '^GATE HETZNER$' "$commands" | sed -n 2p | cut -d: -f1)
pa_up=$(grep -n 'up -d --no-deps --force-recreate platform-api' "$commands" | head -n1 | cut -d: -f1)
[[ -n "$pa_up" ]] && (( second_gate < pa_up ))
echo 'PASS A-GP-1: second round idle -> recreation follows directly after it'
SERVICES_ARG=()
printf '0\n' > "$gate_runden"
: > "$commands"
deploy_server server2 > /dev/null 2>&1 || true
[[ $(grep -c '^GATE SERVER2$' "$commands") == 1 ]]
echo 'PASS A-GP-1: server2 keeps one round (postgres-prod first, BR8R M1)'
rm -f "$gate_runden"

# --- A-GP-2: read-only after-record also after a rollback that recreates platform-api ---
python3 - "$functions" <<'PY2'
import re, sys
from pathlib import Path
src = Path('scripts/bridge-deploy.sh').read_text()
Path(sys.argv[1]).write_text(re.search(r'^phase_rollback\(\) *\{.*?^\}', src, re.M | re.S)[0])
PY2
source "$functions"
_phase_rollback_kern() { echo "KERN $*" >> "$commands"; return "${kern_rc:-0}"; }
platform_api_job_gate_nachher() { echo "NACHHER $*" >> "$commands"; }
JOBGATE_NACHHER_PREFIX=HETZNER DRY_RUN=false
: > "$commands"; kern_rc=0; phase_rollback hz compose sha bl platform-api worker1; grep -q '^NACHHER HETZNER$' "$commands"
: > "$commands"; kern_rc=2; rc=0; phase_rollback hz compose sha bl worker1 platform-api || rc=$?; [[ $rc == 2 ]]; grep -q '^NACHHER HETZNER$' "$commands"
: > "$commands"; kern_rc=0; phase_rollback hz compose sha bl worker1 worker2; absent '^NACHHER'
JOBGATE_NACHHER_PREFIX=""; : > "$commands"; phase_rollback hz compose sha bl platform-api; absent '^NACHHER'
echo 'PASS A-GP-2: after-record after every rollback with platform-api (also a failed one, rc kept), never otherwise'
