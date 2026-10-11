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
class H(BaseHTTPRequestHandler):
    def _h(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        code = 503 if (FAIL and self.path.startswith("/v1/")) else 200
        body = json.dumps({"worker": NAME,
                           "affinity_key": self.headers.get("X-Cache-Affinity-Key")}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
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
ACC = {}
for n, w in [("engelmann", "worker1"), ("office", "worker2"), ("gmail", "worker3"), ("werking", "worker4")]:
    ACC[n] = {"worker": w, "available": True, "cooldown_remaining_s": 0,
              "effective_cap_tokens": 400000, "current_in_flight_tokens": 0,
              "weekly_percent": 20.0, "usage_known": True}
if TIGHT:
    ACC[TIGHT]["effective_cap_tokens"] = 100
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

render() {
    sed "s/\${BRIDGE_ID}/test/g; s/\${BRIDGE_BACKUP_HOST}/192.0.2.10/g; s/\${METRICS_READER_TARGET}/metrics-elsewhere:8000/g" \
        "$D/nginx.conf" > "$WORK/nginx.conf"
    sed 's/${BRIDGE_BACKUP_HOST}/backup-host/g; s/${BRIDGE_ID}/test/g' "$D/upstreams-primary.conf" > "$WORK/upstreams.conf"
    cp "$D/routes-metrics-reader.conf" "$D/routes-platform-api.conf" "$D/worker-map-primary.conf" "$WORK/"
}

start_stack() { # $1 = failing worker (e.g. worker4) or ""   $2 = TIGHT account or ""
    cleanup
    docker network create $NET >/dev/null
    render
    local i
    for i in 1 2 3 4; do
        local f=""; [ "worker$i" = "${1:-}" ] && f=1
        docker run -d --name ca-w$i --network $NET --network-alias worker$i \
            -e NAME=worker$i -e FAIL=$f -v "$WORK/worker.py:/w.py:ro" $PY python /w.py >/dev/null
    done
    docker run -d --name ca-misc --network $NET --network-alias backup-host \
        --network-alias platform-api --network-alias privacy-pdf \
        -e NAME=misc -v "$WORK/worker.py:/w.py:ro" $PY python /w.py >/dev/null
    docker run -d --name ca-metrics --network $NET --network-alias metrics-elsewhere \
        -e TIGHT="${2:-}" -v "$WORK/metrics.py:/m.py:ro" $PY python /m.py >/dev/null
    docker run -d --name ca-nginx --network $NET -p $PORT:80 --tmpfs /var/log/nginx \
        -e BRIDGE_WORKERS="worker1,worker2,worker3,worker4" -e METRICS_READER_TARGET=metrics-elsewhere:8000 \
        -v "$WORK/nginx.conf:/usr/local/openresty/nginx/conf/nginx.conf:ro" \
        -v "$WORK/upstreams.conf:/tmp/upstreams.conf:ro" \
        -v "$WORK/worker-map-primary.conf:/tmp/worker-map.conf:ro" \
        -v "$WORK/routes-metrics-reader.conf:/etc/nginx/routes-metrics-reader.conf:ro" \
        -v "$WORK/routes-platform-api.conf:/etc/nginx/routes-platform-api.conf:ro" \
        $IMG >/dev/null
    for _ in $(seq 60); do curl -sf -o /dev/null http://127.0.0.1:$PORT/health && break; sleep 0.25; done
    sleep 3   # router refresh timer (2 s)
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
if [ "$FAILURES" -eq 0 ]; then
    echo "CACHE_AFFINITY_E2E_OK: all assertions passed"
    exit 0
fi
echo "CACHE_AFFINITY_E2E_FAIL: $FAILURES assertion(s) failed"
exit 1
