#!/usr/bin/env bash
# ============================================================================
# test_smoke_pool_empty.sh — the dev smoke separates "code broken" from
# "pool empty" (BR6S + BR6Sb, 2026-10-10)
# ============================================================================
# With the whole dev pool in cooldown, both pool-gated probes (research, chat)
# are refused by the router, the smoke reports `no pool-gated probe passed`
# and the deploy rolled a healthy build back. An empty pool now leaves the
# deploy standing as UNPROVEN — but only if it was measurably empty BEFORE the
# worker swap (the old image's view) and still is, with a complete router view,
# and every deployed worker answers health green with the right stamp
# (BR6SR MUSS 1). UNPROVEN is no proof: it never advances .bridge-deployed-sha,
# the prod gate refuses it, `both` stops before server2, and
# `smoke-nachholen` catches the probes up later (BR6SR MUSS 2).
#
# Runs only extracted functions against stubs: no SSH, no network, no LLM.
# The "host" is a local directory; rssh runs its command there.
# Usage: tests/deploy/test_smoke_pool_empty.sh     (exit 0 = all cases hold)
# ============================================================================
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
SCRIPT="${BRIDGE_DEPLOY_SCRIPT:-scripts/bridge-deploy.sh}"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# Load only the functions under test; never execute the CLI entry point.
# A function missing from $SCRIPT is reported, not fatal: the cases that need
# it then fail on their own (used to run this matrix against older commits).
mkdir -p "$work/repo/scripts"
python3 - "$SCRIPT" "$work/repo/scripts/functions.sh" "$work/both_block.sh" <<'PY'
import re
import sys
from pathlib import Path
source = Path(sys.argv[1]).read_text()
names = ['log', 'info', 'warn', 'error_', 'step', 'phase_smoke_test',
         'fetch_pool_router_state', 'pool_router_exhaustion', 'probe_unpooled_stamp',
         'measure_pool_before_deploy', 'smoke_pool_unproven', 'check_pool_router_state',
         'phase_distribution_test', 'distribution_assertion', 'phase_prod_order_gate',
         'record_deploy_proof', 'write_smoke_unproven_marker', 'catch_up_unproven_smoke']
lines = source.split('\n')

def extract(name):
    # Up to the first column-0 `}` OUTSIDE a heredoc: the embedded Python
    # (dict literals) has column-0 braces of its own.
    starts = [i for i, l in enumerate(lines) if re.match(r'^' + name + r'\(\) *\{', l)]
    if not starts:
        print(f'MISSING function in {sys.argv[1]}: {name}', file=sys.stderr)
        return ''
    out, term = [], None
    for l in lines[starts[0]:]:
        out.append(l)
        if term:
            if l.strip() == term:
                term = None
            continue
        m = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", l)
        if m and len(out) > 1:
            term = m.group(1)
        elif l.startswith('}') and (len(out) > 1 or l.rstrip().endswith('}')):
            return '\n'.join(out)
    raise SystemExit(f'no end for {name}')

