#!/usr/bin/env bash
# bridge-deploy.sh — Atomic idempotent multi-server Bridge deployment
#
# Usage: bridge-deploy.sh <server> [<service>...] [--dry-run]
#   server  = hetzner | server2 | both
#   service = optional, default = all services for the server
#
# EXIT CODES:
#   0 = success (or dry-run completed)
#   1 = deployment failure (rollback attempted and succeeded)
#   2 = critical failure (rollback itself failed — manual intervention required)
set -euo pipefail

# ============================================================================
# Config
# ============================================================================
HETZNER_HOST="49.12.72.66"
SERVER2_HOST="178.104.178.79"
SSH_KEY="/root/.ssh/id_ed25519"
SSH_BASE_OPTS="-i ${SSH_KEY} -o ConnectTimeout=10 -o StrictHostKeyChecking=no -o BatchMode=yes"
REMOTE_REPO="/root/werkingflow-bridge"
HETZNER_COMPOSE="-f docker/docker-compose.yml -f docker/docker-compose-platform-overlay.yml"
SERVER2_COMPOSE="-f docker/docker-compose-prod.yml -f docker/docker-compose-prod-platform.yml"
# Worker init on a fresh container is much slower than expected: container
# bootstrap (~60s) + a ~80s blocking pause inside lifespan between
# "Session cleanup task started" and "AdaptiveLoadLimiter tune loop started"
# (root cause of that pause not yet investigated, observed Apr/May 2026).
# Total time-to-healthy is consistently ~4 min in production, occasionally
# longer. Old 120s/240s defaults were both too tight and triggered
# spurious auto-rollbacks on otherwise-healthy deploys. 360s gives
# enough headroom; per-call override stays available via env var.
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-360}"
ROLLBACK_HEALTH_TIMEOUT="${ROLLBACK_HEALTH_TIMEOUT:-360}"   # symmetric: rollback restart hits the same lifespan pause
SKIP_DIST_TEST="${SKIP_DIST_TEST:-false}"  # escape hatch: SKIP_DIST_TEST=true bridge-deploy.sh hetzner
MIN_FREE_KB=$(( 5 * 1024 * 1024 ))  # 5 GB in KiB

# Hetzner: service → container name
HETZNER_SVC_nginx="wt-wrapper-lb"
HETZNER_SVC_worker1="wt-wrapper-worker1"
HETZNER_SVC_worker2="wt-wrapper-worker2"
HETZNER_SVC_worker3="wt-wrapper-worker3"
HETZNER_SVC_worker4="wt-wrapper-worker4"
HETZNER_SVC_privacy_service="wt-privacy-pdf-service"
HETZNER_SVC_metrics_reader="wt-wrapper-metrics-reader"
HETZNER_SVC_platform_api="wt-platform-api"
# platform-api is deployed BEFORE nginx so the upstream resolves when nginx
# restarts. nginx is last so the new routing is live only after platform-api
# is healthy.
HETZNER_ALL="platform-api nginx worker1 worker2 worker3 worker4 privacy-service metrics-reader"
HETZNER_NEEDS_BUILD="platform-api nginx worker1 worker2 worker3 worker4 privacy-service metrics-reader"

# Server-2: service → container name (use _ not - for var names)
SERVER2_SVC_nginx="wt-prod-lb"
# Replaces the old single worker-prod after commit ad14f82 split it into
# two-account workers (sahori + kurt) for ~2x rate-limit capacity.
SERVER2_SVC_worker_sahori="wt-prod-worker-sahori"
SERVER2_SVC_worker_kurt="wt-prod-worker-kurt"
# privacy-prod REMOVED 2026-06-26: Flair (~13GB) OOM'd this 7GB host; prod now
# routes smart-anonymize to the dev-bridge privacy service over Tailscale.
SERVER2_SVC_metrics_reader_prod="wt-prod-metrics-reader"
SERVER2_SVC_postgres_prod="bridge-postgres-prod"
SERVER2_SVC_platform_api="wt-prod-platform-api"
# postgres-prod zuerst (DB vor platform-api), platform-api vor nginx (Upstream-Resolve).
SERVER2_ALL="postgres-prod platform-api nginx worker-sahori worker-kurt metrics-reader-prod"
# nginx now BUILDS from Dockerfile.nginx-lb (OpenResty+Lua) — the same image as
# primary (ADR-0006 B/C). It was a pre-built nginx:alpine before the unification.
SERVER2_NEEDS_BUILD="platform-api nginx worker-sahori worker-kurt metrics-reader-prod"

# State (reset per server in deploy_server)
ROLLBACK_SHA=""
DEPLOYED_SERVICES=()

# ============================================================================
# Logging
# ============================================================================
log() {
    local level="$1"; shift
    printf '[%s %s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$level" "$*"
}
info()    { log "INFO " "$@"; }
warn()    { log "WARN " "$@" >&2; }
error_()  { log "ERROR" "$@" >&2; }
step()    { log "STEP " "=== $* ===" ; }

# ============================================================================
# Helpers
# ============================================================================
rssh() {
    local host="$1"; shift
    # shellcheck disable=SC2086
    sudo -n ssh $SSH_BASE_OPTS "root@${host}" "$@"
}

# rssh_run: run a multi-line bash script on a remote host via stdin.
#
# Defensive against the quote-escaping hell of `rssh host "$big_string"`:
# the remote runs `bash -s` and reads the entire script from stdin, so
# single quotes, backticks, awk programs and dollar-signs travel literally
# without needing layers of \" and \$ escapes.
#
# `errexit -o pipefail` is enabled inside the remote shell so a failing
# command in the middle of the script does not silently leak its exit
# status — the caller sees the first failure as the script exit code.
# The local caller still decides what to do on non-zero (|| / && chains).
#
# The script body is captured from THIS function's stdin so a heredoc at
# the call site is the natural input. Both remote stdout and stderr flow
# back over SSH and are visible to the caller's command substitution —
# matching the single-stream behaviour callers already had from rssh().
#
# Local variables in the heredoc are expanded BEFORE rssh_run sees them
# (normal bash heredoc semantics), so `${REMOTE_REPO}` etc. are baked in.
# To keep a literal `$foo` on the remote side, escape it as `\$foo`.
#
# Usage:
#   output=$(rssh_run "$host" <<EOF
#     cd ${REMOTE_REPO}
#     awk '/^services:/ { print }' docker/compose.yml
#   EOF
#   )
rssh_run() {
    local host="$1"
    local script_body
    script_body=$(cat)
    # shellcheck disable=SC2086
    sudo -n ssh $SSH_BASE_OPTS "root@${host}" "bash -s" <<EOF
set -euo pipefail
${script_body}
EOF
}

