#!/usr/bin/env bash
# DEV-KEIN-PROD-HOP (KD-Entscheid 03.11.2026) — end to end against a real
# OpenResty with the REAL shared docker/nginx.conf, the REAL start script
# (docker/nginx-start.sh) and the COMMITTED docker/upstreams-primary.conf
# (generator default PRIMARY_PEER_HOP=0).
#
# What it proves (contract: docs/contracts/dev-kein-prod-hop.md):
#   * dev/staging traffic without X-Priority never reaches the peer (= the
#     prod bridge): chat, research, POST /v1/jobs with and without
#     Idempotency-Key, /v1/jobs/lookup, the jobs list, polls of own and of
#     unmarked ids — all answered by the local workers;
#   * polls and cancels of a job whose id names the prod bridge STILL go to
#     the peer (jobs in flight at cutover keep working, ADR-0012);
#   * X-Priority: production still rides claude_production = peer first
#     (deliberately unchanged), keyed X-Priority jobs keep their fixed
#     bridge at the peer, the Gemini image path stays local;
#   * the rollback (PRIMARY_PEER_HOP=1) restores peer-first.
#
# Stub workers and a stub peer only — no network outside the docker test net.
# USAGE  tests/nginx/test_dev_no_prod_hop_e2e.sh      (needs docker)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
D="$REPO/docker"
SUFFIX="$$"
NET="dev-no-hop-e2e-$SUFFIX"
IMG=pool-gate-e2e:local
PY=python:3.12-alpine
WORK="$(mktemp -d)"
FAILURES=0
STUBS="worker1 worker2 worker3 worker4 backup-host"
LOCAL="worker1 worker2 worker3 worker4"
PORT=$((20000 + SUFFIX % 800))
URL="http://127.0.0.1:$PORT"
# TEST-NET-1 stands in for the prod bridge address in the geo{} blocks; the
# upstreams keep the resolvable stub alias backup-host.
PEER_ADDR=192.0.2.10

cleanup() {
    for s in $STUBS nginx; do docker rm -f "dh-$s-$SUFFIX" >/dev/null 2>&1; done
    docker network rm "$NET" >/dev/null 2>&1
}
[ -n "${KEEP:-}" ] || trap 'cleanup; rm -rf "$WORK"' EXIT

cat > "$WORK/stub.py" <<'PYEOF'
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

NAME = os.environ["NAME"]
PEER = NAME == "backup-host"
HITS = []
DB = "/peerdb" if PEER else "/db"
os.makedirs(DB, exist_ok=True)


class H(BaseHTTPRequestHandler):
    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _hit(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        HITS.append({"method": self.command, "path": self.path,
                     "key": self.headers.get("Idempotency-Key")})

    def do_GET(self):
        if self.path == "/__hits":
            return self._send(200, HITS)
        if self.path == "/health":
            return self._send(200, {"worker": NAME})
        self._hit()
        return self._send(200, {"worker": NAME})

    def do_DELETE(self):
        self._hit()
        return self._send(200, {"worker": NAME, "cancelled": True})

    def do_POST(self):
        self._hit()
        if PEER and self.headers.get("X-Stub-Peer") == "workers429":
            return self._send(429, {"error": {"reason": "account_exhausted"}})
        key = self.headers.get("Idempotency-Key")
        if key is None or self.path != "/v1/jobs":
            return self._send(200, {"worker": NAME})
        path = os.path.join(DB, key)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"job_{NAME}_{key}".encode())
            os.close(fd)
            created = True
        except FileExistsError:
            created = False
        with open(path) as f:
            return self._send(201 if created else 200, {"worker": NAME, "job_id": f.read()})

    def log_message(self, *a):
        pass


HTTPServer(("0.0.0.0", 8000), H).serve_forever()
PYEOF

if ! docker image inspect $IMG >/dev/null 2>&1; then
    echo "Building $IMG from docker/Dockerfile.nginx-lb ..."
    docker build -q -f "$D/Dockerfile.nginx-lb" -t $IMG "$REPO" >/dev/null || {
        echo "FAIL: image build"; exit 1; }
fi

