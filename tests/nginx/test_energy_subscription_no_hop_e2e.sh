#!/usr/bin/env bash
# ENERGY_SUBSCRIPTION_NO_HOP (Energy-Abo-Modus, KD 03.11.2026) — end to end
# against a real OpenResty with the REAL shared docker/nginx.conf, the REAL
# start script and the generated upstreams include (committed default and the
# variants ENERGY_SUBSCRIPTION_NO_HOP=1 with PRIMARY_PEER_HOP=1 and 0).
#
# What it proves (contract: docs/contracts/energy-abo-bildweg.md §3):
#   * switch on: werking-energy never reaches the peer (= prod bridge) —
#     chat, research, POST /v1/jobs (with/without key), and location /
#     (/v1/document/convert, /v1/auth/status), also with X-Priority: production;
#   * switch on: every other caller routes exactly as without it;
#   * committed default: Energy hops as ADR-0010 says (switch off = unchanged).
# Harness (stubs, start_stack, helpers) copied from test_dev_no_prod_hop_e2e.sh.
# Stub workers and a stub peer only — no network outside the docker test net.
# USAGE  tests/nginx/test_energy_subscription_no_hop_e2e.sh      (needs docker)
# USAGE  tests/nginx/test_dev_no_prod_hop_e2e.sh      (needs docker)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
D="$REPO/docker"
SUFFIX="$$"
NET="energy-no-hop-e2e-$SUFFIX"
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
    for s in $STUBS nginx; do docker rm -f "eh-$s-$SUFFIX" >/dev/null 2>&1; done
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
        docker run -d --name "eh-$s-$SUFFIX" --network "$NET" --network-alias "$s" \
            --memory 64m -e NAME="$s" -v "$WORK/db:/db" \
            -v "$WORK/stub.py:/stub.py:ro" $PY python /stub.py >/dev/null
    done
    # BRIDGE_ID stays a ${VAR} until the REAL start script envsubst's it.
    docker run -d --name "eh-nginx-$SUFFIX" --network "$NET" -p "127.0.0.1:$PORT:80" \
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
    echo "FAIL: nginx did not come up"; docker logs "eh-nginx-$SUFFIX" 2>&1 | tail -5
    exit 1
}

