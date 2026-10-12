#!/usr/bin/env bash
# BRREF2b — keyed job submissions keep to ONE bridge (docker/nginx.conf
# @keyed_jobs_peer*, generator emit_keyed_job_routing).
#
# The Idempotency-Key is exactly-once per bridge DATABASE. On the primary
# bridge POST /v1/jobs goes to the peer first; before BRREF2b a lost answer
# marked the peer down (max_fails=1) and the client's retry with the same key
# landed on the local workers: a second job. Rule now: same key → same bridge;
# the local tier only after a CONFIRMED refusal (peer not reached, its pool
# gate, or 429 from every worker behind it); every unknown outcome → retryable
# 503 to the client, no local fallback. Without the key: unchanged.
#
# Real nginx.conf + real upstreams-primary.conf against stub workers and a
# stub peer that keeps key → job like a database. Stub hosts only (docker
# aliases worker1..4, backup-host) — no real address is ever configured.
#
# USAGE  tests/nginx/test_keyed_jobs_routing_e2e.sh      (needs docker)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
D="$REPO/docker"
SUFFIX="$$"
NET="keyed-jobs-e2e-$SUFFIX"
IMG=pool-gate-e2e:local
PY=python:3.12-alpine
WORK="$(mktemp -d)"
FAILURES=0
STUBS="worker1 worker2 worker3 worker4 backup-host"
PORT=$((19000 + SUFFIX % 800))
URL="http://127.0.0.1:$PORT"

cleanup() {
    for s in $STUBS nginx; do docker rm -f "kj-$s-$SUFFIX" >/dev/null 2>&1; done
    docker network rm "$NET" >/dev/null 2>&1
}
[ -n "${KEEP:-}" ] || trap 'cleanup; rm -rf "$WORK"' EXIT

cat > "$WORK/stub.py" <<'PYEOF'
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

NAME = os.environ["NAME"]
PEER = NAME == "backup-host"
HITS = []
JOBS = {}          # key -> job_id, the peer's "database"
SEEN_LOSE = set()


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/__hits":
            return self._send(200, HITS)
        if self.path == "/__down":
            threading.Thread(target=lambda: (SRV.shutdown(), SRV.server_close())).start()
            return self._send(200, {"down": True})
        return self._send(200, {"worker": NAME})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        key = self.headers.get("Idempotency-Key")
        mode = self.headers.get("X-Stub-Peer", "accept") if PEER else "accept"
        HITS.append({"method": "POST", "path": self.path, "key": key,
                     "hop": self.headers.get("X-Bridge-Hop"), "len": len(body)})
        if mode == "lose_once" and key not in SEEN_LOSE:
            SEEN_LOSE.add(key)
            JOBS.setdefault(key, f"job_{NAME}_{len(JOBS)}")   # persisted ...
            self.close_connection = True                      # ... answer lost
            self.connection.shutdown(2)
            return
        if mode == "pool_exhausted":
            return self._send(429, {"e": 1}, {"X-Bridge-Capacity": "pool_exhausted",
                                              "X-Upstream-Status": "0"})
        if mode == "workers429":
            return self._send(429, {"e": 1}, {"X-Bridge-Capacity": "rate_limited",
                                              "X-Upstream-Status": "429, 429, 429, 429"})
        if mode == "ambig429":
            return self._send(429, {"e": 1}, {"X-Bridge-Capacity": "capacity_full",
                                              "X-Upstream-Status": "504"})
        if mode == "busy503":
            return self._send(503, {"e": 1}, {"X-Bridge-Capacity": "capacity_busy",
                                              "X-Upstream-Status": "500"})
        if mode == "e422":
            return self._send(422, {"error": {"code": "idempotency_key_reused"}})
        if key is None:
            return self._send(200, {"job_id": f"job_{NAME}_nokey_{len(HITS)}",
                                    "worker": NAME, "len": len(body)})
        replay = key in JOBS
        job = JOBS.setdefault(key, f"job_{NAME}_{len(JOBS)}")
        return self._send(200 if replay else 201,
                          {"job_id": job, "worker": NAME, "len": len(body)},
                          {"Idempotent-Replayed": "true" if replay else "false"})

    def log_message(self, *a):
        pass


SRV = HTTPServer(("0.0.0.0", 8000), H)
SRV.serve_forever()
while True:
    time.sleep(3600)
PYEOF

