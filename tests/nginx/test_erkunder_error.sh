#!/usr/bin/env bash
# Isolated nginx, no published ports, live services or external requests.
set -euo pipefail
cd "$(dirname "$0")/../.."
image=${1:-openresty/openresty:1.27.1.1-alpine}
dir=$(mktemp -d "${TMPDIR:?}/b1n-nginx.XXXXXX")
name="b1n-nginx-${RANDOM}-$$"
trap 'docker rm -f "$name" >/dev/null 2>&1 || true; rm -rf "$dir"' EXIT
python3 - "$dir" "${NGINX_SOURCE:-docker/nginx.conf}" <<'PY'
import sys
from pathlib import Path
text = Path(sys.argv[2]).read_text()
start = text.find('        location ^~ /v1/erkunder/ {')
route = text[start:text.index('\n        }', start) + len('\n        }')] if start >= 0 else ''
Path(sys.argv[1], 'nginx.conf').write_text('''
events {}
http {
    upstream claude_workers { server unix:/tmp/worker.sock; }
    server {
        listen unix:/tmp/worker.sock;
        location / {
            default_type application/json;
            return 502 '{"detail":"Erkunder-Leitstand nicht erreichbar"}';
        }
    }
    server {
        listen 80;
        set $bridge_origin_out test;
        proxy_intercept_errors on;
        error_page 500 502 503 504 = @bridge_full;
        location = /probe {
            content_by_lua_block {
                local target = ngx.var.arg_target or "/v1/erkunder/bericht/test-report/ergebnis"
                local sock = ngx.socket.tcp()
                assert(sock:connect("127.0.0.1", 80))
                assert(sock:send("GET " .. target .. " HTTP/1.0\\r\\nHost: test\\r\\n\\r\\n"))
                ngx.print(assert(sock:receive("*a")))
            }
        }
        location @bridge_full { return 503 'at capacity'; }
        location / { proxy_pass http://claude_workers; }
''' + route + '\n    }\n}\n')
PY
docker run -d --name "$name" --network none --memory 128m --pids-limit 64 \
    -v "$dir/nginx.conf:/usr/local/openresty/nginx/conf/nginx.conf:ro" "$image" >/dev/null
# Wait for readiness, not for a particular error response.
for attempt in {1..30}; do
    if docker exec "$name" test -S /tmp/worker.sock; then break; fi
    sleep 0.1
done
out=$(docker exec "$name" wget -q -O - http://127.0.0.1/probe)
grep -q '502 Bad Gateway' <<< "$out"
grep -q 'Erkunder-Leitstand nicht erreichbar' <<< "$out"
if grep -q 'at capacity' <<< "$out"; then exit 1; fi
echo 'PASS: actual Erkunder location preserves upstream 502 and Leitstand error body'
out=$(docker exec "$name" wget -q -O - 'http://127.0.0.1/probe?target=/ordinary')
grep -q '503 Service Temporarily Unavailable' <<< "$out"
grep -q 'at capacity' <<< "$out"
echo 'PASS: ordinary capacity mapping unchanged'
