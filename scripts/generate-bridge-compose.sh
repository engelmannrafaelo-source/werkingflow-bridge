#!/bin/bash
# =============================================================================
# Bridge Compose Generator  —  ⚠️ ASPIRATIONAL / NOT THE LIVE DEPLOY PATH ⚠️
# =============================================================================
# STATUS (verified 2026-07-01, ADR-0006): this generator models the *intended*
# single-source world but does NOT yet match what actually runs on either host.
# NEITHER bridge is deployed from it — bridge-deploy.sh uses the hand-maintained
# docker-compose{,-prod}.yml. Do NOT `mkdir secrets/workers` and run this to
# "fix" it: that path is a trap (see gaps below). Closing these gaps is the
# gated cutover migration in docs/adr/0006-bridge-single-source-centralization.md,
# to be run + diff-verified ON the hosts (secrets live only there).
#
# VERIFIED reality-gaps this generator must close before it can drive a deploy:
#   1. SECRETS: real tokens are FLAT `secrets/claude_token_<name>.txt` (+ account
#      symlinks), NOT `secrets/workers/*.txt`. The worker→token mapping is bespoke
#      per host (primary: worker1..4 → claude_token_account1..4; production:
#      worker-sahori/worker-kurt → claude_token_worker-{sahori,kurt}.txt).
#   2. WORKER NAMING: this script emits `worker-<name>` + upstreams.generated.conf,
#      but the deployed nginx.conf hardcodes `worker1..4` inline and does NOT
#      include the generated upstreams. Reconciling the two is part of the cutover.
#   3. PRIVACY (hardware): primary runs a LOCAL privacy-service (16G box);
#      production has NO local privacy container — its workers use REMOTE
#      PRIVACY_SERVICE_URL=http://100.112.98.39:8100 (dev bridge over Tailscale,
#      because the ~13G model does not fit the 7G prod host). This must become a
#      BRIDGE_ID param (privacy: local | remote(url)); today it emits local for both.
#   4. PLATFORM-API/DB OVERLAY: both hosts layer a separate platform-api + postgres
#      overlay compose (bridge-deploy.sh: `-f base -f *-platform.yml`). Kept as a
#      layered overlay, not emitted here — model it as a BRIDGE_ID-gated overlay.
#   5. BASE IMAGE: prod nginx currently runs plain nginx:alpine (no Lua pool
#      router); this script intends OpenResty for both. Unifying onto OpenResty
#      for prod is a BEHAVIOUR change (Item B) — test + approval required at cutover.
#
# Intended usage (post-cutover):
#   BRIDGE_ID=primary    scripts/generate-bridge-compose.sh
#   BRIDGE_ID=production scripts/generate-bridge-compose.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

WORKERS_DIR="${ROOT}/secrets/workers"
COMPOSE_OUT="${ROOT}/docker/docker-compose.generated.yml"
UPSTREAMS_OUT="${ROOT}/docker/upstreams.generated.conf"

BRIDGE_ID="${BRIDGE_ID:-}"
if [ -z "$BRIDGE_ID" ]; then
  echo "ERROR: BRIDGE_ID env var required (primary|production)" >&2
  exit 1
fi

# Hardware-aware sizing per bridge.
case "$BRIDGE_ID" in
  primary)
    CONTAINER_PREFIX="wt-wrapper"
    NETWORK="bridge-net"
    PRIVACY_WORKERS=4
    DOCLING_THREADS=4
    PRIVACY_MEM_LIMIT=16G
    PRIVACY_MEM_RES=4G
    PRIVACY_START_PERIOD=240s
    METRICS_MEM_LIMIT=1.5G
    NGINX_IMAGE_BUILD=true   # OpenResty + Lua
    ;;
  production)
    CONTAINER_PREFIX="wt-prod"
    NETWORK="bridge-prod-net"
    PRIVACY_WORKERS=2
    DOCLING_THREADS=2
    PRIVACY_MEM_LIMIT=5G
    PRIVACY_MEM_RES=2G
    PRIVACY_START_PERIOD=120s
    METRICS_MEM_LIMIT=512M
    NGINX_IMAGE_BUILD=true
    ;;
  *)
    echo "ERROR: Unknown BRIDGE_ID '$BRIDGE_ID' (allowed: primary, production)" >&2
    exit 1
    ;;
esac

# Find worker token files
if [ ! -d "$WORKERS_DIR" ]; then
  echo "ERROR: $WORKERS_DIR does not exist — and this is EXPECTED (see header)." >&2
  echo "  Real tokens are FLAT at secrets/claude_token_<name>.txt, mapped per host." >&2
  echo "  Do NOT create secrets/workers/ to satisfy this — that produces worker-<name>" >&2
  echo "  services that do not match the deployed worker1..4 / worker-sahori topology." >&2
  echo "  This generator is not yet the deploy path; see ADR-0006 for the cutover plan." >&2
  exit 1
fi