if ! docker image inspect $IMG >/dev/null 2>&1; then
    echo "Building $IMG from docker/Dockerfile.nginx-lb ..."
    docker build -q -f "$D/Dockerfile.nginx-lb" -t $IMG "$REPO" >/dev/null || {
        echo "FAIL: image build"; exit 1; }
fi

start_stack() {
    cleanup
    docker network create "$NET" >/dev/null
    sed "s/\${BRIDGE_ID}/dev/g; s/\${BRIDGE_BACKUP_HOST}/192.0.2.10/g; s/\${METRICS_READER_TARGET}/metrics-elsewhere:8000/g" \
        "$D/nginx.conf" > "$WORK/nginx.conf"
    sed 's/${BRIDGE_BACKUP_HOST}/backup-host/g; s/${BRIDGE_ID}/dev/g' \
        "$D/upstreams-primary.conf" > "$WORK/upstreams.conf"
    # Guard: only stub hosts may be configured as upstream targets.
    if grep -E 'server +[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' "$WORK/upstreams.conf"; then
        echo "ABORT: real IP in upstreams under test"; exit 2
    fi
    cp "$D/routes-metrics-reader.conf" "$D/routes-platform-api.conf" \
        "$D/worker-map-primary.conf" "$WORK/"
    for s in $STUBS; do
        docker run -d --name "kj-$s-$SUFFIX" --network "$NET" --network-alias "$s" \
            --memory 64m -e NAME="$s" \
            -v "$WORK/stub.py:/stub.py:ro" $PY python /stub.py >/dev/null
    done
    docker run -d --name "kj-nginx-$SUFFIX" --network "$NET" -p "127.0.0.1:$PORT:80" \
        --memory 128m --tmpfs /var/log/nginx \
        -e BRIDGE_WORKERS="worker1,worker2,worker3,worker4" \
        -e METRICS_READER_TARGET="metrics-elsewhere:8000" \
        -v "$WORK/nginx.conf:/usr/local/openresty/nginx/conf/nginx.conf:ro" \
        -v "$WORK/upstreams.conf:/tmp/upstreams.conf:ro" \
        -v "$WORK/worker-map-primary.conf:/tmp/worker-map.conf:ro" \
        -v "$WORK/routes-metrics-reader.conf:/etc/nginx/routes-metrics-reader.conf:ro" \
        -v "$WORK/routes-platform-api.conf:/etc/nginx/routes-platform-api.conf:ro" \
        $IMG >/dev/null
    for _ in $(seq 60); do
        [ "$(curl -s -o /dev/null -w '%{http_code}' "$URL/health")" != 000 ] && return 0
        sleep 0.25
    done
    echo "FAIL: nginx did not come up"; docker logs "kj-nginx-$SUFFIX" 2>&1 | tail -5
    exit 1
}