svc_to_varname() {
    # Convert service name to bash variable segment: worker-prod → worker_prod
    echo "${1//-/_}"
}

container_for_svc() {
    local server="$1"
    local svc="$2"
    local varname="${server}_SVC_$(svc_to_varname "$svc")"
    echo "${!varname:-$svc}"
}

service_needs_build() {
    local svc="$1"
    local build_list="$2"
    echo "$build_list" | tr ' ' '\n' | grep -qx "$svc"
}

# ============================================================================
# Dry-run wrappers
# ============================================================================
DRY_RUN=false

dry_rssh() {
    local host="$1"; shift
    if [[ "$DRY_RUN" == "true" ]]; then
        info "[DRY-RUN] ssh root@${host}: $*"
    else
        rssh "$host" "$@"
    fi
}

# ============================================================================
# Parse arguments
# ============================================================================
SERVER=""
SERVICES_ARG=()

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        hetzner|server2|both) SERVER="$arg" ;;
        *) SERVICES_ARG+=("$arg") ;;
    esac
done

if [[ -z "$SERVER" ]]; then
    echo "Usage: bridge-deploy.sh <hetzner|server2|both> [service...] [--dry-run]" >&2
    exit 1
fi

[[ "$DRY_RUN" == "true" ]] && info "=== DRY-RUN MODE — no changes will be made ==="

# ============================================================================
# Deploy-Lock — genau EIN Deploy pro Zielhost gleichzeitig
# ============================================================================
# Zwei parallele Deploys teilen sich den Checkout auf dem Zielhost: der Auto-
# Rollback des einen dreht den Checkout zurück, während der andere mitten im
# Worker-Rollout daraus baut → Mixed-Code-Flotte, die wie ein erfolgreicher
# Deploy aussieht (passiert 2026-07-22: worker1=6f7f4fc, worker2-4=d3c1f0d;
# Repair-Deploy nötig). Der Lock gilt pro Host; "both" hält beide Locks für
# die gesamte Laufzeit. fd bleibt bis Prozessende offen → Lock löst sich bei
# jedem Exit (auch kill/crash) automatisch.
acquire_deploy_lock() {
    local host_label="$1" fd="$2"
    local lockfile="/var/lock/bridge-deploy.${host_label}.lock"
    # Append-Modus: das Öffnen darf die Holder-Info eines laufenden Deploys
    # nicht trunkieren (ein '>' würde das tun, noch VOR dem flock-Versuch).
    eval "exec ${fd}>>\"\$lockfile\""
    if ! flock -n "$fd"; then
        error_ "Another bridge-deploy for '${host_label}' is already running (holder: $(tail -1 "$lockfile" 2>/dev/null || echo unbekannt), lock: ${lockfile})."
        error_ "Warten bis der laufende Deploy fertig ist — NIEMALS parallel deployen (Mixed-Code-Race)."
        exit 1
    fi
    truncate -s 0 "$lockfile"
    printf 'pid=%s started=%s user=%s\n' "$$" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$(whoami)" >&"$fd"
}
case "$SERVER" in
    hetzner) acquire_deploy_lock "hetzner" 210 ;;
    server2) acquire_deploy_lock "server2" 211 ;;
    both)    acquire_deploy_lock "hetzner" 210; acquire_deploy_lock "server2" 211 ;;
esac

# ============================================================================
# Phase 1: Pre-flight checks (read-only)
# ============================================================================
phase_preflight() {
    local host="$1"
    step "Phase 1: Pre-flight ($host)"

    # SSH connectivity
    info "SSH connect test..."
    if ! rssh "$host" 'echo pong' > /dev/null 2>&1; then
        error_ "SSH connection failed to $host"
        return 1
    fi
    info "SSH OK"

    # Reclaim disk BEFORE the build. Every deploy rebuilds --no-cache, which
    # orphans the previous same-tag image (→ dangling) and leaves build cache;
    # across a few deploys /var/lib/docker fills until a build dies mid-way on
    # "no space left on device" (observed 2026-06 — broke a worker build AND
    # its rollback, requiring manual recovery). Prune here so the disk check
    # below sees the real free space. Safe: dangling images are orphaned, build
    # cache is reclaimable, rollback rebuilds from code (never reuses an old
    # image). Non-fatal — a prune hiccup must never block a deploy.
    if [[ "$DRY_RUN" == "false" ]]; then
        info "Reclaiming disk (dangling images + build cache)..."
        local prune_out
        prune_out=$(rssh "$host" "docker image prune -f >/dev/null 2>&1; docker builder prune -af 2>&1 | tail -1" 2>&1 || true)
        info "  ${prune_out:-prune done}"
    else
        info "[DRY-RUN] Would prune dangling images + build cache before build"
    fi

    # Disk space (need ≥ 5 GB free on /var/lib/docker)
    info "Disk space check..."
    local free_kb
    free_kb=$(rssh "$host" "df /var/lib/docker --output=avail -k | tail -1 | tr -d ' '")
    if (( free_kb < MIN_FREE_KB )); then
        error_ "Insufficient disk space on $host: $(( free_kb / 1024 / 1024 ))GB free, need 5GB"
        return 1
    fi
    info "Disk OK: $(( free_kb / 1024 / 1024 ))GB free"

    # Git: no modified/staged tracked files (untracked files are OK for git pull --ff-only)
    info "Git state check..."
    local dirty
    dirty=$(rssh "$host" "cd ${REMOTE_REPO} && git status --porcelain | grep -v '^??'" || true)
    if [[ -n "$dirty" ]]; then
        error_ "Tracked files modified/staged on $host:${REMOTE_REPO} — abort:"
        error_ "$dirty"
        return 1
    fi
    local branch
    branch=$(rssh "$host" "cd ${REMOTE_REPO} && git rev-parse --abbrev-ref HEAD")
    if [[ "$branch" != "develop" ]]; then
        error_ "Not on develop branch on $host (got: '$branch')"
        return 1
    fi
    info "Git OK (branch=develop, clean)"
}

