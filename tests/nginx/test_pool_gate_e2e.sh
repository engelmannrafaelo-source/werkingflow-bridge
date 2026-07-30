#!/usr/bin/env bash
# End-to-end test of the nginx pool-capacity gate.
#
# WHY THIS EXISTS
# ---------------
# The gate's behaviour depends on nginx PHASE ORDER, which no unit test and no
# `nginx -t` can check. From its introduction until 2026-07-30 the reject was
# written as:
#
#     set $target_worker "pending";
#     access_by_lua_block { require("pool_router").choose() }   # access phase
#     if ($target_worker = "unavailable") { return 419; }       # REWRITE phase
#
# `if` belongs to ngx_http_rewrite_module and therefore runs BEFORE the access
# phase — it only ever compared the "pending" sentinel, so it never fired. The
# config read as if a fully exhausted pool was rejected instantly; in reality
# every request still hammered all four exhausted workers and the 429 came from
# @bridge_full afterwards. `nginx -t` was happy the whole time.
#
# This harness runs the REAL nginx.conf + pool_router.lua against stub workers
# and a stub metrics-reader, so the gate is asserted on behaviour.
#
# USAGE
#     tests/nginx/test_pool_gate_e2e.sh              # lint + both scenarios
#     tests/nginx/test_pool_gate_e2e.sh --lint-only  # static check only, no docker
# Requires docker (except --lint-only). Builds the real docker/Dockerfile.nginx-lb.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
D="$REPO/docker"
NET=pool-gate-e2e
IMG=pool-gate-e2e:local
PY=python:3.12-alpine
WORK="$(mktemp -d)"
FAILURES=0

cleanup() {
    docker rm -f pg-nginx pg-echo pg-metrics >/dev/null 2>&1
    docker network rm $NET >/dev/null 2>&1
}
trap 'cleanup; rm -rf "$WORK"' EXIT

# ---------------------------------------------------------------------------
# Step 0 — static phase-order lint (no docker; the class-level guard)
#
# Hard-fails when a rewrite-phase directive READS a variable that Lua assigns in
# the access phase. That combination is always a no-op, and it is invisible to
# `nginx -t`: it cost this codebase a gate that silently never fired.
# ---------------------------------------------------------------------------
lint_phase_order() {
    python3 - "$REPO" <<'PYEOF'
import glob
import os
import re
import sys

repo = sys.argv[1]
lua_files = glob.glob(os.path.join(repo, "docker", "lua", "*.lua"))
# dict.fromkeys keeps order and de-duplicates: nginx.conf is also matched by the
# glob, and a finding reported twice reads like two findings.
conf_files = list(dict.fromkeys(
    [os.path.join(repo, "docker", "nginx.conf")]
    + sorted(glob.glob(os.path.join(repo, "docker", "*.conf")))))

# Variables assigned by Lua (access/content phase).
lua_vars = set()
for path in lua_files:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            for m in re.finditer(r"ngx\.var\.([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)", line):
                lua_vars.add(m.group(1))

if not lua_vars:
    print("  lint: no Lua-assigned nginx variables found — nothing to check")
    sys.exit(0)
print("  lint: Lua-assigned variables: " + ", ".join(sorted(lua_vars)))

REWRITE_PHASE = re.compile(r"^\s*(if|return|rewrite|set)\b")
SET_TARGET = re.compile(r"^\s*set\s+\$([A-Za-z_][A-Za-z0-9_]*)")

errors, warnings = [], []
for path in conf_files:
    if not os.path.exists(path):
        continue
    rel = os.path.relpath(path, repo)
    with open(path, encoding="utf-8") as fh:
        for n, raw in enumerate(fh, 1):
            line = raw.split("#", 1)[0]          # strip comments
            if not line.strip():
                continue

            if REWRITE_PHASE.match(line):
                # `set $X <value>` WRITES $X — that declaration is required.
                written = SET_TARGET.match(line)
                written_var = written.group(1) if written else None
                for var in lua_vars:
                    if var == written_var:
                        continue
                    if re.search(r"\$" + var + r"\b", line):
                        errors.append(
                            f"{rel}:{n}: rewrite-phase directive reads ${var}, "
                            f"which Lua assigns in the access phase — this can "
                            f"never see the Lua value: {line.strip()}")

            m = re.match(r"\s*map\s+\$([A-Za-z_][A-Za-z0-9_]*)\s+\$", line)
            if m and m.group(1) in lua_vars:
                warnings.append(
                    f"{rel}:{n}: map keyed on Lua-assigned ${m.group(1)} — safe "
                    f"only if its output is read after the access phase; verify: "
                    f"{line.strip()}")

for w in warnings:
    print(f"  lint WARN  {w}")
for e in errors:
    print(f"  lint FAIL  {e}")
if errors:
    sys.exit(1)
print(f"  lint: OK — no rewrite-phase reads of Lua-assigned variables "
      f"({len(warnings)} warning(s))")
PYEOF
}