hits() { # <path-substring> <stubs...> → number of recorded requests whose path contains it
    local needle="$1"; shift
    local total=0 n
    for s in "$@"; do
        n=$(docker exec "eh-$s-$SUFFIX" python -c "
import json, urllib.request
h = json.load(urllib.request.urlopen('http://127.0.0.1:8000/__hits'))
print(sum(1 for x in h if '$needle' in (x['path'] + '|' + str(x['key']))))" 2>/dev/null || echo 0)
        total=$((total + n))
    done
    echo "$total"
}

all_peer_hits() {
    docker exec "eh-backup-host-$SUFFIX" python -c "
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
E="X-App-ID: werking-energy"
P="X-Priority: production"

energy_local_checks() { # <label>
    local L="$1" before K
    K="K-EN-${L//\//-}"   # the stub stores keyed jobs as files: no "/" in the key
    before=$(all_peer_hits)
    c=$(req POST /v1/chat/completions "$O" -H "$E"); check "$L energy chat: dev worker" "$(field "$O" worker)" != backup-host
    c=$(req POST /v1/chat/completions "$O" -H "$E" -H "$P"); check "$L energy chat + X-Priority: dev worker" "$(field "$O" worker)" != backup-host
    c=$(req POST /v1/research "$O" -H "$E"); check "$L energy research: dev worker" "$(field "$O" worker)" != backup-host
    c=$(req POST /v1/jobs "$O" -H "$E" -H "$P"); check "$L energy job + X-Priority: dev worker" "$(field "$O" worker)" != backup-host
    c=$(req POST /v1/jobs "$O" -H "$E" -H "$P" -H "Idempotency-Key: $K"); check "$L energy keyed job + X-Priority: created" "$c" = 201
    check "$L energy keyed job: local pool" "$(header "$O" X-Backend-Pool)" = claude_jobs_keyed_local
    check "$L energy keyed job: no POST at the prod bridge" "$(hits "$K" backup-host)" -eq 0
    c=$(req POST /v1/document/convert "$O" -H "$E" -H "$P"); check "$L energy document/convert + X-Priority: status" "$c" = 200
    check "$L energy document/convert + X-Priority: dev worker" "$(field "$O" worker)" != backup-host
    c=$(req POST /v1/document/convert "$O" -H "X-App-ID: Werking-Energy" -H "$P"); check "$L energy (case) document/convert: dev worker" "$(field "$O" worker)" != backup-host
    c=$(req POST /v1/privacy/smart-anonymize "$O" -H "$E" -H "$P"); check "$L energy other location-/ path + X-Priority: dev worker" "$(field "$O" worker)" != backup-host
    c=$(get /v1/auth/status "$O" -H "$E" -H "$P"); check "$L energy /v1/auth/status + X-Priority: dev worker" "$(field "$O" worker)" != backup-host
    check "$L energy: nothing reached the prod bridge" "$(( $(all_peer_hits) - before ))" -eq 0
}

others_checks() { # <label> <expect chat-without-priority at peer: yes|no>
    local L="$1"
    c=$(req POST /v1/document/convert "$O" -H "X-App-ID: werking-report" -H "$P"); check "$L report document/convert + X-Priority: prod bridge" "$(field "$O" worker)" = backup-host
    c=$(req POST /v1/document/convert "$O" -H "X-App-ID: werking-report"); check "$L report document/convert: dev worker" "$(field "$O" worker)" != backup-host
    c=$(req POST /v1/document/convert "$O" -H "X-App-ID: werking-energy-x" -H "$P"); check "$L werking-energy-x + X-Priority: prod bridge" "$(field "$O" worker)" = backup-host
    c=$(req POST /v1/chat/completions "$O" -H "X-App-ID: werking-report" -H "$P"); check "$L report chat + X-Priority: prod bridge" "$(field "$O" worker)" = backup-host
    c=$(req POST /v1/document/convert "$O" -H "$E" -H "$P" -H "X-Vision-Provider: gemini"); check "$L energy+gemini+X-Priority on location /: as before (prod bridge)" "$(field "$O" worker)" = backup-host
    if [ "$2" = yes ]; then
        c=$(req POST /v1/chat/completions "$O" -H "X-App-ID: werking-report"); check "$L report chat: prod bridge first (ADR-0010)" "$(field "$O" worker)" = backup-host
    else
        c=$(req POST /v1/chat/completions "$O" -H "X-App-ID: werking-report"); check "$L report chat: dev worker (PRIMARY_PEER_HOP=0)" "$(field "$O" worker)" != backup-host
    fi
}

for HOP in 1 0; do
    echo "############ ENERGY_SUBSCRIPTION_NO_HOP=1, PRIMARY_PEER_HOP=$HOP ############"
    mkdir -p "$WORK/gen$HOP"
    ENERGY_SUBSCRIPTION_NO_HOP=1 PRIMARY_PEER_HOP=$HOP UPSTREAMS_OUT_DIR="$WORK/gen$HOP" \
        "$REPO/scripts/generate-bridge-upstreams.sh" primary >/dev/null
    start_stack "$WORK/gen$HOP/upstreams-primary.conf"
    energy_local_checks "on/hop$HOP"
    others_checks "on/hop$HOP" $([ "$HOP" = 1 ] && echo yes || echo no)
done

echo "############ COMMITTED default (switch off, PRIMARY_PEER_HOP=1) ############"
start_stack "$D/upstreams-primary.conf"
c=$(req POST /v1/chat/completions "$O" -H "$E"); check "off: energy chat → prod bridge first (ADR-0010)" "$(field "$O" worker)" = backup-host
c=$(req POST /v1/document/convert "$O" -H "$E" -H "$P"); check "off: energy document/convert + X-Priority → prod bridge (as before)" "$(field "$O" worker)" = backup-host
c=$(req POST /v1/document/convert "$O" -H "$E"); check "off: energy document/convert → dev worker (as before)" "$(field "$O" worker)" != backup-host
others_checks "off" yes

echo
if [ "$FAILURES" -eq 0 ]; then echo "ALL PASS"; else echo "$FAILURES FAILURE(S)"; docker logs "eh-nginx-$SUFFIX" 2>&1 | grep -i "emerg\|error" | tail -8; fi
exit $(( FAILURES > 0 ))