# ============================================================================
# Phase 2: Code update — sets global ROLLBACK_SHA
# ============================================================================
phase_code_update() {
    local host="$1"
    step "Phase 2: Code update ($host)"

    ROLLBACK_SHA=$(rssh "$host" "cd ${REMOTE_REPO} && git rev-parse HEAD")
    info "Rollback SHA saved: ${ROLLBACK_SHA}"

    info "Fetching origin..."
    if ! rssh "$host" "cd ${REMOTE_REPO} && timeout 30 git fetch origin"; then
        error_ "git fetch failed on $host"
        return 1
    fi

    local remote_sha
    remote_sha=$(rssh "$host" "cd ${REMOTE_REPO} && git rev-parse origin/develop")

    if [[ "${ROLLBACK_SHA}" == "${remote_sha}" ]]; then
        info "Already up-to-date at ${ROLLBACK_SHA} — skipping pull (idempotent)"
        return 0
    fi

    info "Code: ${ROLLBACK_SHA} → ${remote_sha}"
    dry_rssh "$host" "cd ${REMOTE_REPO} && git pull --ff-only origin develop" || {
        error_ "git pull --ff-only failed — cannot fast-forward to origin/develop"
        return 1
    }
    info "Pull OK → $(rssh "${host}" "cd ${REMOTE_REPO} && git rev-parse HEAD" 2>/dev/null || echo "${remote_sha}")"
}

# ============================================================================
# Phase 3: Pre-deploy validation
# ============================================================================
phase_validate() {
    local host="$1"
    local compose="$2"
    step "Phase 3: Validation ($host)"

    if [[ "$DRY_RUN" == "true" ]]; then
        info "[DRY-RUN] Skipping live validation"
        return 0
    fi

    # Compose config syntax
    info "Compose syntax check..."
    rssh "$host" "cd ${REMOTE_REPO} && docker compose ${compose} config --quiet" > /dev/null
    info "Compose OK"

    # nginx config syntax: envsubst with EXACT same variable list as the container uses,
    # then nginx -t inside nginx:alpine with --add-host for all internal upstream hostnames
    # (needed because nginx -t resolves upstream hosts; they only exist inside the compose
    # network at runtime, not in this test container).
    #
    # add-host list is derived from the compose service names so a worker rename
    # (e.g. worker-prod → worker-sahori + worker-kurt, commit ad14f82) doesn't
    # silently break the next deploy — the previous hardcoded list was the
    # actual root cause of three rolled-back server2 deploys on 12.05.2026.
    # SINGLE SOURCE (ADR-0006 B/C): BOTH bridges validate the shared docker/nginx.conf.
    # The only per-bridge input is the generated upstreams include.
    local nginx_conf upstreams_conf envsubst_vars add_hosts
    nginx_conf="docker/nginx.conf"
    envsubst_vars='$BRIDGE_BACKUP_HOST $BRIDGE_ID'
    if [[ "$compose" == *"prod"* ]]; then
        upstreams_conf="docker/upstreams-prod.conf"
    else
        upstreams_conf="docker/upstreams-primary.conf"
    fi
    # Pull every top-level service from the compose file, drop the lb itself
    # (nginx is the test target, doesn't need to resolve itself), map each
    # to 127.0.0.1 so nginx -t's upstream resolution succeeds in the test
    # container even though those services only exist inside the compose net.
    # awk stays in_services until the next top-level YAML section so
    # networks/volumes/secrets entries don't bleed into the host list.
    #
    # Two defensive moves over the previous implementation:
    #
    #   1. The compose variable holds `-f file1.yml -f file2.yml` flags, but
    #      awk treats `-f` as "read program from file" — so passing it through
    #      directly produced `awk: cannot open '-f' for reading`. Strip the
    #      flags first, leaving only the YAML paths.
    #
    #   2. The awk program contains single quotes and `$` regex anchors that
    #      previously had to survive three layers of escaping inside an ssh
    #      "double-quoted" command string. We now pipe the whole script
    #      through rssh_run via heredoc — no escaping needed beyond the
    #      standard `\$` required by an UNquoted heredoc to defer expansion
    #      to the remote side.
    #
    # The `sort -u` deduplicates entries that appear in both the base and
    # overlay compose files (worker1..4 in our case).
    local compose_files
    compose_files=$(echo "$compose" | tr ' ' '\n' | grep -v '^-f$' | grep -v '^$' | tr '\n' ' ')
    local add_hosts_output add_hosts_rc
    add_hosts_output=$(rssh_run "$host" <<EOF
cd ${REMOTE_REPO}
awk '
  /^services:/ { in_services=1; next }
  /^[a-z][a-zA-Z0-9_-]*:\$/ { in_services=0 }
  in_services && /^  [a-zA-Z0-9_-]+:\$/ { gsub(/[ :]/, ""); print }
' ${compose_files} \
  | grep -vE '^(nginx|lb)\$' \
  | sort -u \
  | sed 's|^|--add-host=|;s|\$|:127.0.0.1|' \
  | tr '\n' ' '
EOF
    )
    add_hosts_rc=$?
    if [ $add_hosts_rc -ne 0 ]; then
        # Fail-fast: a non-zero exit here is almost always a malformed compose
        # file or an SSH-side bash error — both must be fixed before deploy,
        # not silently swallowed. Emit the captured output so the operator
        # has something to grep.
        error_ "Could not derive add_hosts from ${compose} (rc=${add_hosts_rc}). Captured output:"
        while IFS= read -r line; do error_ "  add_hosts: ${line}"; done <<< "$add_hosts_output"
        return 1
    fi
    add_hosts="$add_hosts_output"
    if [ -z "${add_hosts// /}" ]; then
        warn "add_hosts list is empty for ${compose} — nginx test will run without it"
        add_hosts=""
    fi
    info "nginx config syntax check (${nginx_conf})..."
    local nginx_output nginx_rc
    nginx_output=$(rssh_run "$host" <<EOF
cd ${REMOTE_REPO}
BRIDGE_BACKUP_HOST=127.0.0.1 BRIDGE_ID=validate-probe \
    envsubst '${envsubst_vars}' \
    < ${nginx_conf} > /tmp/bridge-nginx-check.conf 2>&1
# Render the per-topology upstreams include (nginx.conf does include /tmp/upstreams.conf).
BRIDGE_BACKUP_HOST=127.0.0.1 \
    envsubst '\$BRIDGE_BACKUP_HOST' \
    < ${upstreams_conf} > /tmp/bridge-upstreams-check.conf 2>&1
# --add-host metrics-reader: nginx.conf's metrics_reader upstream hardcodes the
# name `metrics-reader` (resolves live via prod's compose alias); the derived
# add_hosts list only carries service names (metrics-reader-prod), so add it here.
if docker run --rm \
    ${add_hosts} --add-host=metrics-reader:127.0.0.1 \
    --tmpfs /var/log/nginx \
    -v /tmp/bridge-nginx-check.conf:/etc/nginx/nginx.conf:ro \
    -v /tmp/bridge-upstreams-check.conf:/tmp/upstreams.conf:ro \
    -v ${REMOTE_REPO}/docker/routes-metrics-reader.conf:/etc/nginx/routes-metrics-reader.conf:ro \
    -v ${REMOTE_REPO}/docker/routes-platform-api.conf:/etc/nginx/routes-platform-api.conf:ro \
    -v ${REMOTE_REPO}/docker/lua:/etc/nginx/lua:ro \
    openresty/openresty:1.27.1.1-alpine \
    sh -c 'touch /var/log/nginx/access.jsonl && openresty -t -c /etc/nginx/nginx.conf' 2>&1
then
    echo __NGINX_OK__
else
    echo __NGINX_FAIL__
fi
rm -f /tmp/bridge-nginx-check.conf /tmp/bridge-upstreams-check.conf
EOF
    )
    nginx_rc=$?

    while IFS= read -r line; do info "  nginx-t: ${line}"; done <<< "$nginx_output"
    # Two failure modes to distinguish (fail-fast on each, but with separate
    # diagnostic messages so the operator knows where to look):
    #   1. nginx_rc != 0 → the wrapper script itself failed on the remote
    #      (bash syntax, envsubst missing, docker daemon down, …). NOT an
    #      nginx-config problem; do not silently retry.
    #   2. nginx_rc == 0 but output does not contain __NGINX_OK__ → the
    #      nginx config itself is invalid. Real config-fix needed.
    if [ $nginx_rc -ne 0 ]; then
        error_ "nginx validation wrapper FAILED on ${host} (rc=${nginx_rc}) — this is a script/env problem, not an nginx-config problem. See output above."
        return 1
    fi
    if ! echo "$nginx_output" | grep -q '__NGINX_OK__'; then
        error_ "nginx config validation FAILED (see output above)"
        return 1
    fi
    info "nginx config OK"
}

