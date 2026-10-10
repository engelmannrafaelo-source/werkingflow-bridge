#!/usr/bin/env bash
# ============================================================================
# test_smoke_pool_empty.sh — the dev smoke separates "code broken" from
# "pool empty" (BR6S, 2026-10-10)
# ============================================================================
# With the whole dev pool in cooldown, both pool-gated probes (research, chat)
# are refused by the router, the smoke reports `no pool-gated probe passed`
# and the deploy rolled a healthy build back. Now an empty pool — measured in
# the router's own state, not guessed from the answer — leaves the deploy
# standing as UNPROVEN, provided health, served_by stamp and routing to the
# deployed bridge are green. Everything else stays a FAIL.
#
# Runs only extracted functions against stubs: no SSH, no network, no LLM.
# Usage: tests/deploy/test_smoke_pool_empty.sh     (exit 0 = all cases hold)
# ============================================================================
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# Load only the functions under test; never execute the CLI entry point.
python3 - "$work/functions.sh" <<'PY'
import re
import sys
from pathlib import Path
source = Path('scripts/bridge-deploy.sh').read_text()
names = ['log', 'info', 'warn', 'error_', 'step', 'phase_smoke_test',
         'fetch_pool_router_state', 'pool_router_exhaustion', 'probe_unpooled_stamp',
         'smoke_pool_unproven', 'check_pool_router_state',
         'phase_distribution_test', 'distribution_assertion']
lines = source.split('\n')

def extract(name):
    # Up to the first column-0 `}` OUTSIDE a heredoc: the embedded Python
    # (dict literals) has column-0 braces of its own.
    start = next(i for i, l in enumerate(lines) if re.match(r'^' + name + r'\(\) *\{', l))
    out, term = [], None
    for l in lines[start:]:
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

Path(sys.argv[1]).write_text('\n'.join(extract(n) for n in names))
PY
# phase_smoke_test runs bridge_smoke.py from its own directory — the fake lives there.
cat > "$work/bridge_smoke.py" <<'PY'
import os, sys
sys.stdout.write(os.environ.get("FAKE_SMOKE_OUT", ""))
sys.exit(int(os.environ.get("FAKE_SMOKE_RC", "0")))
PY
source "$work/functions.sh"

source() { [[ "$1" == /root/.infisical/infisical-api.sh ]] || builtin source "$@"; }
rssh() {
    [[ "$*" == *'/internal/pool-router/state'* ]] || { echo "unexpected rssh: $*" >&2; return 99; }
    printf '%s' "$FAKE_STATE"
    return "${FAKE_STATE_RC:-0}"
}
curl() {
    [[ "$*" == *'/health'* ]] || { echo "unexpected curl: $*" >&2; return 99; }
    printf '%s' "$FAKE_HEALTH"
    return "${FAKE_CURL_RC:-0}"
}
DRY_RUN=false

