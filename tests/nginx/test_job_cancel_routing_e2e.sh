#!/usr/bin/env bash
# =============================================================================
# BR10 in nginx: DELETE /v1/jobs/{id} reaches the job's home store EXACTLY ONCE.
#
# Two facts, both invisible to `nginx -t`:
#
#   1. Routing. The cancel follows the id's home marker like the poll
#      (ADR-0012): own marker → this bridge's workers (claude_jobs_home), the
#      peer's marker → the peer (claude_jobs_peer), and a request that already
#      hopped (X-Bridge-Hop: 1) is never forwarded again.
#   2. No repetition of an ANSWER. The poll location inherits the server-level
#      `proxy_next_upstream error timeout http_5xx http_429`, and nginx counts
#      DELETE as idempotent — so a cancel a worker answered with 5xx would be
#      re-sent to the next one. @jobs_cancel retries only `error timeout`.
#   3. The worker's answer arrives as sent (BR10b): status AND its JSON body,
#      not nginx's HTML page (proxy_intercept_errors off).
#   4. Unreachable is skipped, fast (BR10b): a dead home worker costs the
#      2 s connect timeout and the next home worker answers — no 60 s 504.
#      Nothing reachable at all (every home worker, or the peer's nginx —
#      S4 "Peer tot") → a JSON job_home_unreachable answer within seconds.
#
# Every worker is its own stub container and counts the requests it got, so
# "how many workers saw this DELETE" is measured, not inferred. The same
# measurement against the nginx.conf of a base revision shows the repetition
# that this change removes:
#
#     tests/nginx/test_job_cancel_routing_e2e.sh                 # this tree
#     BASE_CONF=/path/to/old/nginx.conf tests/nginx/test_job_cancel_routing_e2e.sh --expect-repeat
#
# Requires docker. Builds the real docker/Dockerfile.nginx-lb.
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
D="$REPO/docker"
SUFFIX="$$"
NET="job-cancel-e2e-$SUFFIX"
IMG=pool-gate-e2e:local
PY=python:3.12-alpine
WORK="$(mktemp -d)"
CONF="${BASE_CONF:-$D/nginx.conf}"
EXPECT_REPEAT=0
[ "${1:-}" = "--expect-repeat" ] && EXPECT_REPEAT=1
FAILURES=0
STUBS="worker1 worker2 worker3 worker4 backup-host"
PORT=$((18100 + SUFFIX % 800))

cleanup() {
    for s in $STUBS nginx; do docker rm -f "jc-$s-$SUFFIX" >/dev/null 2>&1; done
    docker network rm "$NET" >/dev/null 2>&1
}
[ -n "${KEEP:-}" ] || trap 'cleanup; rm -rf "$WORK"' EXIT

# --- stub worker: counts every request; FAIL=1 answers an error with a JSON
# body — 502, or the status named by ?code= (500/503 …) ----------------------
cat > "$WORK/stub.py" <<'PYEOF'
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

NAME = os.environ["NAME"]
FAIL = os.getenv("FAIL") == "1"
HITS = []


class H(BaseHTTPRequestHandler):
    def _h(self):
        if self.path == "/__hits":
            body = json.dumps(HITS).encode()
            self.send_response(200)
        else:
            HITS.append({"method": self.command, "path": self.path,
                         "hop": self.headers.get("X-Bridge-Hop")})
            code = 200
            if FAIL:
                q = parse_qs(urlparse(self.path).query)
                code = int(q.get("code", ["502"])[0])
            body = json.dumps({"worker": NAME, "method": self.command,
                               "error": {"code": "stub_%d" % code}}).encode()
            self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_DELETE = _h

    def log_message(self, *a):
        pass


HTTPServer(("0.0.0.0", 8000), H).serve_forever()
PYEOF

if ! docker image inspect $IMG >/dev/null 2>&1; then
    echo "Building $IMG from docker/Dockerfile.nginx-lb ..."
    docker build -q -f "$D/Dockerfile.nginx-lb" -t $IMG "$REPO" >/dev/null || {
        echo "FAIL: image build"; exit 1; }