# ============================================================================
# Phase 4: Deploy one service
# ============================================================================
deploy_one_service() {
    local host="$1"
    local compose="$2"
    local svc="$3"
    local container="$4"
    local build_list="$5"

    step "Phase 4: Deploy ${svc} (${container}) on ${host}"

    # One-time cleanup for the 2026-05 eco-* → wt-* container rename:
    # if a container with the legacy name still exists it will block the new
    # name (port collision + globally unique container names). Compose alone
    # does not bridge the rename because old containers carry a different
    # com.docker.compose.project label (working_dir=docker/ vs ../). The
    # block becomes a no-op after the first successful deploy since the
    # legacy container no longer exists.
    if [[ "$DRY_RUN" == "false" ]]; then
        local legacy_name="${container/wt-/eco-}"
        if [[ "$legacy_name" != "$container" ]]; then
            if rssh "$host" "docker inspect '${legacy_name}' >/dev/null 2>&1"; then
                warn "Removing legacy container '${legacy_name}' (one-time rename cleanup)"
                rssh "$host" "docker rm -f '${legacy_name}'" || {
                    error_ "Failed to remove legacy '${legacy_name}'"
                    return 1
                }
            fi
        fi
    fi

    # Build if this service has a Dockerfile
    if service_needs_build "$svc" "$build_list"; then
        info "Building ${svc}..."
        dry_rssh "$host" "cd ${REMOTE_REPO} && docker compose ${compose} build --no-cache ${svc} 2>&1" || {
            error_ "Build failed for ${svc} on ${host}"
            return 1
        }
        info "Build OK"
    else
        info "${svc} uses pre-built image — skipping build"
    fi

    # Recreate — NEVER --remove-orphans
    info "Recreating ${svc}..."
    dry_rssh "$host" "cd ${REMOTE_REPO} && docker compose ${compose} up -d --no-deps --force-recreate ${svc} 2>&1" || {
        error_ "docker compose up failed for ${svc} on ${host}"
        return 1
    }

    # Health wait (skip in dry-run)
    if [[ "$DRY_RUN" == "true" ]]; then
        info "[DRY-RUN] Would wait for ${container} to be healthy (timeout ${HEALTH_TIMEOUT}s)"
        return 0
    fi

    info "Waiting for ${container} healthy (timeout ${HEALTH_TIMEOUT}s)..."
    local deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
    local status=""
    while (( $(date +%s) < deadline )); do
        status=$(rssh "$host" \
            "docker inspect --format '{{.State.Health.Status}}' '${container}' 2>/dev/null || echo 'none'")
        case "$status" in
            healthy)
                info "${container} is healthy"
                return 0
                ;;
            unhealthy)
                error_ "${container} entered unhealthy state"
                rssh "$host" "docker logs --tail=50 '${container}' 2>&1" \
                    | while IFS= read -r line; do error_ "  LOG: $line"; done || true
                return 1
                ;;
            *)
                info "  ${container} status: ${status} — waiting..."
                sleep 5
                ;;
        esac
    done
    error_ "Health timeout after ${HEALTH_TIMEOUT}s for ${container} (last: ${status})"
    return 1
}