echo "=== Step 0: static phase-order lint ==="
if ! lint_phase_order; then
    echo "POOL_GATE_E2E_FAIL: phase-order lint failed"
    exit 1
fi

if [ "${1:-}" = "--lint-only" ]; then
    echo "POOL_GATE_LINT_OK (lint-only mode; scenarios skipped)"
    exit 0
fi
echo

# --- stub worker: echoes the headers nginx forwarded ------------------------
cat > "$WORK/echo.py" <<'PYEOF'
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class H(BaseHTTPRequestHandler):
    def _h(self):
        try:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
        except Exception:
            pass
        body = json.dumps({"headers": {k.lower(): v for k, v in self.headers.items()}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _h

    def log_message(self, *a):
        pass


HTTPServer(("0.0.0.0", 8000), H).serve_forever()
PYEOF

# --- stub metrics-reader: the pool state pool_router.lua consumes -----------
cat > "$WORK/metrics.py" <<'PYEOF'
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

MODE = os.getenv("MODE", "exhausted")
ACC = {n: {"worker": w, "available": False, "cooldown_remaining_s": 900,
           "effective_cap_tokens": 0, "current_in_flight_tokens": 0, "weekly_percent": 99.0}
       for n, w in [("engelmann", "worker1"), ("office", "worker2"),
                    ("gmail", "worker3"), ("werking", "worker4")]}
if MODE == "healthy":
    ACC["werking"] = {"worker": "worker4", "available": True, "cooldown_remaining_s": 0,
                      "effective_cap_tokens": 400000, "current_in_flight_tokens": 0,
                      "weekly_percent": 20.0}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ts": 0, "accounts": ACC}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


HTTPServer(("0.0.0.0", 8000), H).serve_forever()
PYEOF

echo "Building $IMG from docker/Dockerfile.nginx-lb ..."
docker build -q -f "$D/Dockerfile.nginx-lb" -t $IMG "$REPO" >/dev/null || {
    echo "FAIL: image build"; exit 1; }

start_stack() { # $1 = exhausted|healthy
    cleanup
    docker network create $NET >/dev/null
    # Same envsubst the compose performs before nginx loads the config.
    sed 's/${BRIDGE_ID}/test/g; s/${BRIDGE_BACKUP_HOST}/backup-host/g' "$D/nginx.conf" > "$WORK/nginx.conf"
    sed 's/${BRIDGE_BACKUP_HOST}/backup-host/g' "$D/upstreams-primary.conf" > "$WORK/upstreams.conf"
    cp "$D/routes-metrics-reader.conf" "$D/routes-platform-api.conf" "$WORK/"

    docker run -d --name pg-echo --network $NET \
        --network-alias worker1 --network-alias worker2 --network-alias worker3 \
        --network-alias worker4 --network-alias backup-host \
        --network-alias platform-api --network-alias privacy-pdf \
        -v "$WORK/echo.py:/echo.py:ro" $PY python /echo.py >/dev/null
    docker run -d --name pg-metrics --network $NET --network-alias metrics-reader \
        -e MODE="$1" -v "$WORK/metrics.py:/metrics.py:ro" $PY python /metrics.py >/dev/null
    docker run -d --name pg-nginx --network $NET -p 18080:80 --tmpfs /var/log/nginx \
        -e BRIDGE_WORKERS="worker1,worker2,worker3,worker4" \
        -v "$WORK/nginx.conf:/usr/local/openresty/nginx/conf/nginx.conf:ro" \
        -v "$WORK/upstreams.conf:/tmp/upstreams.conf:ro" \
        -v "$WORK/routes-metrics-reader.conf:/etc/nginx/routes-metrics-reader.conf:ro" \
        -v "$WORK/routes-platform-api.conf:/etc/nginx/routes-platform-api.conf:ro" \
        $IMG >/dev/null
    for _ in $(seq 60); do
        curl -sf -o /dev/null http://127.0.0.1:18080/health && break; sleep 0.25
    done
    sleep 3   # let the router's 2s refresh timer pull the pool state
}

# assert <label> <path> <expected-http> <expected-marker> [extra-header]
assert() {
    local label="$1" path="$2" want_code="$3" want_marker="$4" hdr="${5:-}"
    local out code body marker
    if [ -n "$hdr" ]; then
        out=$(curl -s -w '\n%{http_code}' -XPOST "http://127.0.0.1:18080$path" -H "$hdr" -d '{"q":1}')
    else
        out=$(curl -s -w '\n%{http_code}' -XPOST "http://127.0.0.1:18080$path" -d '{"q":1}')
    fi
    code=$(printf '%s' "$out" | tail -1)
    body=$(printf '%s' "$out" | sed '$d')
    marker=$(printf '%s' "$body" | python3 -c "
import sys, json
try: print(json.load(sys.stdin)['headers'].get('x-pool-exhausted', 'absent'))
except Exception: print('n/a')" 2>/dev/null)
    if [ "$code" = "$want_code" ] && [ "$marker" = "$want_marker" ]; then
        printf '  PASS  %-52s HTTP %s marker=%s\n' "$label" "$code" "$marker"
    else
        printf '  FAIL  %-52s HTTP %s (want %s) marker=%s (want %s)\n' \
            "$label" "$code" "$want_code" "$marker" "$want_marker"
        FAILURES=$((FAILURES + 1))
    fi
}

echo
echo "=== Scenario: pool EXHAUSTED (no eligible account) ==="
start_stack exhausted
# The regression this file exists for: before the access-phase fix this was 200.
assert "chat: hard reject (no alternative path)"        /v1/chat/completions 429 n/a
# Carve-out: research/jobs must still reach a worker, marked, so the app can
# choose the research-cloud path — whose trigger IS pool saturation.
assert "research: routed + marked (overflow-capable)"   /v1/research         200 1
assert "jobs: routed + marked (overflow-capable)"       /v1/jobs             200 1
# The reject must be the canonical envelope, not a generic 5xx.
env_body=$(curl -s -XPOST http://127.0.0.1:18080/v1/chat/completions -d '{"q":1}')
if printf '%s' "$env_body" | grep -q '"bridge_type":"pool_exhausted"' \
   && printf '%s' "$env_body" | grep -q '"reason":"all_pool_exhausted"'; then
    echo "  PASS  chat reject uses the @pool_exhausted_response envelope"
else
    echo "  FAIL  chat reject envelope wrong: $env_body"; FAILURES=$((FAILURES + 1))
fi

echo
echo "=== Scenario: pool HEALTHY (one eligible account) ==="
start_stack healthy
assert "chat: served"                                   /v1/chat/completions 200 absent
assert "research: served, NOT marked"                   /v1/research         200 absent
assert "jobs: served, NOT marked"                       /v1/jobs             200 absent
# X-Pool-Exhausted is infrastructure-only: a client copy must never reach a worker.
assert "forged marker stripped (research)"              /v1/research         200 absent "X-Pool-Exhausted: 1"
assert "forged marker stripped (chat)"                  /v1/chat/completions 200 absent "X-Pool-Exhausted: 1"

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "POOL_GATE_E2E_OK: all assertions passed"
    exit 0
fi
echo "POOL_GATE_E2E_FAIL: $FAILURES assertion(s) failed"
exit 1