mapfile -t WORKER_FILES < <(find "$WORKERS_DIR" -maxdepth 1 -name "*.txt" -type f | sort)
if [ ${#WORKER_FILES[@]} -eq 0 ]; then
  echo "ERROR: No worker token files found in $WORKERS_DIR/*.txt" >&2
  exit 1
fi

# Extract worker names (file basenames without .txt)
WORKER_NAMES=()
for f in "${WORKER_FILES[@]}"; do
  name=$(basename "$f" .txt)
  # Validate name: alphanumeric + dash + underscore
  if [[ ! "$name" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo "ERROR: Invalid worker name '$name' (file: $f). Use [a-zA-Z0-9_-] only." >&2
    exit 1
  fi
  WORKER_NAMES+=("$name")
done

echo "[generate] BRIDGE_ID=$BRIDGE_ID"
echo "[generate] Workers detected: ${WORKER_NAMES[*]}"
echo "[generate] Hardware: $(nproc) cores, $(free -g | awk '/^Mem:/{print $2"GiB"}') RAM"

# =============================================================================
# Write docker-compose.generated.yml
# =============================================================================
{
cat <<HEADER
# =============================================================================
# AUTO-GENERATED by scripts/generate-bridge-compose.sh — DO NOT EDIT
# Source: secrets/workers/*.txt
# BRIDGE_ID=$BRIDGE_ID
# Regenerate after adding/removing token files.
# =============================================================================

services:
  # ===========================================================================
  # NGINX Load Balancer (OpenResty + Lua pool router)
  # ===========================================================================
  nginx:
    build:
      context: ..
      dockerfile: docker/Dockerfile.nginx-lb
    container_name: ${CONTAINER_PREFIX}-lb
    ports:
      - "8000:80"
HEADER

if [ "$BRIDGE_ID" = "primary" ]; then
cat <<HEADER2
      - "8010:80"
      - "8020:80"
HEADER2
fi

cat <<HEADER3
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./upstreams.generated.conf:/etc/nginx/conf.d/upstreams.conf:ro
      - ../logs/nginx:/var/log/nginx
    environment:
      - BRIDGE_PROD_HOST=\${BRIDGE_PROD_HOST:-127.0.0.1}
      - BRIDGE_PRIMARY_HOST=\${BRIDGE_PRIMARY_HOST:-127.0.0.1}
      - BRIDGE_ID=$BRIDGE_ID
    command: ["/bin/sh", "-c", "envsubst '\$\$BRIDGE_PROD_HOST \$\$BRIDGE_PRIMARY_HOST \$\$BRIDGE_ID' < /etc/nginx/nginx.conf > /tmp/nginx.conf && openresty -c /tmp/nginx.conf -g 'daemon off;'"]
    depends_on:
HEADER3
for name in "${WORKER_NAMES[@]}"; do
  echo "      - worker-$name"
done

cat <<HEADER4
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:80/health"]
      interval: 30s
      timeout: 5s
      retries: 3
    restart: unless-stopped
    networks:
      - $NETWORK

HEADER4

# Workers
i=1
for name in "${WORKER_NAMES[@]}"; do
cat <<WORKER
  # ===========================================================================
  # Worker: $name (token: secrets/workers/$name.txt)
  # ===========================================================================
  worker-$name:
    build:
      context: ..
      dockerfile: docker/Dockerfile.worker
    container_name: ${CONTAINER_PREFIX}-worker-$name
    expose:
      - "8000"
    deploy:
      resources:
        limits:
          memory: 4G
        reservations:
          memory: 1G
    volumes:
      - ${CONTAINER_PREFIX}-logs:/app/logs
      - ${CONTAINER_PREFIX}-instances-shared:/app/instances
      - ~/eco-research-output:/app/research_output
      - /root/.claude/commands/sc:/home/claude/.claude/commands/sc:ro
      - /root/.claude/agents:/home/claude/.claude/agents:ro
      - /root/.claude/superclaude:/home/claude/.claude/superclaude:ro
    secrets:
      - claude_token_$name
    environment:
      - CLAUDE_CODE_OAUTH_TOKEN_FILE=/run/secrets/claude_token_$name
      - TZ=Europe/Vienna
      - LOG_LEVEL=INFO
      - LOG_TO_FILE=true
      - FILTER_SENSITIVE_DATA=true
      - INSTANCE_NAME=worker-$name
      - ADAPTIVE_INITIAL_CAP_TOKENS=1000000
      - ADAPTIVE_FLOOR_TOKENS=500000
      - ADAPTIVE_CEILING_TOKENS=4000000
      - ADAPTIVE_GROW_TRIGGER_SEC=300
      - ADAPTIVE_GROW_FACTOR=1.10
      - ADAPTIVE_QUEUE_WAIT_SEC=5
      - ADAPTIVE_WEEKLY_PREDICTIVE_THROTTLE=true
      - CLAUDE_CWD=/app/instances
      - DISABLE_COACH_MCP=true
      - MAX_TIMEOUT=2400000
      - CLAUDE_PERMISSION_MODE=bypassPermissions
      - ANTHROPIC_VISION_API_KEY=\${ANTHROPIC_API_KEY}
      - OPENAI_API_KEY=\${OPENAI_API_KEY}
      - PRIVACY_ENABLED=true
      - PRIVACY_LANGUAGE=de
      - PRIVACY_LOG_DETECTIONS=false
      - SKIP_SDK_VERIFICATION=true
      - RATE_LIMIT_ENABLED=false
      - DEFAULT_PRIVACY_MODE=none
      - PRIVACY_SERVICE_URL=http://privacy-service:8100
      - AI_BRIDGE_URL_PROD_FALLBACK=\${AI_BRIDGE_URL_PROD_FALLBACK:-}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
    restart: unless-stopped
    networks:
      - $NETWORK

WORKER
  i=$((i+1))
done

# Privacy + Metrics
cat <<TAIL
  # ===========================================================================
  # Privacy + PDF Service (HEAVY — Presidio + spaCy + Docling)
  # ===========================================================================
  privacy-service:
    build:
      context: ..
      dockerfile: docker/Dockerfile.privacy-pdf
    container_name: ${CONTAINER_PREFIX}-privacy
    expose:
      - "8100"
    deploy:
      resources:
        limits:
          memory: $PRIVACY_MEM_LIMIT
        reservations:
          memory: $PRIVACY_MEM_RES
    environment:
      - TZ=Europe/Vienna
      - LOG_LEVEL=INFO
      - PRIVACY_ENABLED=true
      - PRIVACY_LANGUAGE=de
      - PRIVACY_LOG_DETECTIONS=false
      - UVICORN_WORKERS=$PRIVACY_WORKERS
      - DOCLING_THREADS=$DOCLING_THREADS
      - BRIDGE_SELF_URL=http://nginx:80/v1/chat/completions
      - API_KEY=\${API_KEY:-}
      - CONVERTAPI_SECRET=\${CONVERTAPI_SECRET}
      - ANTHROPIC_API_KEY=\${ANTHROPIC_API_KEY:-}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8100/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: $PRIVACY_START_PERIOD
    restart: unless-stopped
    networks:
      - $NETWORK

  # ===========================================================================
  # Metrics Reader (read-only JSONL metrics)
  # ===========================================================================
  metrics-reader:
    build:
      context: ..
      dockerfile: docker/Dockerfile.metrics-reader
    container_name: ${CONTAINER_PREFIX}-metrics-reader
    expose:
      - "8000"
    deploy:
      resources:
        limits:
          memory: $METRICS_MEM_LIMIT
    volumes:
      - ${CONTAINER_PREFIX}-logs:/app/logs:ro
    environment:
      - TZ=Europe/Vienna
      - LOG_LEVEL=INFO
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
    restart: unless-stopped
    networks:
      - $NETWORK

networks:
  $NETWORK:

volumes:
  ${CONTAINER_PREFIX}-logs:
  ${CONTAINER_PREFIX}-instances-shared:

secrets:
TAIL

for name in "${WORKER_NAMES[@]}"; do
cat <<SECRET
  claude_token_$name:
    file: ../secrets/workers/$name.txt
SECRET
done

} > "$COMPOSE_OUT"

# =============================================================================
# Write upstreams.generated.conf (included by nginx.conf)
# =============================================================================
{
  echo "# AUTO-GENERATED — DO NOT EDIT. Source: secrets/workers/*.txt, BRIDGE_ID=$BRIDGE_ID"
  echo
  # Round-robin upstream pool for /health, default routing
  echo "upstream claude_workers {"
  for name in "${WORKER_NAMES[@]}"; do
    echo "    server worker-$name:8000 weight=1 max_fails=0;"
  done
  echo "    keepalive 32;"
  echo "}"
  echo
  # Production fallback pool (same workers — Lua decides priority)
  echo "upstream claude_production {"
  for name in "${WORKER_NAMES[@]}"; do
    echo "    server worker-$name:8000 weight=1 max_fails=0;"
  done
  echo "    keepalive 32;"
  echo "}"
  echo
  # Per-worker map for Lua-routed requests
  echo "map \$target_worker \$target_dest {"
  echo "    default        \"worker-${WORKER_NAMES[0]}:8000\";"
  for name in "${WORKER_NAMES[@]}"; do
    echo "    \"worker-$name\"  \"worker-$name:8000\";"
  done
  echo "    \"unavailable\"  \"claude_unavail\";"
  echo "}"
} > "$UPSTREAMS_OUT"

echo "[generate] Wrote $COMPOSE_OUT (${#WORKER_NAMES[@]} workers)"
echo "[generate] Wrote $UPSTREAMS_OUT"
echo "[generate] To deploy:"
echo "[generate]   cd $(dirname "$COMPOSE_OUT")"
echo "[generate]   docker compose -f docker-compose.generated.yml up -d --build"