# ============================================================================
# Phase 5: End-to-End Smoke Test
# ============================================================================
phase_smoke_test() {
    local label="$1"
    local url="$2"
    local extra_header="${3:-}"  # e.g. "X-Priority: production"

    step "Phase 5: Smoke test (${label} @ ${url})"

    # Functional per-endpoint smoke (bridge_smoke.py): exercises document/convert,
    # smart-anonymize, the convert family, chat, research and metrics with REAL
    # payloads and asserts correctness — not just liveness. This is what stops a
    # broken endpoint (e.g. the /v1/document/convert 415 that shipped for weeks
    # while /health, /lb-status and the old research-only smoke stayed green) from
    # going live. Endpoint coverage is enforced by bridge_smoke_coverage.py.
    # On failure the caller (deploy_server) routes into the existing auto-rollback.
    local smoke_script
    smoke_script="$(dirname "${BASH_SOURCE[0]}")/bridge_smoke.py"

    # Map deploy label -> smoke profile. hetzner runs the full suite (incl. the
    # privacy-pdf endpoints); server2 is LLM-workers only.
    local profile="hetzner"
    [[ "$label" == "server2" ]] && profile="server2"

    if [[ "$DRY_RUN" == "true" ]]; then
        info "[DRY-RUN] Would run: bridge_smoke.py --base-url ${url} --profile ${profile}"
        return 0
    fi

    # Make AI_BRIDGE_API_KEY / INFISICAL_WS_DEV_SERVER available to the smoke
    # script's key resolution (env first, Infisical fallback).
    # shellcheck source=/root/.infisical/infisical-api.sh
    source /root/.infisical/infisical-api.sh 2>/dev/null || true

    local extra_args=()
    [[ -n "$extra_header" ]] && extra_args+=(--extra-header "$extra_header")

    local smoke_out
    if smoke_out=$(python3 "$smoke_script" --base-url "$url" --profile "$profile" \
            --attempts 3 "${extra_args[@]}" 2>&1); then
        while IFS= read -r line; do info "  smoke: ${line}"; done <<< "$smoke_out"
        info "Smoke test PASSED for ${label}"
        return 0
    fi

    while IFS= read -r line; do error_ "  smoke: ${line}"; done <<< "$smoke_out"
    error_ "Smoke test FAILED for ${label}"

    # Auto-spawn an infra-fix session so a broken endpoint is already being worked
    # before anyone notices. Best-effort: the hook's own failure must NEVER change
    # the deploy outcome — the rollback is driven solely by our `return 1` below.
    local fix_hook
    fix_hook="$(dirname "${BASH_SOURCE[0]}")/spawn-infra-fix-session.sh"
    if [[ -x "$fix_hook" ]]; then
        SMOKE_LABEL="$label" SMOKE_URL="$url" SMOKE_OUTPUT="$smoke_out" \
            "$fix_hook" || warn "infra-fix-session hook failed (non-fatal)"
    fi
    return 1
}

# ============================================================================
# Phase 5b: Distribution Test — validates pool-router spreads load across workers
# Runs 8 sequential /v1/chat/completions calls; checks X-Target-Worker header
# distribution and the /internal/pool-router/state endpoint via docker exec.
# ============================================================================
phase_distribution_test() {
    local host="$1"
    local url="$2"
    local lb_container="$3"  # e.g. wt-wrapper-lb

    step "Phase 5b: Distribution + State Test (${label:-dist} @ ${url})"

    if [[ "$DRY_RUN" == "true" ]]; then
        info "[DRY-RUN] Would send 8 chat/completions calls and check /internal/pool-router/state"
        return 0
    fi

    if [[ "$SKIP_DIST_TEST" == "true" ]]; then
        warn "SKIP_DIST_TEST=true — skipping distribution test (escape hatch active)"
        return 0
    fi

    # Load Infisical env
    # shellcheck source=/root/.infisical/infisical-api.sh
    source /root/.infisical/infisical-api.sh 2>/dev/null || true
    local api_key
    api_key=$(infisical_get_secret "${INFISICAL_WS_DEV_SERVER}" dev AI_BRIDGE_API_KEY 2>/dev/null) || true
    if [[ -z "${api_key:-}" ]]; then
        error_ "Failed to retrieve AI_BRIDGE_API_KEY from Infisical"
        return 1
    fi

    # 8 sequential chat/completions calls; parse X-Target-Worker header
    info "Sending 8 chat/completions calls to ${url} to validate worker distribution..."
    local dist_out
    dist_out=$(DIST_URL="${url}" DIST_API_KEY="${api_key}" python3 - <<'PYEOF'
import os, sys, json, requests
from collections import Counter

url     = os.environ["DIST_URL"]
api_key = os.environ["DIST_API_KEY"]

headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
    "X-Client-ID": "bridge-deploy/dist-test",
    # Same contract as the smoke test: deploy probes book anonymous, not
    # unattributed (the dist test fires 8 chat calls per rollout).
    "X-User-ID": "anonymous:bridge-deploy-dist-test",
}

workers_hit = []
for i in range(8):
    try:
        r = requests.post(
            f"{url}/v1/chat/completions",
            headers=headers,
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "Reply with one word: ok"}],
            },
            timeout=60,
        )
    except Exception as e:
        print(f"DIST_FAIL: call {i+1}/8 request error: {e}", file=sys.stderr)
        sys.exit(1)

    # 200 = success, 429 = worker rate-limited but routing decision was made (still counts)
    # 5xx = infrastructure failure → hard fail
    if r.status_code not in (200, 429):
        print(f"DIST_FAIL: call {i+1}/8 unexpected HTTP {r.status_code}: {r.text[:300]}", file=sys.stderr)
        sys.exit(1)

    worker = r.headers.get("X-Target-Worker", "unknown")
    note  = "" if r.status_code == 200 else " (429 worker rate-limited — counts as hit)"
    print(f"  call {i+1}/8: worker={worker} HTTP {r.status_code}{note}")
    if worker and worker != "unknown":
        workers_hit.append(worker)

unique_workers = set(workers_hit)
distribution   = dict(Counter(workers_hit))
print(f"Distribution: {len(unique_workers)}/4 unique workers — {distribution}")

if len(unique_workers) < 2:
    print(
        f"DIST_FAIL: only {len(unique_workers)} unique worker(s) hit across 8 calls "
        f"(need >=2): {sorted(unique_workers)}. "
        f"Pool router is not distributing load — check pool_router.lua tie-breaker logic.",
        file=sys.stderr,
    )
    sys.exit(1)

