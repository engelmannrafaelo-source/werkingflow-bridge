#!/usr/bin/env bash
# Post-deploy verification that the pool-capacity gate actually LANDED.
#
# WHY: nginx.conf is bind-mounted, docker/lua/ is baked into the image. A
# recreate without --build therefore takes the new config with the OLD Lua —
# and that mix is worse than either alone: the new access-phase guard fires
# while the old choose() ignores overflow_capable, so /v1/research gets hard
# rejected and the research-cloud overflow is unreachable again. bridge-deploy.sh
# builds nginx (it is in *_NEEDS_BUILD), but a hand-rolled `docker compose up -d`
# does not. This script proves BOTH halves are live.
#
# Usage: verify_deployed_gate.sh <host> <lb-container> <smoke-profile> [marker-sha]
#   tests/nginx/verify_deployed_gate.sh 49.12.72.66  wt-wrapper-lb hetzner
#   tests/nginx/verify_deployed_gate.sh 178.104.178.79 wt-prod-lb   server2
set -uo pipefail
HOST="$1"; LB="$2"; PROFILE="$3"; MARKER="${4:-9116da0}"
FAIL=0
ok()   { printf '  PASS  %s\n' "$1"; }
bad()  { printf '  FAIL  %s\n' "$1"; FAIL=$((FAIL+1)); }
rs()   { sudo -n ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@$HOST" "$1" 2>/dev/null; }

echo "=== $HOST ($LB) ==="

# 1. Checkout CONTAINS the commit that matters for the running bridge.
#    Not an equality check: later commits may touch only deploy-machine tooling
#    (493dc59 changed scripts/bridge-deploy.sh alone, which runs on the deploy machine), and pinning an exact SHA
#    would report that as a deploy defect.
sha=$(rs "cd /root/werkingflow-bridge && git rev-parse --short HEAD")
if rs "cd /root/werkingflow-bridge && git merge-base --is-ancestor $MARKER HEAD"; then
    ok "checkout at $sha (contains $MARKER)"
else
    bad "checkout at '$sha' does NOT contain $MARKER"
fi

# 2. The MOUNTED config inside the running container carries the new directives.
for needle in "pool_overflow_capable" "more_clear_input_headers" "ngx.exec(\"@pool_exhausted_response\")"; do
    if rs "docker exec $LB grep -qF '$needle' /etc/nginx/nginx.conf" ; then
        ok "nginx.conf in container has: $needle"
    else
        bad "nginx.conf in container MISSING: $needle"
    fi
done

# 3. The BAKED Lua inside the image carries the carve-out. This is the half that
#    a plain `docker compose up -d` (without --build) would silently leave stale.
for needle in "all_unavail_overflow" "overflow_capable"; do
    if rs "docker exec $LB grep -qF '$needle' /etc/nginx/lua/pool_router.lua"; then
        ok "baked pool_router.lua has: $needle"
    else
        bad "baked pool_router.lua MISSING: $needle  <-- image not rebuilt!"
    fi
done

# 4. The dead config is really gone from the running container.
#    Comment lines must be excluded: the removal itself left an explanatory
#    comment that names `claude_unavail`, and a naive grep reads that as the
#    directive still being there (it did, on the first run).
if rs "docker exec $LB sh -c \"cat /etc/nginx/nginx.conf /tmp/upstreams.conf 2>/dev/null | grep -v '^[[:space:]]*#' | grep -q 'claude_unavail\|target_dest'\""; then
    bad "claude_unavail / target_dest still present as a real directive"
else
    ok "dead claude_unavail / target_dest map gone (only the removal comment remains)"
fi

# 5. Config actually loaded (nginx would not serve otherwise) + routing alive.
lb=$(curl -s --max-time 10 "http://$HOST:8000/lb-status")
if printf '%s' "$lb" | grep -q '"status":"healthy"'; then
    ok "lb-status healthy: $(printf '%s' "$lb" | grep -o '"up":[0-9]*' | head -1)"
else
    bad "lb-status not healthy: $(printf '%s' "$lb" | head -c 200)"
fi

# 6. Pool router still deciding (proves access_by_lua_block runs).
st=$(rs "docker exec $LB curl -s http://127.0.0.1/internal/pool-router/state")
if printf '%s' "$st" | grep -q '"last_refresh_status":"ok"'; then
    ok "pool_router state refresh ok"
else
    bad "pool_router state: $(printf '%s' "$st" | head -c 200)"
fi

echo
echo "=== functional smoke ($PROFILE) ==="
cd /root/projekte/werkingflow-bridge || exit 1
smoke=$(python3 scripts/bridge_smoke.py --base-url "http://$HOST:8000" --profile "$PROFILE" --attempts 3 2>&1)
printf '%s\n' "$smoke" | sed 's/^/  /'
if printf '%s' "$smoke" | grep -qE '^SMOKE_OK|^SMOKE_CAPACITY'; then
    ok "smoke acceptable"
else
    bad "smoke failed"
fi

echo
[ "$FAIL" -eq 0 ] && echo "VERIFY_OK ($HOST)" || echo "VERIFY_FAIL ($HOST): $FAIL check(s)"
exit "$FAIL"