fi

start_stack() { # $1 = FAIL value for every stub (0|1)
    cleanup
    docker network create "$NET" >/dev/null
    # Same envsubst the compose performs. This LB is bridge "dev", its peer is
    # "prod" (upstreams-primary.conf). The geo{} block needs an address for
    # the backup host; the upstreams keep the resolvable stub alias.
    sed "s/\${BRIDGE_ID}/dev/g; s/\${BRIDGE_BACKUP_HOST}/192.0.2.10/g; s/\${METRICS_READER_TARGET}/metrics-elsewhere:8000/g" \
        "$CONF" > "$WORK/nginx.conf"
    sed 's/${BRIDGE_BACKUP_HOST}/backup-host/g; s/${BRIDGE_ID}/dev/g' \
        "$D/upstreams-primary.conf" > "$WORK/upstreams.conf"
    cp "$D/routes-metrics-reader.conf" "$D/routes-platform-api.conf" \
        "$D/worker-map-primary.conf" "$WORK/"
    for s in $STUBS; do
        docker run -d --name "jc-$s-$SUFFIX" --network "$NET" --network-alias "$s" \
            -e NAME="$s" -e FAIL="$1" \
            -v "$WORK/stub.py:/stub.py:ro" $PY python /stub.py >/dev/null
    done
    docker run -d --name "jc-nginx-$SUFFIX" --network "$NET" -p "127.0.0.1:$PORT:80" \
        --tmpfs /var/log/nginx \
        -e BRIDGE_WORKERS="worker1,worker2,worker3,worker4" \
        -e METRICS_READER_TARGET="metrics-elsewhere:8000" \
        -v "$WORK/nginx.conf:/usr/local/openresty/nginx/conf/nginx.conf:ro" \
        -v "$WORK/upstreams.conf:/tmp/upstreams.conf:ro" \
        -v "$WORK/worker-map-primary.conf:/tmp/worker-map.conf:ro" \
        -v "$WORK/routes-metrics-reader.conf:/etc/nginx/routes-metrics-reader.conf:ro" \
        -v "$WORK/routes-platform-api.conf:/etc/nginx/routes-platform-api.conf:ro" \
        $IMG >/dev/null
    for _ in $(seq 60); do
        # Any HTTP answer means nginx is up (/health itself proxies to the
        # stubs, which answer 502 in the failing scenario).
        [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health")" != 000 ] \
            && return 0
        sleep 0.25
    done
    echo "FAIL: nginx did not come up"; docker logs "jc-nginx-$SUFFIX" 2>&1 | tail -5
    exit 1
}