print(f"DIST_OK: {len(unique_workers)} unique workers hit — {distribution}")
PYEOF
    ) 2>&1
    rc_dist=$?

    while IFS= read -r line; do info "  dist: ${line}"; done <<< "$dist_out"

    if [[ $rc_dist -ne 0 ]] || ! echo "$dist_out" | grep -q 'DIST_OK:'; then
        error_ "Distribution test FAILED — pool router not spreading load across workers"
        return 1
    fi

    # Check /internal/pool-router/state via docker exec inside the lb container
    info "Checking /internal/pool-router/state on ${host} (via docker exec ${lb_container})..."
    local state_raw
    state_raw=$(rssh "$host" "docker exec '${lb_container}' curl -sf http://127.0.0.1/internal/pool-router/state" 2>&1)
    local rc_state=$?
    if [[ $rc_state -ne 0 ]] || [[ -z "$state_raw" ]]; then
        error_ "Failed to reach /internal/pool-router/state via docker exec (rc=${rc_state}): ${state_raw}"
        return 1
    fi

    info "  raw state: ${state_raw}"

    # Pass JSON via env var — avoids pipe+heredoc stdin conflict with python3 -
    local state_check
    state_check=$(STATE_JSON="${state_raw}" python3 - <<'PYEOF'
import os, sys, json
raw = os.environ.get("STATE_JSON", "")
try:
    d = json.loads(raw)
except Exception as e:
    print(f"STATE_FAIL: response is not valid JSON: {e}", file=sys.stderr)
    sys.exit(1)

status    = d.get("last_refresh_status", "unknown")
age_s     = float(d.get("state_age_s", 9999))
last_err  = d.get("last_refresh_err", "")

if status != "ok":
    print(f"STATE_FAIL: last_refresh_status={status!r} (expected 'ok'); err={last_err!r}", file=sys.stderr)
    sys.exit(1)
if age_s >= 30:
    print(f"STATE_FAIL: state_age_s={age_s:.1f}s >= 30s — metrics-reader refresh not working", file=sys.stderr)
    sys.exit(1)

counters = d.get("decision_counter_per_worker", {})
print(f"STATE_OK: status={status}, age={age_s:.1f}s, counters={counters}")
PYEOF
    )
    rc_sc=$?

    while IFS= read -r line; do info "  state: ${line}"; done <<< "$state_check"

    if [[ $rc_sc -ne 0 ]] || ! echo "$state_check" | grep -q 'STATE_OK:'; then
        error_ "Pool-router state check FAILED"
        return 1
    fi

    info "Distribution + State test PASSED"
    return 0
}

# ============================================================================
# Phase 6: Rollback
# ============================================================================
phase_rollback() {
    local host="$1"
    local compose="$2"
    local sha="$3"
    local build_list="$4"
    # remaining args = services to roll back
    shift 4
    local services=("$@")

    warn "=== ROLLBACK: ${host} → ${sha} ==="

    rssh "$host" "cd ${REMOTE_REPO} && git reset --hard '${sha}'" || {
        error_ "CRITICAL: git reset --hard failed on ${host} — manual intervention required"
        return 2
    }
    warn "Code reset to ${sha}"

    local failed=false
    for svc in "${services[@]}"; do
        local container
        if [[ "$host" == "$HETZNER_HOST" ]]; then
            container=$(container_for_svc "HETZNER" "$svc")
        else
            container=$(container_for_svc "SERVER2" "$svc")
        fi

        warn "Rolling back ${svc} (${container})..."

        if service_needs_build "$svc" "$build_list"; then
            rssh "$host" "cd ${REMOTE_REPO} && docker compose ${compose} build --no-cache ${svc} 2>&1" || {
                error_ "CRITICAL: rollback build failed for ${svc}"
                failed=true
                continue
            }
        fi

        rssh "$host" "cd ${REMOTE_REPO} && docker compose ${compose} up -d --no-deps --force-recreate ${svc} 2>&1" || {
            error_ "CRITICAL: rollback recreate failed for ${svc}"
            failed=true
            continue
        }

        # Health wait during rollback — use ROLLBACK_HEALTH_TIMEOUT (longer than deploy:
        # SDK init on a fresh container can take >60s, standard HEALTH_TIMEOUT may be tight
        # during rollback when the host is under load from the failed deploy)
        local deadline=$(( $(date +%s) + ROLLBACK_HEALTH_TIMEOUT ))
        local status=""
        while (( $(date +%s) < deadline )); do
            status=$(rssh "$host" \
                "docker inspect --format '{{.State.Health.Status}}' '${container}' 2>/dev/null || echo 'none'")
            [[ "$status" == "healthy" ]] && break
            sleep 5
        done
        if [[ "$status" != "healthy" ]]; then
            error_ "CRITICAL: rollback health failed for ${container} (status: ${status}) after ${ROLLBACK_HEALTH_TIMEOUT}s"
            failed=true
        else
            warn "Rollback OK: ${svc}"
        fi
    done

    [[ "$failed" == "true" ]] && { error_ "CRITICAL: partial rollback failure on ${host}"; return 2; }
    warn "Rollback complete on ${host}"
    return 0
}

