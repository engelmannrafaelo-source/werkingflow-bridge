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
HETZNER_COMPOSE="docker/docker-compose.yml"
SERVER2_COMPOSE="docker/docker-compose-prod.yml"
HEALTH_TIMEOUT=120
MIN_FREE_KB=$(( 5 * 1024 * 1024 ))  # 5 GB in KiB

# Hetzner: service → container name
HETZNER_SVC_nginx="eco-wrapper-lb"
HETZNER_SVC_worker1="eco-wrapper-worker1"
HETZNER_SVC_worker2="eco-wrapper-worker2"
HETZNER_SVC_worker3="eco-wrapper-worker3"
HETZNER_SVC_worker4="eco-wrapper-worker4"
HETZNER_SVC_privacy_service="eco-privacy-pdf-service"
HETZNER_SVC_metrics_reader="eco-wrapper-metrics-reader"
HETZNER_ALL="nginx worker1 worker2 worker3 worker4 privacy-service metrics-reader"
HETZNER_NEEDS_BUILD="worker1 worker2 worker3 worker4 privacy-service metrics-reader"

# Server-2: service → container name (use _ not - for var names)
SERVER2_SVC_nginx="eco-prod-lb"
SERVER2_SVC_worker_prod="eco-prod-worker"
SERVER2_SVC_privacy_prod="eco-prod-privacy"
SERVER2_SVC_metrics_reader_prod="eco-prod-metrics-reader"
SERVER2_ALL="nginx worker-prod privacy-prod metrics-reader-prod"
SERVER2_NEEDS_BUILD="worker-prod privacy-prod metrics-reader-prod"

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
    rssh "$host" "cd ${REMOTE_REPO} && docker compose -f ${compose} config --quiet" > /dev/null
    info "Compose OK"

    # nginx config syntax: envsubst with EXACT same variable list as the container uses,
    # then nginx -t inside nginx:alpine with --add-host for all internal upstream hostnames
    # (needed because nginx -t resolves upstream hosts; they only exist inside the compose
    # network at runtime, not in this test container).
    local nginx_conf envsubst_vars add_hosts
    if [[ "$compose" == *"prod"* ]]; then
        # Server-2: envsubst '$BRIDGE_PRIMARY_HOST $BRIDGE_ID', upstreams: worker-prod, metrics-reader-prod
        nginx_conf="docker/nginx-prod.conf"
        envsubst_vars='$BRIDGE_PRIMARY_HOST $BRIDGE_ID'
        add_hosts="--add-host=worker-prod:127.0.0.1 --add-host=metrics-reader-prod:127.0.0.1"
    else
        # Hetzner: envsubst '$BRIDGE_PROD_HOST $BRIDGE_ID', upstreams: worker1-4, metrics-reader
        nginx_conf="docker/nginx.conf"
        envsubst_vars='$BRIDGE_PROD_HOST $BRIDGE_ID'
        add_hosts="--add-host=worker1:127.0.0.1 --add-host=worker2:127.0.0.1 --add-host=worker3:127.0.0.1 --add-host=worker4:127.0.0.1 --add-host=metrics-reader:127.0.0.1"
    fi
    info "nginx config syntax check (${nginx_conf})..."
    local nginx_output
    nginx_output=$(rssh "$host" "
        cd ${REMOTE_REPO}
        BRIDGE_PROD_HOST=127.0.0.1 BRIDGE_PRIMARY_HOST=127.0.0.1 BRIDGE_ID=validate-probe \\
            envsubst '${envsubst_vars}' \\
            < ${nginx_conf} > /tmp/bridge-nginx-check.conf 2>&1
        if docker run --rm \\
            ${add_hosts} \\
            --tmpfs /var/log/nginx \\
            -v /tmp/bridge-nginx-check.conf:/etc/nginx/nginx.conf:ro \\
            -v ${REMOTE_REPO}/docker/lua:/etc/nginx/lua:ro \\
            openresty/openresty:1.27.1.1-alpine sh -c 'touch /var/log/nginx/access.jsonl && openresty -t -c /etc/nginx/nginx.conf' 2>&1; then
            echo __NGINX_OK__
        else
            echo __NGINX_FAIL__
        fi
        rm -f /tmp/bridge-nginx-check.conf
    " 2>&1) || true

    while IFS= read -r line; do info "  nginx-t: ${line}"; done <<< "$nginx_output"
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

    # Build if this service has a Dockerfile
    if service_needs_build "$svc" "$build_list"; then
        info "Building ${svc}..."
        dry_rssh "$host" "cd ${REMOTE_REPO} && docker compose -f ${compose} build --no-cache ${svc} 2>&1" || {
            error_ "Build failed for ${svc} on ${host}"
            return 1
        }
        info "Build OK"
    else
        info "${svc} uses pre-built image — skipping build"
    fi

    # Recreate — NEVER --remove-orphans
    info "Recreating ${svc}..."
    dry_rssh "$host" "cd ${REMOTE_REPO} && docker compose -f ${compose} up -d --no-deps --force-recreate ${svc} 2>&1" || {
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

    if [[ "$DRY_RUN" == "true" ]]; then
        info "[DRY-RUN] Would POST ${url}/v1/research query='smoke test' depth='quick'"
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

    info "Sending smoke test request to ${url}/v1/research ..."
    # All request + response validation in ONE Python call — avoids heredoc injection
    # of multi-line/control-char response bodies into a second script.
    # Pass sensitive values via env vars, not inline string interpolation.
    local smoke_out
    smoke_out=$(SMOKE_URL="${url}" \
        SMOKE_API_KEY="${api_key}" \
        SMOKE_EXTRA_HEADER="${extra_header:-}" \
        python3 - <<'PYEOF'
import os, sys, json, requests

url            = os.environ["SMOKE_URL"]
api_key        = os.environ["SMOKE_API_KEY"]
extra_header   = os.environ.get("SMOKE_EXTRA_HEADER", "")

headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
    "X-Client-ID": "bridge-deploy/smoke-test",
}
if extra_header:
    k, v = extra_header.split(": ", 1)
    headers[k.strip()] = v.strip()

try:
    r = requests.post(
        f"{url}/v1/research",
        headers=headers,
        json={"query": "smoke test", "depth": "quick", "max_turns": 5},
        timeout=120,
    )
except requests.exceptions.Timeout:
    print("SMOKE_FAIL: request timed out after 120s", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"SMOKE_FAIL: request error: {e}", file=sys.stderr)
    sys.exit(1)

print(f"HTTP {r.status_code}")
if r.status_code != 200:
    print(f"SMOKE_FAIL: HTTP {r.status_code}", file=sys.stderr)
    print(f"Body (first 500): {r.text[:500]}", file=sys.stderr)
    sys.exit(1)

try:
    d = r.json()
except Exception as e:
    print(f"SMOKE_FAIL: response not valid JSON: {e}", file=sys.stderr)
    print(f"Body (first 500): {r.text[:500]}", file=sys.stderr)
    sys.exit(1)

status = d.get("status")
if status != "success":
    print(f"SMOKE_FAIL: status={status!r}, expected 'success'", file=sys.stderr)
    sys.exit(1)

content = d.get("content", "")
https_count = content.count("https://")
if https_count < 1:
    print(f"SMOKE_FAIL: content has {https_count} https:// URLs, expected >=1", file=sys.stderr)
    sys.exit(1)

exec_time = d.get("execution_time_seconds", "?")
print(f"SMOKE_OK: status=success, https_count={https_count}, exec_time={exec_time}s")
print(f"Content preview: {content[:400]}")
PYEOF
    ) 2>&1

    while IFS= read -r line; do info "  smoke: ${line}"; done <<< "$smoke_out"

    if ! echo "$smoke_out" | grep -q 'SMOKE_OK:'; then
        error_ "Smoke test FAILED for ${label} (see output above)"
        return 1
    fi

    info "Smoke test PASSED for ${label}"
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
            rssh "$host" "cd ${REMOTE_REPO} && docker compose -f ${compose} build --no-cache ${svc} 2>&1" || {
                error_ "CRITICAL: rollback build failed for ${svc}"
                failed=true
                continue
            }
        fi

        rssh "$host" "cd ${REMOTE_REPO} && docker compose -f ${compose} up -d --no-deps --force-recreate ${svc} 2>&1" || {
            error_ "CRITICAL: rollback recreate failed for ${svc}"
            failed=true
            continue
        }

        # Short health wait during rollback
        local deadline=$(( $(date +%s) + 60 ))
        local status=""
        while (( $(date +%s) < deadline )); do
            status=$(rssh "$host" \
                "docker inspect --format '{{.State.Health.Status}}' '${container}' 2>/dev/null || echo 'none'")
            [[ "$status" == "healthy" ]] && break
            sleep 5
        done
        if [[ "$status" != "healthy" ]]; then
            error_ "CRITICAL: rollback health failed for ${container} (status: ${status})"
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
        phase_smoke_test "hetzner" "${AI_BRIDGE_URL:-http://${HETZNER_HOST}:8000}" "" || {
            error_ "Smoke test failed for hetzner — rolling back"
            if [[ ${#DEPLOYED_SERVICES[@]} -gt 0 ]]; then
                phase_rollback "$host" "$compose" "$ROLLBACK_SHA" "$build_list" "${DEPLOYED_SERVICES[@]}"
                local rc=$?
                (( rc == 2 )) && return 2
            fi
            return 1
        }
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
