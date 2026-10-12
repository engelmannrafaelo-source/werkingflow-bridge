#!/usr/bin/env bash
# End-to-end test of account affinity (CACHE2) through the REAL nginx.conf.
#
# WHY THIS EXISTS
# ---------------
# Until CACHE2 the Lua pick was observability only: `proxy_pass
# http://claude_workers` balanced round-robin, whatever $target_worker said.
# Affinity therefore only works if the request really REACHES the preferred
# worker — and the cross-worker retry must survive the switch to
# `proxy_pass http://$affinity_upstream`. Neither is visible to a unit test or
# to `nginx -t`; this harness asserts both on behaviour, with four stub workers
# that each answer with their own name.
#
# USAGE   tests/nginx/test_cache_affinity_e2e.sh      (requires docker)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
D="$REPO/docker"
NET=cache-affinity-e2e
IMG=cache-affinity-e2e:local
PY=python:3.12-alpine
PORT="${CA_E2E_PORT:-18391}"   # 18080 belongs to test_pool_gate_e2e.sh
WORK="$(mktemp -d)"
FAILURES=0
NAMES="ca-nginx ca-w1 ca-w2 ca-w3 ca-w4 ca-metrics ca-misc"

cleanup() {
    # shellcheck disable=SC2086
    docker rm -f $NAMES >/dev/null 2>&1
    docker network rm $NET >/dev/null 2>&1
}
[ -n "${KEEP:-}" ] || trap 'cleanup; rm -rf "$WORK"' EXIT

pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; FAILURES=$((FAILURES + 1)); }

# --- stub worker: answers with its own name; FAIL=1 → 503 ------------------
cat > "$WORK/worker.py" <<'PYEOF'
import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
NAME = os.environ["NAME"]; FAIL = os.environ.get("FAIL") == "1"
PEER_AFFINITY = os.environ.get("PEER_AFFINITY", "")   # stands in for a peer bridge's own decision
class H(BaseHTTPRequestHandler):
    def _h(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        code = 503 if (FAIL and self.path.startswith("/v1/")) else 200
        body = json.dumps({"worker": NAME,
                           "affinity_key": self.headers.get("X-Cache-Affinity-Key")}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if PEER_AFFINITY and self.headers.get("X-Cache-Affinity-Key"):
            self.send_header("X-Cache-Affinity", PEER_AFFINITY)
        self.end_headers()
        self.wfile.write(body)
    do_GET = do_POST = _h
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8000), H).serve_forever()
PYEOF

# --- stub metrics-reader: four eligible accounts; TIGHT=<acct> → no capacity -
cat > "$WORK/metrics.py" <<'PYEOF'
import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
TIGHT = os.environ.get("TIGHT", "")
WORKERS = os.environ.get("WORKERS", "worker1,worker2,worker3,worker4").split(",")
ACC = {}
for n, w in zip(["engelmann", "office", "gmail", "werking"], WORKERS):
    ACC[n] = {"worker": w, "available": True, "cooldown_remaining_s": 0,
              "effective_cap_tokens": 400000, "current_in_flight_tokens": 0,
              "weekly_percent": 20.0, "usage_known": True}
for t in (list(ACC) if TIGHT == "all" else [TIGHT] if TIGHT else []):
    ACC[t]["effective_cap_tokens"] = 100
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ts": 0, "accounts": ACC}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8000), H).serve_forever()
PYEOF

echo "Building $IMG from docker/Dockerfile.nginx-lb ..."
docker build -q -f "$D/Dockerfile.nginx-lb" -t $IMG "$REPO" >/dev/null || { echo "FAIL: image build"; exit 1; }

PROD_WORKERS="worker-sahori,worker-kurt,worker-coach,worker-erk"