# ============================================================================
# Phase 3.5 — reconcile the worker auth key with the Infisical SSoT
# ============================================================================
# The worker containers authenticate every incoming request against
# AI_BRIDGE_API_KEY, which they read from the host env_file secrets/platform.env
# (see the worker `env_file:` in docker-compose*.yml). That file is host-local
# and gitignored — `git pull` (phase_code_update) never touches it. So when
# AI_BRIDGE_API_KEY is rotated in Infisical (the single SSoT), any host whose
# platform.env was not hand-updated keeps serving the OLD key. Until now the
# deploy only READ the fresh key to run the smoke, never PROVISIONED it — so a
# stale host force-recreated a container that rejects every authed request with
# "[Bridge <worker>] Invalid API key" (a cryptic 401), which the smoke then
# caught and auto-rolled-back. That is exactly the server2 drift seen 2026-07-06.
#
# This phase closes the gap at the source: it upserts AI_BRIDGE_API_KEY in the
# host platform.env from Infisical BEFORE the recreate, so the key the deploy
# tests and the key the container runs are identical by construction. It is
# fail-loud (empty key / missing file / verify mismatch → abort, no recreate),
# idempotent (no-op when already in sync), and preserves every other line.
phase_reconcile_worker_key() {
    local host="$1"
    step "Phase 3.5: reconcile worker AI_BRIDGE_API_KEY (${host})"

    # shellcheck source=/root/.infisical/infisical-api.sh
    source /root/.infisical/infisical-api.sh 2>/dev/null || true
    local api_key
    api_key=$(infisical_get_secret "${INFISICAL_WS_DEV_SERVER}" dev AI_BRIDGE_API_KEY 2>/dev/null | tail -1) || true
    # Fail fast: the key must be present and a single clean token. A blank,
    # 'null', or whitespace-bearing value means the Infisical read failed or
    # returned a diagnostic line — provisioning THAT into platform.env would
    # brick auth on every worker, so abort BEFORE touching the host file.
    if [[ -z "${api_key:-}" || "${api_key}" == "null" ]]; then
        error_ "Cannot reconcile: AI_BRIDGE_API_KEY empty/null from Infisical (dev-server/dev)"
        return 1
    fi
    if [[ "${api_key}" =~ [[:space:]] ]]; then
        error_ "Cannot reconcile: AI_BRIDGE_API_KEY from Infisical contains whitespace (likely a diagnostic line, not the key) — refusing to write it"
        return 1
    fi

    if [[ "$DRY_RUN" == "true" ]]; then
        info "[DRY-RUN] would upsert AI_BRIDGE_API_KEY in ${REMOTE_REPO}/secrets/platform.env on ${host}"
        return 0
    fi

    # base64-transport the secret: it never appears verbatim in the remote script
    # text, process args or logs, and quoting/special chars become irrelevant.
    local key_b64
    key_b64=$(printf '%s' "${api_key}" | base64 -w0)

    local result
    result=$(rssh_run "$host" <<EOF
envfile="${REMOTE_REPO}/secrets/platform.env"
# Fail fast: do NOT create a half-baked secrets file. A missing platform.env
# means the host was never bootstrapped (it also holds BRIDGE_DB_URL etc.);
# writing only the API key would silently break the DB-backed services.
[[ -f "\$envfile" ]] || { echo "MISSING_ENVFILE"; exit 1; }
newkey=\$(printf '%s' "${key_b64}" | base64 -d)
cur=\$(grep -E '^AI_BRIDGE_API_KEY=' "\$envfile" | head -1 | cut -d= -f2-)
if [[ "\$cur" == "\$newkey" ]]; then echo "IN_SYNC"; exit 0; fi
# Temp in the SAME dir as envfile so mv is an atomic rename (a cross-fs mv is a
# non-atomic copy — a reader could see a truncated secrets file). trap cleans up
# the temp on any abort so a failed write never leaks a partial file.
tmp=\$(mktemp "\${envfile}.reconcile.XXXXXX")
trap 'rm -f "\$tmp" 2>/dev/null || true' EXIT
if grep -qE '^AI_BRIDGE_API_KEY=' "\$envfile"; then
    awk -v k="\$newkey" '/^AI_BRIDGE_API_KEY=/{print "AI_BRIDGE_API_KEY=" k; next} {print}' "\$envfile" > "\$tmp"
else
    cat "\$envfile" > "\$tmp"; printf 'AI_BRIDGE_API_KEY=%s\n' "\$newkey" >> "\$tmp"
fi
check=\$(grep -E '^AI_BRIDGE_API_KEY=' "\$tmp" | head -1 | cut -d= -f2-)
[[ "\$check" == "\$newkey" ]] || { echo "VERIFY_FAILED"; exit 1; }
chmod --reference="\$envfile" "\$tmp" 2>/dev/null || chmod 600 "\$tmp"
mv "\$tmp" "\$envfile"
trap - EXIT
echo "ROTATED"
EOF
    ) || { error_ "Key reconcile failed on ${host}: ${result:-<no output>}"; return 1; }

    case "$result" in
        IN_SYNC) info "AI_BRIDGE_API_KEY already in sync on ${host}" ;;
        ROTATED) info "AI_BRIDGE_API_KEY reconciled on ${host} — workers pick it up on recreate" ;;
        *)       error_ "Reconcile aborted on ${host}: ${result}"; return 1 ;;
    esac
    return 0
}

