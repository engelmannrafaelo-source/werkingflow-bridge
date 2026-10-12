#!/usr/bin/env bash
# BRREF2b/IDEMR — a keyed job lives on ONE fixed bridge (docker/nginx.conf
# @keyed_jobs_*, generator emit_keyed_job_routing).
#
# The Idempotency-Key is exactly-once per bridge DATABASE. Rule (Kanzler,
# after Codex IDEMR): with a key there is NO fallback to the other bridge —
# primary topology: always the peer; production topology and hopped requests:
# always the local workers. Fixed bridge busy → its 429 passes; unreachable or
# 5xx → 503 idempotent_outcome_unknown, retryable. No nginx pool gate for
# keyed submissions (the worker looks the key up before its capacity veto).
# Without the key: unchanged.
#
# Real nginx.conf + real upstreams (primary AND production, IPs replaced by
# stub hosts; a guard aborts on any remaining IP) against stub workers and a
# stub peer that keep key → job like a database.
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
SEEN_LOSE = set()


class Jobs:
    """key -> job_id. The peer keeps its own; the four local stubs share one
    directory, like the real local workers share one database."""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def __contains__(self, key):
        return os.path.exists(os.path.join(self.root, key))

    def __len__(self):
        return len(os.listdir(self.root))

    def setdefault(self, key, job):
        path = os.path.join(self.root, key)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, job.encode())
            os.close(fd)
        except FileExistsError:
            pass
        with open(path) as f:
            return f.read()


JOBS = Jobs("/peerdb" if PEER else "/db")


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
        mode = (self.headers.get("X-Stub-Peer", "accept") if PEER
                else self.headers.get("X-Stub-Local", "accept"))
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