# hits <method> <path-substring> <stub...> → total matching requests on those stubs
hits() {
    local method="$1" needle="$2"; shift 2
    local total=0 n
    for s in "$@"; do
        n=$(docker exec "jc-$s-$SUFFIX" python -c "
import json, urllib.request
h = json.load(urllib.request.urlopen('http://127.0.0.1:8000/__hits'))
print(sum(1 for x in h if x['method'] == '$method' and '$needle' in x['path']))")
        total=$((total + n))
    done
    echo "$total"
}

check() { # <label> <actual> <op> <expected>
    if [ "$2" "$3" "$4" ]; then
        printf '  PASS  %-60s %s\n' "$1" "$2"
    else
        printf '  FAIL  %-60s %s (want %s %s)\n' "$1" "$2" "$3" "$4"
        FAILURES=$((FAILURES + 1))
    fi
}

# timed <outfile> <curl args...> → prints "<http_code> <seconds>", body to outfile
timed() {
    local out="$1"; shift
    curl -s -m 90 -o "$out" -D "$out.h" -w '%{http_code} %{time_total}' "$@"
}

# json_field <file> <dotted.path> → value, or MISSING / NOT_JSON
json_field() {
    python3 -c "
import json, sys
try:
    v = json.load(open('$1'))
except Exception:
    print('NOT_JSON'); sys.exit()
for k in '$2'.split('.'):
    v = v.get(k) if isinstance(v, dict) else None
print('MISSING' if v is None else v)"
}

lt() { python3 -c "import sys; sys.exit(0 if float('$1') < float('$2') else 1)"; }

LOCAL="worker1 worker2 worker3 worker4"
OWN="job_dev_0123456789abcdef0123456789abcdef"
OWN2="job_dev_fedcba9876543210fedcba9876543210"
PEER="job_prod_0123456789abcdef0123456789abcdef"
PEER2="job_prod_fedcba9876543210fedcba9876543210"

echo "=== config under test: $CONF ==="
echo
echo "=== Scenario: every worker FAILS (502) — how often is the request sent? ==="
start_stack 1
code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "http://127.0.0.1:$PORT/v1/jobs/$OWN" -H "X-Bridge-Hop: 1")
n_del=$(hits DELETE "$OWN" $LOCAL)
curl -s -o /dev/null "http://127.0.0.1:$PORT/v1/jobs/$OWN2" -H "X-Bridge-Hop: 1"
n_get=$(hits GET "$OWN2" $LOCAL)
# The control: the poll IS repeated across workers — the inherited directive is
# live in this location, so it would apply to DELETE as well without @jobs_cancel.
check "control: failed GET poll repeated across home workers" "$n_get" -gt 1
if [ $EXPECT_REPEAT = 1 ]; then
    check "BASE: failed DELETE repeated across home workers" "$n_del" -gt 1
else
    check "failed DELETE sent to exactly one home worker" "$n_del" -eq 1
    # The worker's own answer, passed through: one attempt, nothing rewritten.
    check "failed DELETE: caller gets that worker's status" "$code" = 502
    for c in 500 502 503; do
        id="job_dev_${c}00000000000000000000000000000"
        read -r got _t < <(timed "$WORK/b$c" -X DELETE \
            "http://127.0.0.1:$PORT/v1/jobs/$id?code=$c" -H "X-Bridge-Hop: 1")
        check "worker $c: status passed through" "$got" = "$c"
        check "worker $c: JSON body of the worker (not nginx HTML)" \
            "$(json_field "$WORK/b$c" error.code)" = "stub_$c"
        check "worker $c: sent to exactly one home worker" "$(hits DELETE "$id" $LOCAL)" -eq 1
    done
fi