# --- canned answers ---------------------------------------------------------
acct() {  # name available cooldown cap inflight
    printf '"%s":{"available":%s,"cooldown_remaining_s":%s,"effective_cap_tokens":%s,"current_in_flight_tokens":%s,"usage_known":true,"weekly_percent":40,"worker":"worker1"}' "$@"
}
state() {  # accounts-json [status] [age]
    printf '{"ts":1,"state_age_s":%s,"last_refresh_status":"%s","last_refresh_err":"","metrics_url":"http://metrics-reader:8000","consecutive_failures":0,"decision_counter_per_worker":{},"last_state_snapshot":{"accounts":{%s}}}' \
        "${3:-2.0}" "${2:-ok}" "$1"
}
POOL_EMPTY=$(state "$(acct gmail true 3600 400000 0),$(acct werking true 7200 400000 0),$(acct coach false 0 400000 0),$(acct erk true 0 300 0)")
POOL_FREE=$(state "$(acct gmail true 3600 400000 0),$(acct coach true 0 400000 0)")
POOL_BLIND=$(state "$(acct gmail true 3600 400000 0)" err 2.0)
HEALTH_OK=$'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nX-Bridge-Served-By: dev/worker2\r\n\r\n'
HEALTH_503=$'HTTP/1.1 503 Service Unavailable\r\nX-Bridge-Served-By: dev/worker2\r\n\r\n'
HEALTH_NOSTAMP=$'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n'
HEALTH_FOREIGN=$'HTTP/1.1 200 OK\r\nX-Bridge-Served-By: prod/worker-kurt\r\n\r\n'
SMOKE_GREEN=$'  [OK  ] research ...\n  [OK  ] chat_completions ...\nSMOKE_OK: 9/9 probes passed (profile=hetzner, answered by bridge \'dev\')\n'
SMOKE_REFUSED=$'  [FAIL] research /v1/research pool capacity unavailable (pool_exhausted) [no pool-gated probe passed — cannot rule out the deployed image]\n  [FAIL] chat_completions /v1/chat/completions pool capacity unavailable (pool_exhausted) [no pool-gated probe passed — cannot rule out the deployed image]\nSMOKE_FAIL: 2/9 probes failed: research(/v1/research), chat_completions(/v1/chat/completions)\nSMOKE_POOL_REFUSED_ONLY: research(pool_exhausted), chat_completions(pool_exhausted)\n'
SMOKE_REFUSED_PLUS_REAL=$'  [FAIL] research ...\n  [FAIL] document_convert /v1/document/convert HTTP 415\nSMOKE_FAIL: 3/9 probes failed: research(/v1/research), chat_completions(/v1/chat/completions), document_convert(/v1/document/convert)\n'

pass=0
fail=0
case_() {  # name expected(PASS|UNPROVEN|FAIL) smoke-out smoke-rc state health [state-rc] [curl-rc]
    local name="$1" want="$2" got rc=0 log
    SMOKE_UNPROVEN=()
    log=$(FAKE_SMOKE_OUT="$3" FAKE_SMOKE_RC="$4" FAKE_STATE="$5" FAKE_HEALTH="$6" \
          FAKE_STATE_RC="${7:-0}" FAKE_CURL_RC="${8:-0}" \
          phase_smoke_test hetzner http://bridge.invalid:8000 dev required \
              fake-host wt-wrapper-lb "X-Bridge-Hop: 1" 2>&1) || rc=$?
    # the count must come from THIS shell: phase_smoke_test ran in a subshell above
    if [[ $rc -ne 0 ]]; then got=FAIL
    elif grep -q 'SMOKE_POOL_UNPROVEN:' <<< "$log"; then got=UNPROVEN
    else got=PASS; fi
    if [[ "$got" == "$want" ]]; then
        printf '  ok    %-52s -> %s\n' "$name" "$got"
        pass=$((pass + 1))
    else
        printf '  FAIL  %-52s expected %s, got %s\n%s\n' "$name" "$want" "$got" "$log"
        fail=$((fail + 1))
    fi
    LAST_LOG="$log"
}

echo "phase_smoke_test (hetzner/dev):"
case_ "pool free + smoke green"                    PASS     "$SMOKE_GREEN" 0 "$POOL_FREE" "$HEALTH_OK"
case_ "pool free + pool probes refused"            FAIL     "$SMOKE_REFUSED" 1 "$POOL_FREE" "$HEALTH_OK"
grep -q 'POOL_AVAILABLE: 1/2 account(s) eligible: coach' <<< "$LAST_LOG"
case_ "pool empty + health/stamp/routing green"    UNPROVEN "$SMOKE_REFUSED" 1 "$POOL_EMPTY" "$HEALTH_OK"
grep -q 'POOL_EXHAUSTED: 0/4 accounts eligible' <<< "$LAST_LOG"
grep -q 'served_by=dev/worker2' <<< "$LAST_LOG"
case_ "pool empty + health 503"                    FAIL     "$SMOKE_REFUSED" 1 "$POOL_EMPTY" "$HEALTH_503"
case_ "pool empty + health unreachable"            FAIL     "$SMOKE_REFUSED" 1 "$POOL_EMPTY" "" 0 7
case_ "pool empty + stamp missing"                 FAIL     "$SMOKE_REFUSED" 1 "$POOL_EMPTY" "$HEALTH_NOSTAMP"
grep -q 'without X-Bridge-Served-By' <<< "$LAST_LOG"
case_ "pool empty + stamp from other bridge"       FAIL     "$SMOKE_REFUSED" 1 "$POOL_EMPTY" "$HEALTH_FOREIGN"
case_ "pool empty + a non-pool probe also failed"  FAIL     "$SMOKE_REFUSED_PLUS_REAL" 1 "$POOL_EMPTY" "$HEALTH_OK"
case_ "router blind (refresh err) = not proven"    FAIL     "$SMOKE_REFUSED" 1 "$POOL_BLIND" "$HEALTH_OK"
case_ "router state stale (age 45s)"               FAIL     "$SMOKE_REFUSED" 1 "$(state "$(acct a true 60 1 0)" ok 45)" "$HEALTH_OK"
case_ "router state unreachable"                   FAIL     "$SMOKE_REFUSED" 1 "" "$HEALTH_OK" 1
case_ "router lists no accounts (round-robin)"     FAIL     "$SMOKE_REFUSED" 1 "$(state "")" "$HEALTH_OK"

