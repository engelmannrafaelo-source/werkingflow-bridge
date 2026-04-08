#!/bin/bash
# Graceful rebuild script — rebuilds services one-by-one without dropping active requests.
#
# Workers (worker1-4): nginx load-balances via proxy_next_upstream, so we rebuild
# one at a time while the others keep serving.
# Privacy-service: single instance, wait until idle before rebuild.
# Nginx: reload config only (no rebuild needed), done last.

set -euo pipefail

cd "$(dirname "$0")"

# Config
IDLE_TIMEOUT=600        # 10 minutes max wait for service to become idle
HEALTH_TIMEOUT=120      # 2 minutes max wait for service to become healthy
POLL_INTERVAL=5         # seconds between ready checks
WORKERS="worker1 worker2 worker3 worker4"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log() { echo -e "${BLUE}[$(date '+%H:%M:%S')]${NC} $1"; }
ok()  { echo -e "${GREEN}[$(date '+%H:%M:%S')] ✅ $1${NC}"; }
warn(){ echo -e "${YELLOW}[$(date '+%H:%M:%S')] ⚠️  $1${NC}"; }
err() { echo -e "${RED}[$(date '+%H:%M:%S')] ❌ $1${NC}"; }

# Get container name for a service
container_name() {
    docker compose ps --format '{{.Name}}' "$1" 2>/dev/null | head -1
}

# Wait for service to have 0 active requests via /ready endpoint
wait_for_idle() {
    local service="$1"
    local port="$2"
    local container
    container=$(container_name "$service")

    if [ -z "$container" ]; then
        warn "$service not running, skipping idle check"
        return 0
    fi

    log "Waiting for $service to become idle..."
    local elapsed=0

    while [ $elapsed -lt $IDLE_TIMEOUT ]; do
        local ready_json
        ready_json=$(docker exec "$container" curl -sf "http://localhost:${port}/ready" 2>/dev/null || echo "")

        if [ -z "$ready_json" ]; then
            warn "$service /ready not reachable, proceeding anyway"
            return 0
        fi

        local active
        active=$(echo "$ready_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('active_requests',0))" 2>/dev/null || echo "0")

        if [ "$active" = "0" ]; then
            ok "$service is idle (0 active requests)"
            return 0
        fi

        log "$service has $active active request(s), waiting ${POLL_INTERVAL}s..."
        sleep $POLL_INTERVAL
        elapsed=$((elapsed + POLL_INTERVAL))
    done

    warn "$service still has active requests after ${IDLE_TIMEOUT}s — rebuilding anyway (safety timeout)"
    return 0
}

# Wait for container health check to pass
wait_for_healthy() {
    local service="$1"
    log "Waiting for $service to become healthy..."

    local elapsed=0
    while [ $elapsed -lt $HEALTH_TIMEOUT ]; do
        local health
        health=$(docker inspect --format='{{.State.Health.Status}}' "$(container_name "$service")" 2>/dev/null || echo "starting")

        if [ "$health" = "healthy" ]; then
            ok "$service is healthy"
            return 0
        fi

        sleep $POLL_INTERVAL
        elapsed=$((elapsed + POLL_INTERVAL))
    done

    warn "$service did not become healthy within ${HEALTH_TIMEOUT}s"
    return 0
}

# Rebuild a single service
rebuild_service() {
    local service="$1"
    local port="$2"

    echo ""
    log "=== Rebuilding $service ==="

    # 1. Wait for idle
    wait_for_idle "$service" "$port"

    # 2. Build new image
    log "Building $service..."
    docker compose build "$service"

    # 3. Restart only this service (--no-deps avoids pulling in dependencies)
    log "Restarting $service..."
    docker compose up -d --no-deps "$service"

    # 4. Wait for healthy
    wait_for_healthy "$service"
}

# ============================================================
# Main
# ============================================================

echo ""
echo "=================================================="
echo "  Graceful Bridge Rebuild"
echo "  $(date)"
echo "=================================================="
echo ""

# Phase 1: Rebuild workers one by one (nginx routes to healthy ones)
for worker in $WORKERS; do
    rebuild_service "$worker" 8000
done

# Phase 2: Rebuild privacy-service (single instance, must wait for idle)
rebuild_service "privacy-service" 8100

# Phase 3: Reload nginx config (no rebuild needed, just reload)
echo ""
log "=== Reloading nginx ==="
docker compose exec nginx nginx -s reload 2>/dev/null && ok "nginx config reloaded" || {
    warn "nginx reload failed, rebuilding..."
    docker compose up -d --no-deps nginx
    wait_for_healthy "nginx"
}

# Phase 4: Cleanup
echo ""
log "Cleaning up build cache..."
docker builder prune -f

echo ""
ok "Graceful rebuild complete!"
echo ""
df -h /