# ============================================================================
# Main: deploy one server
# ============================================================================
deploy_server() {
    local server_name="$1"
    local host compose all_services build_list server_prefix

    case "$server_name" in
        hetzner)
            host="$HETZNER_HOST"
            compose="$HETZNER_COMPOSE"
            all_services="$HETZNER_ALL"
            build_list="$HETZNER_NEEDS_BUILD"
            server_prefix="HETZNER"
            ;;
        server2)
            host="$SERVER2_HOST"
            compose="$SERVER2_COMPOSE"
            all_services="$SERVER2_ALL"
            build_list="$SERVER2_NEEDS_BUILD"
            server_prefix="SERVER2"
            ;;
        *)
            error_ "Unknown server: ${server_name}"
            return 1
            ;;
    esac

    # Resolve services to deploy
    local services_to_deploy=()
    if [[ ${#SERVICES_ARG[@]} -eq 0 ]]; then
        # Default: all services
        IFS=' ' read -ra services_to_deploy <<< "$all_services"
    else
        for svc in "${SERVICES_ARG[@]}"; do
            if ! echo "$all_services" | tr ' ' '\n' | grep -qx "$svc"; then
                error_ "Unknown service '${svc}' for ${server_name}. Valid: ${all_services}"
                return 1
            fi
        done
        services_to_deploy=("${SERVICES_ARG[@]}")
    fi

    info "Server: ${server_name} (${host})"
    info "Compose: ${compose}"
    info "Services: ${services_to_deploy[*]}"

    # Reset state
    ROLLBACK_SHA=""
    DEPLOYED_SERVICES=()

    # === Phase 1 ===
    phase_preflight "$host" || return 1

    # === Phase 2 ===
    phase_code_update "$host" || return 1

    # === Phase 3 ===
    phase_validate "$host" "$compose" || {
        error_ "Validation failed — reverting code to ${ROLLBACK_SHA}"
        if [[ "$DRY_RUN" == "false" ]]; then
            rssh "$host" "cd ${REMOTE_REPO} && git reset --hard '${ROLLBACK_SHA}'" || true
        fi
        return 1
    }

    # === Phase 3.5: reconcile worker auth key BEFORE any recreate ===
    # Must run after code_update (repo present) and before Phase 4 so the
    # force-recreate picks up the correct key via env_file. Failure aborts the
    # deploy with no container touched; reset code to the pre-deploy SHA.
    phase_reconcile_worker_key "$host" || {
        error_ "Key reconcile failed — reverting code to ${ROLLBACK_SHA}"
        if [[ "$DRY_RUN" == "false" ]]; then
            rssh "$host" "cd ${REMOTE_REPO} && git reset --hard '${ROLLBACK_SHA}'" || true
        fi
        return 1
    }

    # === Phase 4: per-service deploy ===
    for svc in "${services_to_deploy[@]}"; do
        local container
        container=$(container_for_svc "$server_prefix" "$svc")

        if deploy_one_service "$host" "$compose" "$svc" "$container" "$build_list"; then
            DEPLOYED_SERVICES+=("$svc")
        else
            error_ "Deploy failed for ${svc} — initiating rollback"
            if [[ ${#DEPLOYED_SERVICES[@]} -gt 0 ]]; then
                phase_rollback "$host" "$compose" "$ROLLBACK_SHA" "$build_list" "${DEPLOYED_SERVICES[@]}"
                local rc=$?
                (( rc == 2 )) && return 2
            else
                # Nothing deployed yet — just reset code
                [[ "$DRY_RUN" == "false" ]] && rssh "$host" "cd ${REMOTE_REPO} && git reset --hard '${ROLLBACK_SHA}'" || true
            fi
            return 1
        fi
    done

    # === Phase 5: smoke test ===
    if [[ "$server_name" == "hetzner" ]]; then
        # Source env to get AI_BRIDGE_URL
        source /root/.infisical/infisical-api.sh 2>/dev/null || true
        local hetzner_url="${AI_BRIDGE_URL:-http://${HETZNER_HOST}:8000}"

        phase_smoke_test "hetzner" "${hetzner_url}" "" || {
            error_ "Smoke test failed for hetzner — rolling back"
            if [[ ${#DEPLOYED_SERVICES[@]} -gt 0 ]]; then
                phase_rollback "$host" "$compose" "$ROLLBACK_SHA" "$build_list" "${DEPLOYED_SERVICES[@]}"
                local rc=$?
                (( rc == 2 )) && return 2
            fi
            return 1
        }

        phase_distribution_test "$host" "${hetzner_url}" "${HETZNER_SVC_nginx}" || \
            warn "Distribution test FAILED — optimization signal only, NOT rolling back (smoke test passed, deployment succeeded)"
    else
        phase_smoke_test "server2" "http://${SERVER2_HOST}:8000" "X-Priority: production" || {
            error_ "Smoke test failed for server2 — rolling back"
            if [[ ${#DEPLOYED_SERVICES[@]} -gt 0 ]]; then
                phase_rollback "$host" "$compose" "$ROLLBACK_SHA" "$build_list" "${DEPLOYED_SERVICES[@]}"
                local rc=$?
                (( rc == 2 )) && return 2
            fi
            return 1
        }
    fi

    # === Phase 7: Success report ===
    step "SUCCESS: ${server_name}"
    info "Deployed services : ${DEPLOYED_SERVICES[*]}"
    info "Pre-deploy SHA    : ${ROLLBACK_SHA}"
    if [[ "$DRY_RUN" == "false" ]]; then
        local current_sha
        current_sha=$(rssh "$host" "cd ${REMOTE_REPO} && git rev-parse HEAD")
        info "Current SHA       : ${current_sha}"
        info "Container states  :"
        for svc in "${DEPLOYED_SERVICES[@]}"; do
            local c
            c=$(container_for_svc "$server_prefix" "$svc")
            local state
            state=$(rssh "$host" \
                "docker inspect --format '{{.State.Status}} ({{.State.Health.Status}})' '${c}' 2>/dev/null || echo 'unknown'")
            info "  ${c}: ${state}"
        done
    fi
}

# ============================================================================
# Entry point
# ============================================================================

# Phase 3.6 — principal union sync + drift verification (once per run).
# The bridge hosts form ONE failover fabric (nginx claude_production cascades
# cross-host: prod workers → dev workers → 429), so every host must recognize
# every valid principal token (Rafael-Entscheid 2026-07-07, nach dem
# Energy-Phase-4-Incident: Drift machte aus einem Capacity-Failover einen
# spurious non-retryable 401). sync-principals.sh distributes the union
# (insert-only, fail-loud on conflicts, never deletes/updates auth material);
# the drift check afterwards VERIFIES convergence. Neither gates the deploy:
# a conflict/deactivation drift is a human decision, surfaced loudly here.
sync_principals="$(dirname "${BASH_SOURCE[0]}")/sync-principals.sh"
if [[ -f "$sync_principals" ]]; then
    step "Phase 3.6: principal union sync"
    if sync_out=$(DRY_RUN="$DRY_RUN" bash "$sync_principals" 2>&1); then
        while IFS= read -r line; do info "$line"; done <<< "$sync_out"
    else
        warn "Principal sync did not converge (conflict or query error) — reconcile deliberately:"
        while IFS= read -r line; do warn "  ${line}"; done <<< "$sync_out"
    fi
else
    warn "sync-principals.sh not found next to bridge-deploy.sh — skipping principal sync"
fi
drift_check="$(dirname "${BASH_SOURCE[0]}")/check-principal-drift.sh"
if [[ -x "$drift_check" || -f "$drift_check" ]]; then
    if drift_out=$(bash "$drift_check" 2>&1); then
        info "Principal parity: ${drift_out}"
    else
        warn "════════════════════════════════════════════════════════════════"
        warn "SERVICE-PRINCIPAL DRIFT between bridge hosts (see below)."
        warn "Cross-host failover WILL convert capacity errors into spurious"
        warn "401 'Invalid API key' for affected callers until reconciled."
        while IFS= read -r line; do warn "  ${line}"; done <<< "$drift_out"
        warn "════════════════════════════════════════════════════════════════"
    fi
else
    warn "check-principal-drift.sh not found next to bridge-deploy.sh — skipping parity check"
fi

case "$SERVER" in
    hetzner)
        deploy_server "hetzner"
        ;;
    server2)
        deploy_server "server2"
        ;;
    both)
        deploy_server "hetzner" || exit $?
        deploy_server "server2" || exit $?
        ;;
esac

info "=== bridge-deploy.sh finished successfully ==="
exit 0