render() { # $1 = primary (default) | prod | prod-affinity
    sed "s/\${BRIDGE_ID}/test/g; s/\${BRIDGE_BACKUP_HOST}/192.0.2.10/g; s/\${BRIDGE_NET_GATEWAY}/192.0.2.1/g; s/\${METRICS_READER_TARGET}/metrics-elsewhere:8000/g" \
        "$D/nginx.conf" > "$WORK/nginx.conf"
    if [ "${1:-primary}" = prod ] || [ "${1:-primary}" = prod-affinity ]; then
        # the prod include, with its worker-host targets pointed at the stub workers;
        # prod-affinity = the PREPARED switch, generated exactly as an activation would
        local src="$D/upstreams-prod.conf"
        if [ "$1" = prod-affinity ]; then
            mkdir -p "$WORK/gen"
            PRIORITY_AFFINITY=1 UPSTREAMS_OUT_DIR="$WORK/gen" "$REPO/scripts/generate-bridge-upstreams.sh" production >/dev/null
            src="$WORK/gen/upstreams-prod.conf"
        fi
        sed 's/${BRIDGE_BACKUP_HOST}/backup-host/g; s/${BRIDGE_ID}/test/g; s/100\.93\.143\.105:800\([1-4]\)/worker\1:8000/g' \
            "$src" > "$WORK/upstreams.conf"
    else
        sed 's/${BRIDGE_BACKUP_HOST}/backup-host/g; s/${BRIDGE_ID}/test/g' "$D/upstreams-primary.conf" > "$WORK/upstreams.conf"
    fi
    cp "$D/routes-metrics-reader.conf" "$D/routes-platform-api.conf" "$D/worker-map-primary.conf" "$WORK/"
}

start_stack() { # $1 = failing worker (e.g. worker4) or ""   $2 = TIGHT account or ""
               # $3 = topology primary (default) | prod   $4 = X-Cache-Affinity the misc/peer stub answers with
    cleanup
    docker network create $NET >/dev/null
    local topo="${3:-primary}" workers="worker1,worker2,worker3,worker4"
    case "$topo" in prod|prod-affinity) workers="$PROD_WORKERS" ;; esac
    render "$topo"
    local i
    for i in 1 2 3 4; do
        local f="" name; [ "worker$i" = "${1:-}" ] && f=1
        name=$(printf '%s' "$workers" | cut -d, -f$i)
        docker run -d --name ca-w$i --network $NET --network-alias worker$i \
            -e NAME="$name" -e FAIL=$f -v "$WORK/worker.py:/w.py:ro" $PY python /w.py >/dev/null
    done
    docker run -d --name ca-misc --network $NET --network-alias backup-host \
        --network-alias platform-api --network-alias privacy-pdf \
        -e NAME=misc -e PEER_AFFINITY="${4:-}" -v "$WORK/worker.py:/w.py:ro" $PY python /w.py >/dev/null
    docker run -d --name ca-metrics --network $NET --network-alias metrics-elsewhere \
        -e TIGHT="${2:-}" -e WORKERS="$workers" -v "$WORK/metrics.py:/m.py:ro" $PY python /m.py >/dev/null
    docker run -d --name ca-nginx --network $NET -p $PORT:80 --tmpfs /var/log/nginx \
        -e BRIDGE_WORKERS="$workers" -e METRICS_READER_TARGET=metrics-elsewhere:8000 \
        -v "$WORK/nginx.conf:/usr/local/openresty/nginx/conf/nginx.conf:ro" \
        -v "$WORK/upstreams.conf:/tmp/upstreams.conf:ro" \
        -v "$WORK/worker-map-primary.conf:/tmp/worker-map.conf:ro" \
        -v "$WORK/routes-metrics-reader.conf:/etc/nginx/routes-metrics-reader.conf:ro" \
        -v "$WORK/routes-platform-api.conf:/etc/nginx/routes-platform-api.conf:ro" \
        $IMG >/dev/null
    for _ in $(seq 60); do curl -sf -o /dev/null http://127.0.0.1:$PORT/health && break; sleep 0.25; done
    sleep 3   # router refresh timer (2 s)
}