start_stack() { # <primary|prod>
    local topo="$1" id=dev
    [ "$topo" = prod ] && id=prod
    cleanup
    docker network create "$NET" >/dev/null
    sed "s/\${BRIDGE_ID}/$id/g; s/\${BRIDGE_BACKUP_HOST}/192.0.2.10/g; s/\${METRICS_READER_TARGET}/metrics-elsewhere:8000/g" \
        "$D/nginx.conf" > "$WORK/nginx.conf"
    # Worker addresses of the production file → the stub aliases worker1..4.
    sed -E "s/\\$\\{BRIDGE_BACKUP_HOST\\}/backup-host/g; s/\\$\\{BRIDGE_ID\\}/$id/g; s/[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+:800([1-4])/worker\\1:8000/g" \
        "$D/upstreams-$topo.conf" > "$WORK/upstreams.conf"
    # Guard: only stub hosts may be configured as upstream targets.
    if grep -E 'server +[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' "$WORK/upstreams.conf"; then
        echo "ABORT: real IP in upstreams under test"; exit 2
    fi
    cp "$D/routes-metrics-reader.conf" "$D/routes-platform-api.conf" \
        "$D/worker-map-primary.conf" "$WORK/"
    rm -rf "$WORK/db"; mkdir -p "$WORK/db"; chmod 777 "$WORK/db"
    for s in $STUBS; do
        docker run -d --name "kj-$s-$SUFFIX" --network "$NET" --network-alias "$s" \
            --memory 64m -e NAME="$s" -v "$WORK/db:/db" \
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
O="$WORK/o"

echo "############ PRIMARY topology (fixed bridge = peer) ############"
start_stack primary

echo "=== 1. key, peer accepts → peer, never local ==="
c=$(post "$O" K1); check "K1 status" "$c" = 201
check "K1 answered by the peer" "$(field "$O" worker)" = backup-host
check "K1 local POSTs" "$(hits K1 $LOCAL)" -eq 0

echo "=== 2. peer takes the job, answer LOST → retry = same job, no local ==="
c=$(post "$O" K2 -H "X-Stub-Peer: lose_once"); check "K2 first: status" "$c" = 503
check "K2 first: reason" "$(field "$O" error.reason)" = idempotent_outcome_unknown
check "K2 first: retryable" "$(field "$O" error.retryable)" = True
check "K2 first: Idempotency-Key echoed" "$(header "$O" Idempotency-Key)" = K2
c=$(post "$O" K2); check "K2 retry: status (replay)" "$c" = 200
check "K2 retry: same job as the peer stored" "$(field "$O" job_id)" = job_backup-host_1
check "K2 local POSTs" "$(hits K2 $LOCAL)" -eq 0
c=$(post "$O" K3); check "K3 right after the loss: still the peer" "$(field "$O" worker)" = backup-host

echo "=== 3. peer busy → its 429 passes, NO local fallback ==="
for m in pool_exhausted workers429 ambig429; do
    c=$(post "$O" "K-$m" -H "X-Stub-Peer: $m"); check "K-$m: status" "$c" = 429
    check "K-$m: no local POST" "$(hits "K-$m" $LOCAL)" -eq 0
done
c=$(post "$O" K-busy503 -H "X-Stub-Peer: busy503"); check "K-busy503: status" "$c" = 503
check "K-busy503: reason" "$(field "$O" error.reason)" = idempotent_outcome_unknown
check "K-busy503: no local POST" "$(hits K-busy503 $LOCAL)" -eq 0

echo "=== Codex IDEMR A: peer refused, then recovers → ONE job only ==="
c=$(post "$O" K-pool_exhausted); check "A retry at the recovered peer" "$c" = 201
c=$(post "$O" K-pool_exhausted); check "A next retry: replay" "$c" = 200
check "A local POSTs (no second job anywhere)" "$(hits K-pool_exhausted $LOCAL)" -eq 0

echo "=== Codex IDEMR B: key exists at the peer, peer at capacity → no local job ==="
c=$(post "$O" K2 -H "X-Stub-Peer: workers429")
check "B status" "$c" = 429
check "B local POSTs" "$(hits K2 $LOCAL)" -eq 0

echo "=== Codex IDEMR C: no local attempt exists that could be lost ==="
c=$(post "$O" KC -H "X-Stub-Peer: pool_exhausted" -H "X-Stub-Local: lose_once")
check "C refused attempt: status" "$c" = 429
c=$(post "$O" KC); check "C retry at the peer: created once" "$c" = 201
check "C local POSTs" "$(hits KC $LOCAL)" -eq 0

echo "=== 4. peer verdict on the request passes through ==="
c=$(post "$O" K6 -H "X-Stub-Peer: e422"); check "K6 422 passes" "$c" = 422
check "K6 no local POST" "$(hits K6 $LOCAL)" -eq 0

echo "=== 5. key + hopped → local workers (this IS the fixed bridge) ==="
c=$(post "$O" K7 -H "X-Bridge-Hop: 1"); check "K7 hopped: local" "$(hits K7 $LOCAL)" -eq 1
check "K7 hopped: peer untouched" "$(hits K7 backup-host)" -eq 0
check "K7 hopped: status" "$c" = 201
c=$(post "$O" K7b -H "X-Bridge-Hop: 1" -H "X-Stub-Local: workers429")
check "K7b hopped, all local 429: status" "$c" = 429
check "K7b every local worker asked (429 retry)" "$(hits K7b $LOCAL)" -eq 4
check "K7b peer untouched" "$(hits K7b backup-host)" -eq 0

echo "=== 6. without key: unchanged ==="
c=$(post "$O" - -H "X-Stub-Peer: pool_exhausted"); check "no key, peer 429 → local (as before)" "$(field "$O" worker)" != backup-host

echo "=== 7. key, peer NOT reachable → 503 unknown, no local ==="
docker exec "kj-backup-host-$SUFFIX" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/__down')" >/dev/null
sleep 0.5
c=$(post "$O" K8); check "K8 peer down: status" "$c" = 503
check "K8 reason" "$(field "$O" error.reason)" = idempotent_outcome_unknown
check "K8 no local POST" "$(hits K8 $LOCAL)" -eq 0

echo "############ PRODUCTION topology (fixed bridge = local) ############"
start_stack prod

echo "=== P1. key → local, never the peer ==="
c=$(post "$O" P1); check "P1 status" "$c" = 201
check "P1 local" "$(hits P1 $LOCAL)" -eq 1
check "P1 peer untouched" "$(hits P1 backup-host)" -eq 0

echo "=== P2. key + X-Priority production, all local 429 → 429, NOT the peer ==="
c=$(post "$O" P2 -H "X-Priority: production" -H "X-Stub-Local: workers429")
check "P2 status" "$c" = 429
check "P2 peer untouched" "$(hits P2 backup-host)" -eq 0

echo "=== P3. key, local answer LOST → 503 unknown, retry = same job ==="
c=$(post "$O" P3 -H "X-Stub-Local: lose_once"); check "P3 first: status" "$c" = 503
check "P3 first: reason" "$(field "$O" error.reason)" = idempotent_outcome_unknown
c=$(post "$O" P3); check "P3 retry: replay" "$c" = 200
check "P3 peer untouched" "$(hits P3 backup-host)" -eq 0

echo "=== P4. without key: unchanged (X-Priority production overflows to the peer) ==="
c=$(post "$O" - -H "X-Priority: production" -H "X-Stub-Local: workers429")
check "P4 no key: reached the peer as before" "$(hits - backup-host)" -ge 1

echo
if [ "$FAILURES" -eq 0 ]; then echo "ALL PASS"; else echo "$FAILURES FAILURE(S)"; docker logs "kj-nginx-$SUFFIX" 2>&1 | grep -i "keyed\|emerg\|error" | tail -8; fi
exit $(( FAILURES > 0 ))