start_stack() { # <path of the upstreams include under test>
    local src="$1"
    cleanup
    docker network create "$NET" >/dev/null
    sed 's/${BRIDGE_BACKUP_HOST}/backup-host/g' "$src" > "$WORK/upstreams.conf"
    if grep -E 'server +[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' "$WORK/upstreams.conf"; then
        echo "ABORT: real IP in upstreams under test"; exit 2
    fi
    cp "$D/routes-metrics-reader.conf" "$D/routes-platform-api.conf" "$D/worker-map-primary.conf" "$WORK/"
    rm -rf "$WORK/db"; mkdir -p "$WORK/db"; chmod 777 "$WORK/db"
    for s in $STUBS; do
        docker run -d --name "dh-$s-$SUFFIX" --network "$NET" --network-alias "$s" \
            --memory 64m -e NAME="$s" -v "$WORK/db:/db" \
            -v "$WORK/stub.py:/stub.py:ro" $PY python /stub.py >/dev/null
    done
    # BRIDGE_ID stays a ${VAR} until the REAL start script envsubst's it.
    docker run -d --name "dh-nginx-$SUFFIX" --network "$NET" -p "127.0.0.1:$PORT:80" \
        --memory 128m --tmpfs /var/log/nginx \
        -e BRIDGE_WORKERS="worker1,worker2,worker3,worker4" \
        -e METRICS_READER_TARGET="metrics-elsewhere:8000" \
        -e BRIDGE_BACKUP_HOST="$PEER_ADDR" -e BRIDGE_ID=dev \
        -v "$D/nginx.conf:/etc/nginx/nginx.conf:ro" \
        -v "$WORK/upstreams.conf:/etc/nginx/upstreams.conf:ro" \
        -v "$WORK/worker-map-primary.conf:/etc/nginx/worker-map.conf:ro" \
        -v "$WORK/routes-metrics-reader.conf:/etc/nginx/routes-metrics-reader.conf:ro" \
        -v "$WORK/routes-platform-api.conf:/etc/nginx/routes-platform-api.conf:ro" \
        -v "$D/lua:/etc/nginx/lua:ro" \
        -v "$D/nginx-start.sh:/etc/nginx/nginx-start.sh:ro" \
        $IMG /bin/sh /etc/nginx/nginx-start.sh >/dev/null
    for _ in $(seq 60); do
        [ "$(curl -s -o /dev/null -w '%{http_code}' "$URL/health")" != 000 ] && return 0
        sleep 0.25
    done
    echo "FAIL: nginx did not come up"; docker logs "dh-nginx-$SUFFIX" 2>&1 | tail -5
    exit 1
}