# Without a pool LB (server2) the exception does not exist at all.
SMOKE_UNPROVEN=()
if FAKE_SMOKE_OUT="$SMOKE_REFUSED" FAKE_SMOKE_RC=1 FAKE_STATE="$POOL_EMPTY" FAKE_HEALTH="$HEALTH_OK" \
        phase_smoke_test server2 http://s2.invalid:8000 prod optional - - "X-Priority: production" >/dev/null 2>&1; then
    echo "  FAIL  server2 (no pool LB) must not excuse refusals"; fail=$((fail + 1))
else
    echo "  ok    server2 (no pool LB): refusals stay FAIL"; pass=$((pass + 1))
fi

# The UNPROVEN line must reach the caller's exit contract (SMOKE_UNPROVEN -> exit 3).
SMOKE_UNPROVEN=()
FAKE_SMOKE_OUT="$SMOKE_REFUSED" FAKE_SMOKE_RC=1 FAKE_STATE="$POOL_EMPTY" FAKE_HEALTH="$HEALTH_OK" \
    phase_smoke_test hetzner http://bridge.invalid:8000 dev required fake-host lb "X-Bridge-Hop: 1" >/dev/null 2>&1
if [[ ${#SMOKE_UNPROVEN[@]} == 1 && "${SMOKE_UNPROVEN[0]}" == "hetzner: pool-gated probes UNPROVEN — research(pool_exhausted), chat_completions(pool_exhausted); 0/4"* ]]; then
    echo "  ok    UNPROVEN recorded for the exit-3 contract"; pass=$((pass + 1))
else
    echo "  FAIL  SMOKE_UNPROVEN=${SMOKE_UNPROVEN[*]:-<empty>}"; fail=$((fail + 1))
fi
grep -q 'if (( ${#SMOKE_UNPROVEN\[@\]} > 0 )); then' scripts/bridge-deploy.sh \
    && grep -q 'exit 3' scripts/bridge-deploy.sh \
    && { echo "  ok    entry point exits 3 on UNPROVEN"; pass=$((pass + 1)); } \
    || { echo "  FAIL  entry point lacks the exit-3 branch"; fail=$((fail + 1)); }

echo "phase_distribution_test (BR6R SOLLTE b):"
# A red distribution must not skip the router-state check any more.
state_checked=0
distribution_assertion() { return 1; }
check_pool_router_state() { state_checked=1; return 0; }
if phase_distribution_test fake-host http://bridge.invalid:8000 lb dev >/dev/null 2>&1; then
    echo "  FAIL  red distribution reported green"; fail=$((fail + 1))
elif [[ $state_checked == 1 ]]; then
    echo "  ok    red distribution still runs the router-state check"; pass=$((pass + 1))
else
    echo "  FAIL  router-state check skipped after red distribution"; fail=$((fail + 1))
fi
distribution_assertion() { return 0; }
check_pool_router_state() { return 1; }
if phase_distribution_test fake-host http://bridge.invalid:8000 lb dev >/dev/null 2>&1; then
    echo "  FAIL  blind router hidden behind green distribution"; fail=$((fail + 1))
else
    echo "  ok    green distribution + blind router = red"; pass=$((pass + 1))
fi

echo
echo "${pass} passed, ${fail} failed"
[[ $fail == 0 ]]