# preq <path> [key] → like req, but as a NON-hopped X-Priority: production call
# (the Energy-Prod customer path: llm_client.py / ai_bridge_client.py)
preq() {
    local path="$1" key="${2:-}" hdrs out code body
    hdrs="$WORK/h.txt"
    if [ -n "$key" ]; then
        out=$(curl -s -D "$hdrs" -w '\n%{http_code}' -XPOST "http://127.0.0.1:$PORT$path" \
              -H "X-Priority: production" -H "X-Cache-Affinity-Key: $key" -d '{"q":1}')
    else
        out=$(curl -s -D "$hdrs" -w '\n%{http_code}' -XPOST "http://127.0.0.1:$PORT$path" \
              -H "X-Priority: production" -d '{"q":1}')
    fi
    code=$(printf '%s' "$out" | tail -1)
    body=$(printf '%s' "$out" | sed '$d')
    local w ca n
    w=$(printf '%s' "$body" | python3 -c "import sys,json
try: print(json.load(sys.stdin)['worker'])
except Exception: print('n/a')")
    ca=$(grep -i '^x-cache-affinity:' "$hdrs" | tr -d '\r' | awk '{print $2}' | paste -sd+)
    echo "$code $w ${ca:-none}"
}

# req <path> [key] → prints "<http> <answering-worker> <X-Target-Worker> <X-Cache-Affinity> <X-Pool-Decision>"
req() {
    local path="$1" key="${2:-}" hdrs out code body
    hdrs="$WORK/h.txt"
    if [ -n "$key" ]; then
        out=$(curl -s -D "$hdrs" -w '\n%{http_code}' -XPOST "http://127.0.0.1:$PORT$path" \
              -H "X-Bridge-Hop: 1" -H "X-Cache-Affinity-Key: $key" -d '{"q":1}')
    else
        out=$(curl -s -D "$hdrs" -w '\n%{http_code}' -XPOST "http://127.0.0.1:$PORT$path" \
              -H "X-Bridge-Hop: 1" -d '{"q":1}')
    fi
    code=$(printf '%s' "$out" | tail -1)
    body=$(printf '%s' "$out" | sed '$d')
    local w tw ca pd
    w=$(printf '%s' "$body" | python3 -c "import sys,json
try: print(json.load(sys.stdin)['worker'])
except Exception: print('n/a')")
    tw=$(grep -i '^x-target-worker:' "$hdrs" | tr -d '\r' | awk '{print $2}')
    ca=$(grep -i '^x-cache-affinity:' "$hdrs" | tr -d '\r' | awk '{print $2}')
    pd=$(grep -i '^x-pool-decision:' "$hdrs" | tr -d '\r' | awk '{print $2}')
    echo "$code $w ${tw:-none} ${ca:-none} ${pd:-none}"
}

echo
echo "=== nginx -t with BOTH generated topologies ==="
render
sed 's/${BRIDGE_BACKUP_HOST}/192.0.2.10/g; s/${BRIDGE_ID}/test/g' "$D/upstreams-prod.conf" > "$WORK/upstreams-prod.conf"
cp "$D/worker-map-prod.conf" "$WORK/"
for topo in primary prod; do
    if docker run --rm --tmpfs /var/log/nginx --add-host worker1:192.0.2.1 --add-host worker2:192.0.2.2 \
        --add-host worker3:192.0.2.3 --add-host worker4:192.0.2.4 \
        --add-host backup-host:192.0.2.10 --add-host metrics-elsewhere:192.0.2.20 \
        --add-host platform-api:192.0.2.21 --add-host privacy-pdf:192.0.2.22 \
        -v "$WORK/nginx.conf:/usr/local/openresty/nginx/conf/nginx.conf:ro" \
        -v "$WORK/upstreams$( [ $topo = prod ] && echo -prod ).conf:/tmp/upstreams.conf:ro" \
        -v "$WORK/worker-map-$( [ $topo = prod ] && echo prod || echo primary ).conf:/tmp/worker-map.conf:ro" \
        -v "$WORK/routes-metrics-reader.conf:/etc/nginx/routes-metrics-reader.conf:ro" \
        -v "$WORK/routes-platform-api.conf:/etc/nginx/routes-platform-api.conf:ro" \
        --entrypoint openresty $IMG -t >"$WORK/t.txt" 2>&1; then
        pass "nginx -t ($topo upstreams incl. affinity groups)"
    else
        fail "nginx -t ($topo): $(tail -3 "$WORK/t.txt")"
    fi
done

echo
echo "=== Scenario: healthy pool, all workers up ==="
start_stack "" ""

# 1. Same key → same worker, every time, and it is the worker the router picked.
first=$(req /v1/chat/completions job-affinity-1)
set -- $first
pref_worker=$3
ok=1
for _ in $(seq 12); do
    set -- $(req /v1/chat/completions job-affinity-1)
    if [ "$1" != 200 ] || [ "$2" != "$pref_worker" ] || [ "$3" != "$pref_worker" ] \
       || [ "$4" != hit ] || [ "$5" != affinity ]; then ok=0; echo "    got: $*"; fi
done
[ $ok = 1 ] && pass "chat: 12x same key → always $pref_worker (answered by it, header hit)" \
            || fail "chat: same key did not stay on one worker"

# 2. Different keys reach different workers (it is affinity, not a pinned pool).
seen=""
for i in $(seq 40); do set -- $(req /v1/chat/completions "lauf-$i"); seen="$seen $2"; done
distinct=$(printf '%s\n' $seen | sort -u | wc -l)
[ "$distinct" -ge 3 ] && pass "chat: 40 keys reach $distinct distinct workers" \
                      || fail "chat: 40 keys reached only $distinct worker(s)"

# 3. No key → unchanged round-robin across the pool, no affinity header.
seen=""; hdr_ok=1
for _ in $(seq 8); do set -- $(req /v1/chat/completions); seen="$seen $2"; [ "$4" = none ] || hdr_ok=0; done
distinct=$(printf '%s\n' $seen | sort -u | wc -l)
[ "$distinct" -eq 4 ] && [ $hdr_ok = 1 ] && pass "chat without key: round-robin over all 4, no X-Cache-Affinity" \
    || fail "chat without key: $distinct distinct workers (want 4), header_absent=$hdr_ok"

# 4. /v1/jobs honours affinity too (the accepting worker runs the job).
ok=1
for _ in $(seq 6); do set -- $(req /v1/jobs job-affinity-1); [ "$2" = "$pref_worker" ] && [ "$4" = hit ] || ok=0; done
[ $ok = 1 ] && pass "jobs: same key → $pref_worker" || fail "jobs: affinity not applied"

# 5. Router state counts hits.
st=$(docker exec ca-nginx curl -s http://127.0.0.1/internal/pool-router/state)
STATE="$st" python3 -c "import json,os,sys; d=json.loads(os.environ['STATE']); sys.exit(0 if d['affinity']['hit']>=18 else 1)" \
    && pass "state endpoint counts affinity hits" || fail "state endpoint: ${st:0:200}"

echo
echo "=== Scenario: preferred worker fails (503) → cross-worker retry still works ==="
# Find a key that prefers worker4 (the router tells us via X-Target-Worker), then
# make worker4 fail and send the same key again.
start_stack "" ""
key4=""
for i in $(seq 200); do set -- $(req /v1/chat/completions "probe-$i"); [ "$3" = worker4 ] && { key4="probe-$i"; break; }; done
[ -n "$key4" ] || fail "no key preferring worker4 found"
start_stack worker4 ""
ok=1
for _ in $(seq 6); do
    set -- $(req /v1/chat/completions "$key4")
    if [ "$1" != 200 ] || [ "$2" = worker4 ] || [ "$2" = misc ] || [ "$3" != worker4 ]; then ok=0; echo "    got: $*"; fi
done
[ $ok = 1 ] && pass "chat: preferred worker4 returns 503 → retried on another local worker, HTTP 200" \
            || fail "chat: retry after preferred-worker failure broken"

echo
echo "=== Scenario: preferred account has no capacity → old routing, said out loud ==="
start_stack "" werking     # werking = worker4
set -- $(req /v1/chat/completions "$key4")
if [ "$1" = 200 ] && [ "$4" = "fallback:no_capacity" ] && [ "$5" = "weighted_capacity+affinity_fallback" ]; then
    pass "chat: header fallback:no_capacity, decision weighted_capacity+affinity_fallback"
else
    fail "chat: fallback not reported: $*"
fi
if docker exec ca-nginx cat /var/log/nginx/error.log | grep -q 'AFFINITY LOST.*fallback:no_capacity'; then
    pass "fallback logged (WARN 'AFFINITY LOST … fallback:no_capacity')"
else
    fail "no 'AFFINITY LOST' line in the nginx error log"
fi
seen=""
for _ in $(seq 8); do set -- $(req /v1/chat/completions "$key4"); seen="$seen $2"; done
distinct=$(printf '%s\n' $seen | sort -u | wc -l)
[ "$distinct" -ge 3 ] && pass "fallback requests are spread like before ($distinct workers)" \
                      || fail "fallback requests not spread: $seen"

echo
echo "=== Scenario: production topology, X-Priority: production (Energy-Prod path) ==="
# @production_streaming / @production_jobs run no account pick. A key there must
# be answered with bypass:priority_pool and a WARN line — never look like a hit.
start_stack "" "" prod
ok=1
for path in /v1/chat/completions /v1/jobs; do
    set -- $(preq $path job-affinity-1)
    if [ "$1" != 200 ] || [ "$3" != "bypass:priority_pool" ]; then ok=0; echo "    $path got: $*"; fi
done
[ $ok = 1 ] && pass "priority pool (prod): chat + jobs with key → 200, X-Cache-Affinity: bypass:priority_pool" \
            || fail "priority pool (prod): bypass not reported"
n=$(docker exec ca-nginx cat /var/log/nginx/error.log | grep -c 'AFFINITY BYPASSED.*bypass:priority_pool')
[ "$n" -ge 2 ] && pass "priority bypass logged ($n x WARN 'AFFINITY BYPASSED … bypass:priority_pool')" \
               || fail "priority bypass: $n WARN line(s), want >= 2"
seen=""; hdr_ok=1
for _ in $(seq 8); do set -- $(preq /v1/chat/completions); seen="$seen $2"; [ "$3" = none ] || hdr_ok=0; done
distinct=$(printf '%s\n' $seen | sort -u | wc -l)
[ "$distinct" -eq 4 ] && [ $hdr_ok = 1 ] && pass "priority pool (prod) without key: round-robin over 4 prod workers, no header" \
    || fail "priority pool (prod) without key: $distinct workers (want 4), header_absent=$hdr_ok"
st=$(docker exec ca-nginx curl -s http://127.0.0.1/internal/pool-router/state)
STATE="$st" python3 -c "import json,os,sys; d=json.loads(os.environ['STATE']); a=d['affinity']; sys.exit(0 if a['bypass']>=2 and a['last_bypass']=='bypass:priority_pool' else 1)" \
    && pass "state endpoint counts priority bypasses" || fail "state endpoint: ${st:0:200}"
# the hopped path on the same LB still has the full affinity (that is where the
# dev bridge sends Energy-Staging)
set -- $(req /v1/chat/completions job-affinity-1)
[ "$1" = 200 ] && [ "$4" = hit ] && pass "prod LB, hopped: affinity hit as before ($3)" \
    || fail "prod LB, hopped: $*"

echo
echo "=== Prepared (B1b): committed prod include keeps the priority pool in mode bypass ==="
grep -q 'default bypass;' "$D/upstreams-prod.conf" && ! grep -q 'default affinity;' "$D/upstreams-prod.conf" \
    && pass "docker/upstreams-prod.conf: \$priority_pool_affinity = bypass (affinity NOT active)" \
    || fail "docker/upstreams-prod.conf does not say bypass"
# the affinity groups the prepared mode routes to must hold exactly the servers of
# claude_production on production (same targets, dev bridge only as backup)
python3 - "$D/upstreams-prod.conf" <<'PYEOF' && pass "prod: claude_workers_affin_* hold exactly the servers of claude_production" || fail "prod: affinity groups differ from claude_production"
import re, sys
txt = open(sys.argv[1]).read()
def servers(name):
    body = re.search(r"upstream %s \{(.*?)\}" % re.escape(name), txt, re.S).group(1)
    return sorted(re.findall(r"server (\S+)", body))
prod = servers("claude_production")
groups = re.findall(r"upstream (claude_workers_affin_\S+) \{", txt)
sys.exit(0 if groups and all(servers(g) == prod for g in groups) else 1)
PYEOF

echo
echo "=== Scenario (B1b, switched on for the test): affinity on the priority pool ==="
start_stack "" "" prod-affinity
first=$(preq /v1/chat/completions job-affinity-1); set -- $first; pw=$2
ok=1
for path in /v1/chat/completions /v1/chat/completions /v1/chat/completions /v1/jobs /v1/jobs; do
    set -- $(preq $path job-affinity-1)
    if [ "$1" != 200 ] || [ "$2" != "$pw" ] || [ "$3" != hit ]; then ok=0; echo "    $path got: $*"; fi
done
[ $ok = 1 ] && [ "$pw" != misc ] && pass "priority pool + affinity: same key → always $pw (chat + jobs), header hit" \
            || fail "priority pool + affinity: key did not stay on one worker"
seen=""; hdr_ok=1
for _ in $(seq 8); do set -- $(preq /v1/chat/completions); seen="$seen $2"; [ "$3" = none ] || hdr_ok=0; done
distinct=$(printf '%s\n' $seen | sort -u | wc -l)
[ "$distinct" -eq 4 ] && [ $hdr_ok = 1 ] && pass "priority pool + affinity, no key: round-robin over 4 as before" \
    || fail "priority pool + affinity, no key: $distinct workers, header_absent=$hdr_ok"
# find which stub index the preferred worker is, then make it fail
idx=$(printf '%s' "$PROD_WORKERS" | tr , '\n' | grep -n "^$pw$" | cut -d: -f1)
start_stack "worker$idx" "" prod-affinity
ok=1
for _ in $(seq 4); do
    set -- $(preq /v1/chat/completions job-affinity-1)
    if [ "$1" != 200 ] || [ "$2" = "$pw" ] || [ "$2" = misc ] || [ "$3" != hit ]; then ok=0; echo "    got: $*"; fi
done
[ $ok = 1 ] && pass "priority pool + affinity: preferred $pw 503 → retried on another prod worker, 200" \
            || fail "priority pool + affinity: retry broken"
start_stack "" all prod-affinity
ok=1
for _ in $(seq 4); do
    set -- $(preq /v1/chat/completions job-affinity-1)
    if [ "$1" != 200 ] || [ "$3" != "fallback:no_capacity" ]; then ok=0; echo "    got: $*"; fi
done
[ $ok = 1 ] && pass "priority pool + affinity, whole pool without capacity: still 200 (no gate), fallback:no_capacity" \
            || fail "priority pool + affinity must not start rejecting"
code=$(curl -s -o /dev/null -w '%{http_code}' -XPOST "http://127.0.0.1:$PORT/v1/chat/completions" -H "X-Bridge-Hop: 1" -d '{"q":1}')
[ "$code" = 429 ] && pass "contrast: hopped path in the same state is gated (429), unchanged" \
                  || fail "contrast: hopped path gave $code, want 429"

echo
echo "=== Scenario: primary topology, X-Priority pool forwards to the peer bridge ==="
start_stack "" "" primary hit
set -- $(preq /v1/chat/completions job-affinity-1)
[ "$1" = 200 ] && [ "$2" = misc ] && [ "$3" = hit ] \
    && pass "peer states its decision → passed through unchanged (one header, 'hit')" \
    || fail "peer decision not passed through cleanly: $*"
start_stack "" "" primary ""
set -- $(preq /v1/chat/completions job-affinity-1)
[ "$1" = 200 ] && [ "$3" = "bypass:peer_without_affinity" ] \
    && pass "answer without the peer's decision → bypass:peer_without_affinity" \
    || fail "missing peer decision not reported: $*"
docker exec ca-nginx cat /var/log/nginx/error.log | grep -q 'AFFINITY BYPASSED.*bypass:peer_without_affinity' \
    && pass "missing peer decision logged at WARN" || fail "no WARN for missing peer decision"
set -- $(preq /v1/chat/completions)
[ "$3" = none ] && pass "peer path without key: no header" || fail "peer path without key: $*"

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "CACHE_AFFINITY_E2E_OK: all assertions passed"
    exit 0
fi
echo "CACHE_AFFINITY_E2E_FAIL: $FAILURES assertion(s) failed"
exit 1