if [ $EXPECT_REPEAT = 0 ]; then
    echo
    echo "=== Scenario: workers healthy — where does the DELETE land? ==="
    start_stack 0
    out=$(curl -s -w '\n%{http_code}' -X DELETE "http://127.0.0.1:$PORT/v1/jobs/$OWN" -H "X-Bridge-Hop: 1")
    check "own marker: answered 200 by a worker" "$(printf '%s' "$out" | tail -1)" = 200
    check "own marker: one DELETE on the home workers" "$(hits DELETE "$OWN" $LOCAL)" -eq 1
    check "own marker: no DELETE on the peer" "$(hits DELETE "$OWN" backup-host)" -eq 0

    curl -s -o /dev/null -X DELETE "http://127.0.0.1:$PORT/v1/jobs/$PEER"
    check "peer marker (not hopped): one DELETE forwarded to the peer" "$(hits DELETE "$PEER" backup-host)" -eq 1
    check "peer marker (not hopped): none on the home workers" "$(hits DELETE "$PEER" $LOCAL)" -eq 0

    curl -s -o /dev/null -X DELETE "http://127.0.0.1:$PORT/v1/jobs/$PEER2" -H "X-Bridge-Hop: 1"
    check "peer marker (already hopped): stays local, never forwarded" "$(hits DELETE "$PEER2" backup-host)" -eq 0
    check "peer marker (already hopped): one DELETE on a home worker" "$(hits DELETE "$PEER2" $LOCAL)" -eq 1

    # GET is untouched by the DELETE branch.
    curl -s -o /dev/null "http://127.0.0.1:$PORT/v1/jobs/$OWN2" -H "X-Bridge-Hop: 1"
    check "GET poll still served by the home workers" "$(hits GET "$OWN2" $LOCAL)" -eq 1

    echo
    echo "=== Scenario: one home worker DEAD (stopped) — skipped, fast ==="
    # A stopped container's address stays in nginx's upstream and answers
    # nothing: the connect hangs (measured BR10R: 60 s → 504 HTML).
    docker stop -t0 "jc-worker1-$SUFFIX" >/dev/null
    ALIVE="worker2 worker3 worker4"
    ok=0; slowest=0
    for i in 1 2 3 4 5 6 7 8; do
        id="job_dev_dead${i}000000000000000000000000000"
        read -r got t < <(timed "$WORK/d$i" -X DELETE "http://127.0.0.1:$PORT/v1/jobs/$id" \
            -H "X-Bridge-Hop: 1")
        [ "$got" = 200 ] && [ "$(hits DELETE "$id" $ALIVE)" -eq 1 ] && ok=$((ok + 1))
        lt "$slowest" "$t" && slowest=$t
    done
    check "8 DELETEs, each answered 200 by exactly one live home worker" "$ok" -eq 8
    echo "  info  slowest of the 8: ${slowest} s"
    if lt "$slowest" 4; then fast=yes; else fast="no ($slowest s)"; fi
    check "slowest of them under 4 s (dead worker costs the connect timeout)" "$fast" = yes

    echo
    echo "=== Scenario: EVERY home worker dead — JSON, within seconds ==="
    for s in worker2 worker3 worker4; do docker stop -t0 "jc-$s-$SUFFIX" >/dev/null; done
    read -r got t < <(timed "$WORK/all" -X DELETE "http://127.0.0.1:$PORT/v1/jobs/$OWN" \
        -H "X-Bridge-Hop: 1")
    check "all home workers dead: 502/504" "$( [ "$got" = 502 ] || [ "$got" = 504 ] && echo 5xx || echo "$got")" = 5xx
    check "all home workers dead: JSON job_home_unreachable" \
        "$(json_field "$WORK/all" error.code)" = job_home_unreachable
    echo "  info  every home worker dead: HTTP $got after ${t} s, $(head -c 160 "$WORK/all")"
    if lt "$t" 8; then fast=yes; else fast="no ($t s)"; fi
    check "all home workers dead: answered under 8 s" "$fast" = yes
    # Control: the poll keeps its own failure answer (@bridge_full).
    read -r got _t < <(timed "$WORK/allget" "http://127.0.0.1:$PORT/v1/jobs/$OWN2" \
        -H "X-Bridge-Hop: 1")
    check "control: GET with all workers dead stays @bridge_full JSON" \
        "$(json_field "$WORK/allget" error.bridge_type)" = capacity_busy

    echo
    echo "=== Scenario: PEER dead (S4) — JSON, within seconds, nothing local ==="
    start_stack 0
    docker stop -t0 "jc-backup-host-$SUFFIX" >/dev/null
    read -r got t < <(timed "$WORK/peer" -X DELETE "http://127.0.0.1:$PORT/v1/jobs/$PEER")
    check "peer dead: 502/504" "$( [ "$got" = 502 ] || [ "$got" = 504 ] && echo 5xx || echo "$got")" = 5xx
    check "peer dead: JSON job_home_unreachable" \
        "$(json_field "$WORK/peer" error.code)" = job_home_unreachable
    check "peer dead: X-Job-Home names the peer" \
        "$(grep -i '^x-job-home:' "$WORK/peer.h" | tr -d '\r' | awk '{print $2}')" = prod
    echo "  info  peer dead: HTTP $got after ${t} s, $(head -c 160 "$WORK/peer")"
    if lt "$t" 5; then fast=yes; else fast="no ($t s)"; fi
    check "peer dead: answered under 5 s" "$fast" = yes
    check "peer dead: never answered by a home worker instead" "$(hits DELETE "$PEER" $LOCAL)" -eq 0
fi

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "JOB_CANCEL_E2E_OK"
    exit 0
fi
echo "JOB_CANCEL_E2E_FAIL: $FAILURES failure(s)"
exit 1