hits() { # <key or -> <stubs...>  → POSTs carrying that key (- = without key)
    local key="$1"; shift
    local total=0 n
    for s in "$@"; do
        n=$(docker exec "kj-$s-$SUFFIX" python -c "
import json, urllib.request
h = json.load(urllib.request.urlopen('http://127.0.0.1:8000/__hits'))
k = None if '$key' == '-' else '$key'
print(sum(1 for x in h if x['method'] == 'POST' and x['key'] == k))" 2>/dev/null || echo 0)
        total=$((total + n))
    done
    echo "$total"
}

check() { # <label> <actual> <op> <expected>
    if [ "$2" "$3" "$4" ]; then
        printf '  PASS  %-62s %s\n' "$1" "$2"
    else
        printf '  FAIL  %-62s %s (want %s %s)\n' "$1" "$2" "$3" "$4"
        FAILURES=$((FAILURES + 1))
    fi
}

post() { # <outfile> <key or -> [extra curl args...] → http code
    local out="$1" key="$2"; shift 2
    local kh=()
    [ "$key" != "-" ] && kh=(-H "Idempotency-Key: $key")
    curl -s -m 60 -o "$out" -D "$out.h" -w '%{http_code}' -X POST "$URL/v1/jobs" \
        -H "Content-Type: application/json" "${kh[@]}" "$@" \
        --data '{"kind":"chat","payload":{"text":"hallo"}}'
}

field() { python3 -c "
import json
try: v = json.load(open('$1'))
except Exception: print('NOT_JSON'); raise SystemExit
for k in '$2'.split('.'): v = v.get(k) if isinstance(v, dict) else None
print('MISSING' if v is None else v)"; }

header() { grep -i "^$2:" "$1.h" | head -1 | cut -d: -f2- | tr -d ' \r'; }

LOCAL="worker1 worker2 worker3 worker4"
start_stack
O="$WORK/o"

echo "=== 1. key, peer accepts → peer, never local ==="
c=$(post "$O" K1); check "K1 status" "$c" = 201
check "K1 answered by the peer" "$(field "$O" worker)" = backup-host
check "K1 local POSTs" "$(hits K1 $LOCAL)" -eq 0

echo "=== 2. key, peer takes the job, answer LOST → retry = same job, no local ==="
c=$(post "$O" K2 -H "X-Stub-Peer: lose_once"); check "K2 first: status" "$c" = 503
check "K2 first: reason" "$(field "$O" error.reason)" = idempotent_outcome_unknown
check "K2 first: retryable" "$(field "$O" error.retryable)" = True
check "K2 first: Idempotency-Key echoed" "$(header "$O" Idempotency-Key)" = K2
check "K2 first: no local POST" "$(hits K2 $LOCAL)" -eq 0
c=$(post "$O" K2); check "K2 retry: status (replay)" "$c" = 200
check "K2 retry: same job as the peer stored" "$(field "$O" job_id)" = job_backup-host_1
check "K2 retry: answered by the peer" "$(field "$O" worker)" = backup-host
check "K2 retry: still no local POST" "$(hits K2 $LOCAL)" -eq 0
check "K2 peer POSTs (lost + retry)" "$(hits K2 backup-host)" -eq 2
c=$(post "$O" K3); check "K3 right after the loss: peer not marked down" "$(field "$O" worker)" = backup-host

echo "=== 3. key, CONFIRMED refusals → local tier ==="
c=$(post "$O" K4 -H "X-Stub-Peer: pool_exhausted"); check "K4 pool_exhausted: status" "$c" = 201
check "K4 answered locally" "$(hits K4 $LOCAL)" -eq 1
check "K4 local got the whole body" "$(field "$O" len)" -gt 30
check "K4 fallback reason" "$(header "$O" X-Keyed-Fallback)" = peer_pool_exhausted
c=$(post "$O" K5 -H "X-Stub-Peer: workers429"); check "K5 all-429 behind the peer → local" "$(hits K5 $LOCAL)" -eq 1
check "K5 fallback reason" "$(header "$O" X-Keyed-Fallback)" = peer_workers_429

echo "=== 4. key, UNKNOWN outcomes → 503 retryable, no local ==="
for m in ambig429 busy503; do
    c=$(post "$O" "K-$m" -H "X-Stub-Peer: $m"); check "K-$m: status" "$c" = 503
    check "K-$m: reason" "$(field "$O" error.reason)" = idempotent_outcome_unknown
    check "K-$m: no local POST" "$(hits "K-$m" $LOCAL)" -eq 0
done

echo "=== 5. key, peer verdict on the request passes through ==="
c=$(post "$O" K6 -H "X-Stub-Peer: e422"); check "K6 422 passes" "$c" = 422
check "K6 no local POST" "$(hits K6 $LOCAL)" -eq 0

echo "=== 6. key + hopped → local tier, as before ==="
c=$(post "$O" K7 -H "X-Bridge-Hop: 1"); check "K7 hopped: local" "$(hits K7 $LOCAL)" -eq 1
check "K7 hopped: peer untouched" "$(hits K7 backup-host)" -eq 0

echo "=== 7. without key: unchanged ==="
c=$(post "$O" - -H "X-Stub-Peer: pool_exhausted"); check "no key, peer 429 → local (as before)" "$(field "$O" worker)" != backup-host
check "no key, no keyed fallback header" "$(header "$O" X-Keyed-Fallback)" = ""

echo "=== 8. key, peer NOT reachable (nothing sent) → local ==="
docker exec "kj-backup-host-$SUFFIX" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/__down')" >/dev/null
sleep 0.5
c=$(post "$O" K8); check "K8 peer down: status" "$c" = 201
check "K8 answered locally" "$(hits K8 $LOCAL)" -eq 1
check "K8 fallback reason" "$(header "$O" X-Keyed-Fallback)" = peer_not_reached

echo
if [ "$FAILURES" -eq 0 ]; then echo "ALL PASS"; else echo "$FAILURES FAILURE(S)"; docker logs "kj-nginx-$SUFFIX" 2>&1 | grep -i "keyed\|emerg\|error" | tail -8; fi
exit $(( FAILURES > 0 ))