hits() { # <path-substring> <stubs...> → number of recorded requests whose path contains it
    local needle="$1"; shift
    local total=0 n
    for s in "$@"; do
        n=$(docker exec "dh-$s-$SUFFIX" python -c "
import json, urllib.request
h = json.load(urllib.request.urlopen('http://127.0.0.1:8000/__hits'))
print(sum(1 for x in h if '$needle' in (x['path'] + '|' + str(x['key']))))" 2>/dev/null || echo 0)
        total=$((total + n))
    done
    echo "$total"
}

all_peer_hits() {
    docker exec "dh-backup-host-$SUFFIX" python -c "
import json, urllib.request
print(len(json.load(urllib.request.urlopen('http://127.0.0.1:8000/__hits'))))"
}

check() { # <label> <actual> <op> <expected>
    if [ "$2" "$3" "$4" ]; then
        printf '  PASS  %-66s %s\n' "$1" "$2"
    else
        printf '  FAIL  %-66s %s (want %s %s)\n' "$1" "$2" "$3" "$4"
        FAILURES=$((FAILURES + 1))
    fi
}

req() { # <method> <path> <outfile> [curl args...] → http code
    local m="$1" p="$2" out="$3"; shift 3
    curl -s -m 30 -o "$out" -D "$out.h" -w '%{http_code}' -X "$m" "$URL$p" \
        -H "Content-Type: application/json" "$@" --data '{"q":1}'
}
get() { local p="$1" out="$2"; shift 2
    curl -s -m 30 -o "$out" -D "$out.h" -w '%{http_code}' "$URL$p" "$@"; }

field() { python3 -c "
import json
try: v = json.load(open('$1'))
except Exception: print('NOT_JSON'); raise SystemExit
for k in '$2'.split('.'): v = v.get(k) if isinstance(v, dict) else None
print('MISSING' if v is None else v)"; }
header() { grep -i "^$2:" "$1.h" | head -1 | cut -d: -f2- | tr -d ' \r'; }

O="$WORK/o"
OWN="job_dev_0123456789abcdef0123456789abcdef"
PRODJOB="job_prod_0123456789abcdef0123456789abcdef"
PRODJOB2="job_prod_fedcba9876543210fedcba9876543210"

echo "############ PRIMARY, committed include (PRIMARY_PEER_HOP=0) ############"
grep -q 'map "$bridge_hopped:$http_x_priority:$http_x_vision_provider" $llm_backend_pool' "$D/upstreams-primary.conf" \
    || { echo "ABORT: committed upstreams-primary.conf is not the PRIMARY_PEER_HOP=0 variant"; exit 2; }
start_stack "$D/upstreams-primary.conf"

echo "=== 1. dev/staging traffic without X-Priority stays on the dev workers ==="
c=$(req POST /v1/chat/completions "$O" -H "X-App-ID: werking-energy"); check "chat: status" "$c" = 200
check "chat: answered by a dev worker" "$(field "$O" worker)" != backup-host
c=$(req POST /v1/research "$O"); check "research: answered by a dev worker" "$(field "$O" worker)" != backup-host
c=$(req POST /v1/jobs "$O"); check "POST /v1/jobs (no key): dev worker" "$(field "$O" worker)" != backup-host
check "POST /v1/jobs (no key): status" "$c" = 200
c=$(req POST /v1/jobs "$O" -H "Idempotency-Key: K-DEV-1"); check "POST /v1/jobs keyed: created" "$c" = 201
check "keyed: local pool" "$(header "$O" X-Backend-Pool)" = claude_jobs_keyed_local
first=$(field "$O" job_id)
c=$(req POST /v1/jobs "$O" -H "Idempotency-Key: K-DEV-1"); check "keyed retry: replay at the same (dev) bridge" "$c" = 200
check "keyed retry: same job" "$(field "$O" job_id)" = "$first"
c=$(req POST /v1/jobs/lookup "$O" -H "Idempotency-Key: K-DEV-1"); check "lookup keyed: dev worker" "$(field "$O" worker)" != backup-host
c=$(get /v1/jobs "$O"); check "GET /v1/jobs (list): dev worker" "$(field "$O" worker)" != backup-host
c=$(get "/v1/jobs/$OWN" "$O"); check "poll own job (dev marker): dev worker" "$(field "$O" worker)" != backup-host
check "poll own job: pool" "$(header "$O" X-Backend-Pool)" = claude_jobs_home
c=$(get /v1/jobs/legacy-unmarked-id "$O"); check "poll unmarked id: dev worker (was: peer)" "$(field "$O" worker)" != backup-host
c=$(req POST /v1/chat/completions "$O" -H "X-Vision-Provider: gemini"); check "gemini image path: dev worker" "$(field "$O" worker)" != backup-host
check "NOTHING of the above reached the prod bridge" "$(all_peer_hits)" -eq 0
check "access log: chat rode llm_pool claude_workers" \
    "$(docker exec "dh-nginx-$SUFFIX" sh -c 'grep "/v1/chat/completions" /var/log/nginx/access.jsonl | head -1' | python3 -c 'import json,sys; print(json.load(sys.stdin)["llm_pool"])')" = claude_workers

echo "=== 2. jobs in flight with prod home keep following their home ==="
c=$(get "/v1/jobs/$PRODJOB" "$O"); check "poll prod-home job: forwarded to the prod bridge" "$(field "$O" worker)" = backup-host
check "poll prod-home job: pool" "$(header "$O" X-Backend-Pool)" = claude_jobs_peer
c=$(curl -s -m 30 -o "$O" -w '%{http_code}' -X DELETE "$URL/v1/jobs/$PRODJOB2"); check "cancel prod-home job: forwarded to the prod bridge" "$(field "$O" worker)" = backup-host
check "cancel prod-home job: exactly one DELETE at the peer" "$(hits "$PRODJOB2" backup-host)" -eq 1
check "cancel prod-home job: none on dev workers" "$(hits "$PRODJOB2" $LOCAL)" -eq 0

echo "=== 3. X-Priority: production (real prod-priority traffic) — deliberately unchanged ==="
before=$(all_peer_hits)
c=$(req POST /v1/chat/completions "$O" -H "X-Priority: production"); check "X-Priority chat: prod bridge first" "$(field "$O" worker)" = backup-host
c=$(req POST /v1/jobs "$O" -H "X-Priority: production"); check "X-Priority job: prod bridge first" "$(field "$O" worker)" = backup-host
c=$(req POST /v1/jobs "$O" -H "X-Priority: production" -H "Idempotency-Key: K-PRIO-1")
check "X-Priority keyed job: fixed bridge = prod" "$(header "$O" X-Backend-Pool)" = claude_jobs_keyed_peer
check "X-Priority keyed job: no dev POST" "$(hits K-PRIO-1 $LOCAL)" -eq 0
c=$(req POST /v1/chat/completions "$O" -H "X-Priority: production" -H "X-Vision-Provider: gemini")
check "X-Priority + gemini: stays local (no Gemini on prod)" "$(field "$O" worker)" != backup-host
c=$(req POST /v1/chat/completions "$O" -H "X-Priority: production" -H "X-Stub-Peer: workers429")
check "X-Priority, prod bridge 429 → dev workers as backup (unchanged)" "$(field "$O" worker)" != backup-host
check "peer saw exactly the four X-Priority requests" "$(( $(all_peer_hits) - before ))" -eq 4

echo "############ ROLLBACK variant (PRIMARY_PEER_HOP=1) ############"
mkdir -p "$WORK/gen"
PRIMARY_PEER_HOP=1 UPSTREAMS_OUT_DIR="$WORK/gen" "$REPO/scripts/generate-bridge-upstreams.sh" primary >/dev/null
start_stack "$WORK/gen/upstreams-primary.conf"
c=$(req POST /v1/chat/completions "$O"); check "rollback: chat without X-Priority → prod bridge first again" "$(field "$O" worker)" = backup-host
c=$(req POST /v1/jobs "$O" -H "Idempotency-Key: K-RB-1"); check "rollback: keyed job → prod bridge again" "$(field "$O" worker)" = backup-host
c=$(get /v1/jobs/legacy-unmarked-id "$O"); check "rollback: unmarked poll → prod bridge first" "$(field "$O" worker)" = backup-host

echo
if [ "$FAILURES" -eq 0 ]; then echo "ALL PASS"; else echo "$FAILURES FAILURE(S)"; docker logs "dh-nginx-$SUFFIX" 2>&1 | grep -i "emerg\|error" | tail -8; fi
exit $(( FAILURES > 0 ))