Path(sys.argv[2]).write_text('\n'.join(extract(n) for n in names))
# The entry point's `both)` branch, to run it against a stubbed deploy_server.
case_start = max(i for i, l in enumerate(lines) if l.startswith('case "$SERVER" in'))
i = next(i for i in range(case_start, len(lines)) if lines[i].strip() == 'both)')
j = next(j for j in range(i, len(lines)) if lines[j].strip() == ';;')
Path(sys.argv[3]).write_text('\n'.join(lines[i + 1:j]))
PY
# phase_smoke_test / catch_up run bridge_smoke.py from their own directory — the fake lives there.
cat > "$work/repo/scripts/bridge_smoke.py" <<'PY'
import os, sys
with open(os.environ["SMOKE_ARGV_LOG"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
sys.stdout.write(os.environ.get("FAKE_SMOKE_OUT", ""))
sys.exit(int(os.environ.get("FAKE_SMOKE_RC", "0")))
PY
export SMOKE_ARGV_LOG="$work/smoke_argv.log"
source "$work/repo/scripts/functions.sh"

# --- the fake dev host: a git checkout plus the two marker files -------------
HOST_DIR="$work/host"
git init -q "$HOST_DIR"
git -C "$HOST_DIR" -c user.name=t -c user.email=t@t commit -q --allow-empty -m one
OLD_SHA=$(git -C "$HOST_DIR" rev-parse HEAD)
git -C "$HOST_DIR" -c user.name=t -c user.email=t@t commit -q --allow-empty -m two
NEW_SHA=$(git -C "$HOST_DIR" rev-parse HEAD)
REMOTE_REPO="$HOST_DIR"
DEPLOYED_SHA_FILE="$HOST_DIR/.bridge-deployed-sha"
SMOKE_UNPROVEN_FILE="$HOST_DIR/.bridge-smoke-unproven"
HETZNER_HOST=fake-hetzner
HETZNER_SVC_nginx=wt-wrapper-lb

source() { [[ "$1" == /root/.infisical/infisical-api.sh ]] || builtin source "$@"; }
rssh() {
    if [[ "$2" == *'/internal/pool-router/state'* ]]; then
        printf '%s' "$FAKE_STATE"
        return "${FAKE_STATE_RC:-0}"
    fi
    bash -c "$2"
}
curl() {
    [[ "$*" == *'/health'* ]] || { echo "unexpected curl: $*" >&2; return 99; }
    local n
    n=$(cat "$work/curl_n" 2>/dev/null || echo 0)
    echo $((n + 1)) > "$work/curl_n"
    # FAKE_HEALTH: responses separated by "@@", served round-robin like claude_workers
    awk -v RS='@@' -v n="$n" '{r[NR-1]=$0} END {printf "%s", r[n % NR]}' <<< "$FAKE_HEALTH"
    return "${FAKE_CURL_RC:-0}"
}
DRY_RUN=false

# --- canned answers ---------------------------------------------------------
acct() {  # name available cooldown cap inflight
    printf '"%s":{"available":%s,"cooldown_remaining_s":%s,"effective_cap_tokens":%s,"current_in_flight_tokens":%s,"usage_known":true,"weekly_percent":40,"worker":"worker1"}' "$@"
}
state() {  # accounts-json [status] [age] [errors-json]
    local errors="${4:-}"
    [[ -n "$errors" ]] || errors='{}'
    printf '{"ts":1,"state_age_s":%s,"last_refresh_status":"%s","last_refresh_err":"","metrics_url":"http://metrics-reader:8000","consecutive_failures":0,"decision_counter_per_worker":{},"last_state_snapshot":{"accounts":{%s},"errors":%s}}' \
        "${3:-2.0}" "${2:-ok}" "$1" "$errors"
}
POOL_EMPTY=$(state "$(acct gmail true 3600 400000 0),$(acct werking true 7200 400000 0),$(acct coach false 0 400000 0),$(acct erk true 0 300 0)")
POOL_FREE=$(state "$(acct gmail true 3600 400000 0),$(acct werking true 0 400000 0),$(acct coach true 0 400000 0),$(acct erk true 0 400000 0)")
POOL_BLIND=$(state "$(acct gmail true 3600 400000 0)" err 2.0)
# BR6SR case A: the new image reports every account available:false (field lost/wrong)
POOL_ALL_UNAVAILABLE=$(state "$(acct gmail false 0 400000 0),$(acct werking false 0 400000 0),$(acct coach false 0 400000 0),$(acct erk false 0 400000 0)")
# BR6SR case B: the new image's limiter reports a cap of 0 everywhere
POOL_CAP_ZERO=$(state "$(acct gmail true 0 0 0),$(acct werking true 0 0 0),$(acct coach true 0 0 0),$(acct erk true 0 0 0)")
# BR6SR case E: 3 of 4 workers missing from the snapshot, the 4th in cooldown
POOL_PARTIAL=$(state "$(acct gmail true 3600 400000 0)" ok 2.0 '{"worker2":"timeout","worker3":"connection refused","worker4":"HTTP 500"}')
# Same accounts minus two, no errors recorded: the set itself changed
POOL_FEWER=$(state "$(acct gmail true 3600 400000 0),$(acct werking true 7200 400000 0)")
# BR6SR case C: 15 s old — the Lua router (STALE_THRESHOLD_S=10) round-robins
POOL_EMPTY_STALE15=$(state "$(acct gmail true 3600 400000 0),$(acct werking true 7200 400000 0),$(acct coach false 0 400000 0),$(acct erk true 0 300 0)" ok 15)
POOL_AGE_NULL=$(state "$(acct gmail true 3600 400000 0)" ok null)

h() {  # worker [code] -> one /health answer from dev/<worker>
    printf 'HTTP/1.1 %s\r\nContent-Type: application/json\r\nX-Bridge-Served-By: dev/%s\r\n\r\n' "${2:-200 OK}" "$1"
}
HEALTH_ALL="$(h worker1)@@$(h worker2)@@$(h worker3)@@$(h worker4)"
HEALTH_ONLY_W1="$(h worker1)"
HEALTH_W3_503="$(h worker1)@@$(h worker2)@@$(h worker3 '503 Service Unavailable')@@$(h worker4)"
HEALTH_503=$(h worker2 '503 Service Unavailable')
HEALTH_NOSTAMP=$'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n'
HEALTH_FOREIGN=$'HTTP/1.1 200 OK\r\nX-Bridge-Served-By: prod/worker-kurt\r\n\r\n'
SMOKE_GREEN=$'  [OK  ] research ...\n  [OK  ] chat_completions ...\nSMOKE_OK: 9/9 probes passed (profile=hetzner, answered by bridge \'dev\')\n'
SMOKE_REFUSED=$'  [FAIL] research /v1/research pool capacity unavailable (all_pool_exhausted) [no pool-gated probe passed — cannot rule out the deployed image]\n  [FAIL] chat_completions /v1/chat/completions pool capacity unavailable (all_pool_exhausted) [no pool-gated probe passed — cannot rule out the deployed image]\nSMOKE_FAIL: 2/9 probes failed: research(/v1/research), chat_completions(/v1/chat/completions)\nSMOKE_POOL_REFUSED_ONLY: research(all_pool_exhausted), chat_completions(all_pool_exhausted)\n'
SMOKE_REFUSED_PLUS_REAL=$'  [FAIL] research ...\n  [FAIL] document_convert /v1/document/convert HTTP 415\nSMOKE_FAIL: 3/9 probes failed: research(/v1/research), chat_completions(/v1/chat/completions), document_convert(/v1/document/convert)\n'

pass=0
fail=0
ok_()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad_() { printf '  FAIL  %s\n' "$1"; [[ -n "${2:-}" ]] && printf '%s\n' "$2"; fail=$((fail + 1)); }

DEPLOYED_SERVICES=(platform-api metrics-reader nginx worker1 worker2 worker3 worker4)
case_() {  # name expected(PASS|UNPROVEN|FAIL) smoke-out smoke-rc pre-state post-state health [state-rc] [curl-rc]
    local name="$1" want="$2" got rc=0 log pre_log=""
    SMOKE_UNPROVEN=()
    SMOKE_UNPROVEN_PROBES=""
    echo 0 > "$work/curl_n"
    POOL_PRE_DEPLOY_STATE="UNKNOWN"; POOL_PRE_DEPLOY_LINE="not measured"; POOL_PRE_DEPLOY_JSON=""
    if declare -F measure_pool_before_deploy >/dev/null; then
        if [[ "$5" == UNREADABLE ]]; then
            pre_log=$(FAKE_STATE="" FAKE_STATE_RC=1 measure_pool_before_deploy fake-host wt-wrapper-lb 2>&1)
            FAKE_STATE="" FAKE_STATE_RC=1 measure_pool_before_deploy fake-host wt-wrapper-lb >/dev/null 2>&1
        else
            FAKE_STATE="$5" measure_pool_before_deploy fake-host wt-wrapper-lb >/dev/null 2>&1
        fi
    fi
    log=$(FAKE_SMOKE_OUT="$3" FAKE_SMOKE_RC="$4" FAKE_STATE="$6" FAKE_HEALTH="$7" \
          FAKE_STATE_RC="${8:-0}" FAKE_CURL_RC="${9:-0}" \
          phase_smoke_test hetzner http://bridge.invalid:8000 dev required \
              fake-host wt-wrapper-lb "X-Bridge-Hop: 1" 2>&1) || rc=$?
    if [[ $rc -ne 0 ]]; then got=FAIL
    elif grep -q 'SMOKE_POOL_UNPROVEN:' <<< "$log"; then got=UNPROVEN
    else got=PASS; fi
    if [[ "$got" == "$want" ]]; then
        printf '  ok    %-60s -> %s\n' "$name" "$got"
        pass=$((pass + 1))
    else
        printf '  FAIL  %-60s expected %s, got %s\n%s\n' "$name" "$want" "$got" "$pre_log$log"
        fail=$((fail + 1))
    fi
    LAST_LOG="$log"
}
expect_log() {  # pattern description
    if grep -qF -- "$1" <<< "$LAST_LOG"; then ok_ "  ... $2"; else bad_ "  ... $2 (no '$1' in log)" "$LAST_LOG"; fi
}

echo "phase_smoke_test (hetzner/dev) — BR6S cases, pool measured before AND after the deploy:"
case_ "pool free + smoke green"                         PASS     "$SMOKE_GREEN" 0 "$POOL_FREE" "$POOL_FREE" "$HEALTH_ALL"
case_ "pool free + pool probes refused"                 FAIL     "$SMOKE_REFUSED" 1 "$POOL_FREE" "$POOL_FREE" "$HEALTH_ALL"
case_ "pool empty before+after, all 4 workers green"    UNPROVEN "$SMOKE_REFUSED" 1 "$POOL_EMPTY" "$POOL_EMPTY" "$HEALTH_ALL"
expect_log 'POOL_EXHAUSTED: 0/4 accounts eligible' "post-deploy verdict logged"
expect_log 'dev/{worker1,worker2,worker3,worker4}' "every deployed worker seen with the dev stamp"
expect_log 'smoke-nachholen' "points at the catch-up command"
case_ "pool empty + health 503"                         FAIL     "$SMOKE_REFUSED" 1 "$POOL_EMPTY" "$POOL_EMPTY" "$HEALTH_503"
case_ "pool empty + health unreachable"                 FAIL     "$SMOKE_REFUSED" 1 "$POOL_EMPTY" "$POOL_EMPTY" "" 0 7
case_ "pool empty + stamp missing"                      FAIL     "$SMOKE_REFUSED" 1 "$POOL_EMPTY" "$POOL_EMPTY" "$HEALTH_NOSTAMP"
case_ "pool empty + stamp from other bridge"            FAIL     "$SMOKE_REFUSED" 1 "$POOL_EMPTY" "$POOL_EMPTY" "$HEALTH_FOREIGN"
case_ "pool empty + a non-pool probe also failed"       FAIL     "$SMOKE_REFUSED_PLUS_REAL" 1 "$POOL_EMPTY" "$POOL_EMPTY" "$HEALTH_ALL"
case_ "router blind (refresh err) = not proven"         FAIL     "$SMOKE_REFUSED" 1 "$POOL_EMPTY" "$POOL_BLIND" "$HEALTH_ALL"
case_ "router state stale (age 45s)"                    FAIL     "$SMOKE_REFUSED" 1 "$POOL_EMPTY" "$(state "$(acct a true 60 1 0)" ok 45)" "$HEALTH_ALL"
case_ "router state unreachable"                        FAIL     "$SMOKE_REFUSED" 1 "$POOL_EMPTY" "" "$HEALTH_ALL" 1
case_ "router lists no accounts (round-robin)"          FAIL     "$SMOKE_REFUSED" 1 "$POOL_EMPTY" "$(state "")" "$HEALTH_ALL"

echo "MUSS 1 — the post-deploy reading comes from the new image and cannot excuse it alone:"
case_ "A: pool free before, new image says all unavailable" FAIL "$SMOKE_REFUSED" 1 "$POOL_FREE" "$POOL_ALL_UNAVAILABLE" "$HEALTH_ALL"
expect_log 'NOT proven empty before the deploy' "reason names the pre-deploy reading"
case_ "B: pool free before, new limiter reports cap 0"  FAIL     "$SMOKE_REFUSED" 1 "$POOL_FREE" "$POOL_CAP_ZERO" "$HEALTH_ALL"
case_ "E: empty before, 3/4 workers missing (errors)"   FAIL     "$SMOKE_REFUSED" 1 "$POOL_EMPTY" "$POOL_PARTIAL" "$HEALTH_ALL"
expect_log 'snapshot is incomplete' "reason names the missing workers"
case_ "E: free before, 3/4 workers missing (errors)"    FAIL     "$SMOKE_REFUSED" 1 "$POOL_FREE" "$POOL_PARTIAL" "$HEALTH_ALL"
case_ "empty before, accounts vanished after (no errors)" FAIL   "$SMOKE_REFUSED" 1 "$POOL_EMPTY" "$POOL_FEWER" "$HEALTH_ALL"
expect_log 'account set changed' "reason names the changed account set"
case_ "pre-deploy state unreadable, empty after"        FAIL     "$SMOKE_REFUSED" 1 UNREADABLE "$POOL_EMPTY" "$HEALTH_ALL"
case_ "pool blind before, empty after"                  FAIL     "$SMOKE_REFUSED" 1 "$POOL_BLIND" "$POOL_EMPTY" "$HEALTH_ALL"

echo "SOLL — freshness like the Lua router, every deployed worker, no traceback:"
case_ "C: state 15s old (Lua round-robins past 10s)"     FAIL     "$SMOKE_REFUSED" 1 "$POOL_EMPTY" "$POOL_EMPTY_STALE15" "$HEALTH_ALL"
case_ "D: state_age_s null"                             FAIL     "$SMOKE_REFUSED" 1 "$POOL_EMPTY" "$POOL_AGE_NULL" "$HEALTH_ALL"
expect_log 'no usable state_age_s' "named, not a traceback"
case_ "only worker1 ever answers health (4 deployed)"   FAIL     "$SMOKE_REFUSED" 1 "$POOL_EMPTY" "$POOL_EMPTY" "$HEALTH_ONLY_W1"
expect_log 'worker2 worker3 worker4 never answered' "names the unseen workers"
case_ "worker3 answers health 503"                      FAIL     "$SMOKE_REFUSED" 1 "$POOL_EMPTY" "$POOL_EMPTY" "$HEALTH_W3_503"

# Without a pool LB (server2) the exception does not exist at all.
SMOKE_UNPROVEN=()
if FAKE_SMOKE_OUT="$SMOKE_REFUSED" FAKE_SMOKE_RC=1 FAKE_STATE="$POOL_EMPTY" FAKE_HEALTH="$HEALTH_ALL" \
        phase_smoke_test server2 http://s2.invalid:8000 prod optional - - "X-Priority: production" >/dev/null 2>&1; then
    bad_ "server2 (no pool LB) must not excuse refusals"
else
    ok_ "server2 (no pool LB): refusals stay FAIL"
fi

# The UNPROVEN line must reach the caller's exit contract (SMOKE_UNPROVEN -> exit 3).
SMOKE_UNPROVEN=(); SMOKE_UNPROVEN_PROBES=""; echo 0 > "$work/curl_n"
FAKE_STATE="$POOL_EMPTY" measure_pool_before_deploy fake-host lb >/dev/null 2>&1 || true
FAKE_SMOKE_OUT="$SMOKE_REFUSED" FAKE_SMOKE_RC=1 FAKE_STATE="$POOL_EMPTY" FAKE_HEALTH="$HEALTH_ALL" \
    phase_smoke_test hetzner http://bridge.invalid:8000 dev required fake-host lb "X-Bridge-Hop: 1" >/dev/null 2>&1 || true
if [[ ${#SMOKE_UNPROVEN[@]} == 1 && "${SMOKE_UNPROVEN[0]}" == "hetzner: pool-gated probes UNPROVEN — research(all_pool_exhausted), chat_completions(all_pool_exhausted); 0/4"* \
      && "$SMOKE_UNPROVEN_PROBES" == "research,chat_completions" ]]; then
    ok_ "UNPROVEN recorded for the exit-3 contract (probes: ${SMOKE_UNPROVEN_PROBES})"
else
    bad_ "SMOKE_UNPROVEN=${SMOKE_UNPROVEN[*]:-<empty>} probes=${SMOKE_UNPROVEN_PROBES:-<empty>}"
fi

echo "MUSS 2 — UNPROVEN is no proof:"
# Phase 7 after an UNPROVEN smoke: the proof file stays at the last proven build.
printf '%s\n' "$OLD_SHA" > "$DEPLOYED_SHA_FILE"; rm -f "$SMOKE_UNPROVEN_FILE"
record_deploy_proof fake-hetzner hetzner "$NEW_SHA" >/dev/null 2>&1 || true
if [[ "$(cat "$DEPLOYED_SHA_FILE")" == "$OLD_SHA" ]]; then
    ok_ "UNPROVEN deploy does NOT advance .bridge-deployed-sha"
else
    bad_ "UNPROVEN deploy wrote .bridge-deployed-sha=$(cat "$DEPLOYED_SHA_FILE")"
fi
if grep -qx "sha=${NEW_SHA}" "$SMOKE_UNPROVEN_FILE" 2>/dev/null && grep -qx 'probes=research,chat_completions' "$SMOKE_UNPROVEN_FILE" \
        && grep -q '^grund=pool-gated probes UNPROVEN' "$SMOKE_UNPROVEN_FILE"; then
    ok_ ".bridge-smoke-unproven names sha, open probes and reason"
else
    bad_ ".bridge-smoke-unproven missing or incomplete" "$(cat "$SMOKE_UNPROVEN_FILE" 2>&1)"
fi
# A clean deploy afterwards: proof advances, marker gone.
SMOKE_UNPROVEN_PROBES=""
record_deploy_proof fake-hetzner hetzner "$NEW_SHA" >/dev/null 2>&1 || true
if [[ "$(cat "$DEPLOYED_SHA_FILE")" == "$NEW_SHA" && ! -e "$SMOKE_UNPROVEN_FILE" ]]; then
    ok_ "clean deploy records the proof and clears the marker"
else
    bad_ "clean deploy: deployed-sha=$(cat "$DEPLOYED_SHA_FILE"), marker present=$([[ -e $SMOKE_UNPROVEN_FILE ]] && echo yes || echo no)"
fi

# phase_prod_order_gate reads both files. Its repo = the fake host's git, with
# origin/develop = NEW_SHA (the unproven build is the prod target).
git -C "$work/repo" init -q
git -C "$work/repo" remote add origin "$HOST_DIR"
git -C "$HOST_DIR" branch -q -f develop "$NEW_SHA"
gate() {  # deployed-sha [marker-sha] -> rc, log in GATE_LOG
    printf '%s\n' "$1" > "$DEPLOYED_SHA_FILE"
    rm -f "$SMOKE_UNPROVEN_FILE"
    [[ -n "${2:-}" ]] && printf 'sha=%s\nprobes=research,chat_completions\ngrund=pool empty (test)\n' "$2" > "$SMOKE_UNPROVEN_FILE"
    local rc=0
    GATE_LOG=$(FORCE_PROD_AHEAD=false phase_prod_order_gate server2 2>&1 < /dev/null) || rc=$?
    return $rc
}
if gate "$NEW_SHA"; then ok_ "prod gate: target proven on dev -> open"; else bad_ "prod gate blocked a proven target" "$GATE_LOG"; fi
if gate "$OLD_SHA" "$NEW_SHA"; then
    bad_ "prod gate passed an UNPROVEN dev build" "$GATE_LOG"
elif grep -q 'NO STAGING PROOF' <<< "$GATE_LOG" && grep -q 'grund=pool empty (test)' <<< "$GATE_LOG"; then
    ok_ "prod gate: target only UNPROVEN on dev -> blocked, loud, with reason"
else
    bad_ "prod gate blocked, but without the UNPROVEN reason" "$GATE_LOG"
fi
# What acfdee9 wrote on UNPROVEN: the proof file names the unproven build.
if gate "$NEW_SHA" "$NEW_SHA"; then
    bad_ "prod gate trusted a deployed-sha the UNPROVEN marker names" "$GATE_LOG"
else
    ok_ "prod gate: deployed-sha = UNPROVEN build -> blocked"
fi
git -C "$HOST_DIR" branch -q -f develop "$OLD_SHA"
if gate "$OLD_SHA" "$NEW_SHA"; then
    ok_ "prod gate: older target proven on dev, newer build unproven -> open (warned)"
else
    bad_ "prod gate blocked a target the last proven build covers" "$GATE_LOG"
fi
git -C "$HOST_DIR" branch -q -f develop "$NEW_SHA"

# `both`: UNPROVEN on hetzner stops the run before server2.
both_out=$(
    SMOKE_UNPROVEN=()
    deploy_server() { echo "deploy_server $1"; [[ "$1" == hetzner ]] && SMOKE_UNPROVEN+=("hetzner: pool-gated probes UNPROVEN — test"); return 0; }
    builtin source "$work/both_block.sh"
    echo "fell through"
) 2>/dev/null && both_rc=0 || both_rc=$?
if [[ $both_rc == 3 && "$both_out" != *"deploy_server server2"* ]]; then
    ok_ "both: UNPROVEN hetzner -> exit 3, server2 not deployed"
else
    bad_ "both: rc=${both_rc}, server2 ran=$([[ "$both_out" == *"deploy_server server2"* ]] && echo yes || echo no)" "$both_out"
fi
both_out=$(
    SMOKE_UNPROVEN=()
    deploy_server() { echo "deploy_server $1"; return 0; }
    builtin source "$work/both_block.sh"
) 2>/dev/null && both_rc=0 || both_rc=$?
if [[ $both_rc == 0 && "$both_out" == *"deploy_server server2"* ]]; then
    ok_ "both: clean hetzner -> server2 runs"
else
    bad_ "both: clean hetzner did not reach server2 (rc=${both_rc})" "$both_out"
fi
grep -q 'if (( ${#SMOKE_UNPROVEN\[@\]} > 0 )); then' "$SCRIPT" && grep -q 'exit 3' "$SCRIPT" \
    && ok_ "entry point exits 3 on UNPROVEN" || bad_ "entry point lacks the exit-3 branch"

echo "smoke-nachholen (catch_up_unproven_smoke):"
phase_tooling_freshness_gate() { return 0; }
deployed_bridge_id() { echo dev; }
catch_up() {  # name want-rc smoke-out smoke-rc [marker-sha]
    local name="$1" want="$2" rc=0
    printf '%s\n' "$OLD_SHA" > "$DEPLOYED_SHA_FILE"
    rm -f "$SMOKE_UNPROVEN_FILE" "$SMOKE_ARGV_LOG"
    [[ "${5-x}" != "" ]] && printf 'sha=%s\nseit=x\nprobes=research,chat_completions\ngrund=pool empty (test)\n' "${5:-$NEW_SHA}" > "$SMOKE_UNPROVEN_FILE"
    CATCH_LOG=$(FAKE_SMOKE_OUT="$3" FAKE_SMOKE_RC="$4" catch_up_unproven_smoke 2>&1) || rc=$?
    if [[ $rc == "$want" ]]; then ok_ "$(printf '%-52s -> rc %s' "$name" "$rc")"; else bad_ "$name: rc ${rc}, expected ${want}" "$CATCH_LOG"; fi
}
catch_up "no marker -> nothing to do"                 0 "" 0 ""
catch_up "probes green -> proven"                     0 "$SMOKE_GREEN" 0
if [[ "$(cat "$DEPLOYED_SHA_FILE")" == "$NEW_SHA" && ! -e "$SMOKE_UNPROVEN_FILE" ]] \
        && grep -q -- '--only research,chat_completions' "$SMOKE_ARGV_LOG" && grep -q -- '--expect-bridge dev' "$SMOKE_ARGV_LOG"; then
    ok_ "  ... ran only the open probes; proof = ${NEW_SHA:0:9}, marker removed"
else
    bad_ "  ... green catch-up did not record the proof" "$(cat "$SMOKE_ARGV_LOG" 2>&1)"
fi
catch_up "pool still refuses -> still UNPROVEN"       3 "$SMOKE_REFUSED" 1
[[ -e "$SMOKE_UNPROVEN_FILE" && "$(cat "$DEPLOYED_SHA_FILE")" == "$OLD_SHA" ]] \
    && ok_ "  ... marker stays, no proof written" || bad_ "  ... refused catch-up changed the markers"
catch_up "only partly proven (SMOKE_CAPACITY)"        3 $'SMOKE_CAPACITY: chat_completions(all_pool_exhausted)\n' 0
catch_up "probes red on the running build"            1 "$SMOKE_REFUSED_PLUS_REAL" 1
[[ -e "$SMOKE_UNPROVEN_FILE" && "$(cat "$DEPLOYED_SHA_FILE")" == "$OLD_SHA" ]] \
    && ok_ "  ... red: marker stays, no proof written" || bad_ "  ... red catch-up changed the markers"
catch_up "marker names a build that no longer runs"   1 "$SMOKE_GREEN" 0 "$OLD_SHA"

echo "phase_distribution_test (BR6R SOLLTE b):"
# A red distribution must not skip the router-state check any more.
state_checked=0
distribution_assertion() { return 1; }
check_pool_router_state() { state_checked=1; return 0; }
if phase_distribution_test fake-host http://bridge.invalid:8000 lb dev >/dev/null 2>&1; then
    bad_ "red distribution reported green"
elif [[ $state_checked == 1 ]]; then
    ok_ "red distribution still runs the router-state check"
else
    bad_ "router-state check skipped after red distribution"
fi
distribution_assertion() { return 0; }
check_pool_router_state() { return 1; }
if phase_distribution_test fake-host http://bridge.invalid:8000 lb dev >/dev/null 2>&1; then
    bad_ "blind router hidden behind green distribution"
else
    ok_ "green distribution + blind router = red"
fi
# SOLL 3: after UNPROVEN the 5b warning must not claim the smoke passed.
if awk '/phase_distribution_test "\$host" "\$\{hetzner_url\}"/,/^        fi$/' "$SCRIPT" | grep -q 'SMOKE_UNPROVEN_PROBES' ; then
    ok_ "5b warning distinguishes UNPROVEN from 'smoke test passed'"
else
    bad_ "5b warning says 'smoke test passed' even after UNPROVEN"
fi

echo
echo "${pass} passed, ${fail} failed"
[[ $fail == 0 ]]
