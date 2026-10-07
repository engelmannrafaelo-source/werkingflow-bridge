# Sourced by bridge-deploy.sh. Erkunder is one image / one coordinated release.
ERKUNDER_SERVICES="erkunder-ausgang erkunder-platz-1 erkunder-platz-2 erkunder-platz-3 erkunder"

# Compare actual running image provenance, not the checkout/deploy marker: a
# partial Bridge deploy may have advanced those while Erkunder stayed behind.
erkunder_unchanged() {
    local host="$1"
    rssh_run "$host" <<EOF
cd '${REMOTE_REPO}'
for svc in ${ERKUNDER_SERVICES}; do
    state=\$(docker inspect --format '{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{end}} {{index .Config.Labels "bridge.git.commit"}}' "docker-\${svc}-1") || exit 1
    read -r running health commit <<< "\$state"
    [[ "\$running" == true && "\$health" == healthy && "\$commit" =~ ^[0-9a-f]{40}$ ]] || exit 1
    git cat-file -e "\$commit^{commit}" || exit 1
    git diff --quiet "\$commit" HEAD -- src/erkunder src/sdk_parser.py src/__init__.py pyproject.toml poetry.lock .dockerignore docker/Dockerfile.erkunder docker/erkunder docker/docker-compose.yml docker/docker-compose-platform-overlay.yml scripts/erkunder-deploy.sh || exit 1
done
EOF
}

prepare_erkunder_deploy() {
    local host="$1" compose="$2" svc selected=false
    for svc in "${services_to_deploy[@]}"; do
        [[ "$svc" == erkunder* ]] && selected=true
    done
    [[ "$selected" == true ]] || return 0
    local rest=()
    for svc in "${services_to_deploy[@]}"; do
        [[ "$svc" == erkunder* ]] || rest+=("$svc")
    done
    if [[ ${#SERVICES_ARG[@]} -eq 0 && "$DRY_RUN" == false ]] && erkunder_unchanged "$host"; then
        info "Erkunder inputs unchanged and all five containers healthy — no build/restart/wait"
        services_to_deploy=("${rest[@]}")
        return 0
    fi
    # Even an explicit single place selects the group: mixed image generations
    # are not safe. Places must be ready before Leitstand startup (B1m).
    local group=()
    read -ra group <<< "$ERKUNDER_SERVICES"
    services_to_deploy=("${group[@]}" "${rest[@]}")
    info "Building shared Erkunder image once (GIT_COMMIT = host HEAD)"
    dry_rssh "$host" "cd ${REMOTE_REPO} && GIT_COMMIT=\$(git rev-parse HEAD) docker compose ${compose} build erkunder" || {
        error_ "Erkunder build failed; no Erkunder container changed"
        return 1
    }
    erkunder_wait_idle "$host" "$compose"
}

# The previous release may predate Docker healthchecks. Use the same read-only
# probe from this deploy tool, without requiring it in the old image.
erkunder_legacy_health() {
    local host="$1" svc="$2" container="$3" role=platz code
    [[ "$svc" == erkunder ]] && role=leitstand
    [[ "$svc" == erkunder-ausgang ]] && role=proxy
    code=$(base64 -w0 "$(dirname "${BASH_SOURCE[0]}")/../src/erkunder/gesundheit.py") || return 1
    if rssh "$host" "printf '%s' '$code' | base64 -d | docker exec -i '$container' python - '$role'"; then
        echo healthy
    else
        echo starting
    fi
}

# Run before ANY container recreation. The coordinator atomically closes new
# admission only once idle; missing/old API, bad response and timeout all FAIL.
erkunder_wait_idle() {
    local host="$1" compose="${2:-}" timeout="${ERKUNDER_DEPLOY_WAIT_S:-900}" code port rc
    local introduction="${ERKUNDER_DEPLOY_EINFUEHRUNG:-0}"
    if [[ "$introduction" != 0 && "$introduction" != 1 ]]; then
        error_ "ERKUNDER_DEPLOY_EINFUEHRUNG must be 0 or 1"
        return 1
    fi
    if [[ ! "$timeout" =~ ^[0-9]+$ || ${#timeout} -gt 6 ]]; then
        error_ "ERKUNDER_DEPLOY_WAIT_S must be an integer (0..999999 seconds)"
        return 1
    fi
    if [[ "$DRY_RUN" == true ]]; then
        info "[DRY-RUN] Wait up to ${timeout}s for Erkunder reports; atomically close admission; FAIL without stopping on timeout/unreachable/old API"
        if [[ "$introduction" == 1 ]]; then
            info "[DRY-RUN] ERKUNDER-EINFUEHRUNG only on POST 404: require empty /arbeit, stop Leitstand, recheck read-only volume; restart old Leitstand and FAIL if post-check fails. New protocol: switch ignored."
        else
            info "[DRY-RUN] Old protocol (POST 404) requires ERKUNDER_DEPLOY_EINFUEHRUNG=1"
        fi
        return 0
    fi
    code=$(base64 -w0 "$(dirname "${BASH_SOURCE[0]}")/../src/erkunder/deploy.py") || return 1
    port=$(PYTHONPATH="$(dirname "${BASH_SOURCE[0]}")/.." python3 -c 'from src.erkunder.gesundheit import PORTS; print(PORTS["leitstand"])') || return 1
    if rssh "$host" "printf '%s' '$code' | base64 -d | docker exec -i docker-erkunder-1 python - '$timeout' '$port'"; then
        [[ "$introduction" != 1 ]] || info "ERKUNDER-EINFUEHRUNG ignored: new deploy protocol present; normal admission gate used"
        return 0
    else
        rc=$?
    fi
    if [[ "$rc" == 3 && "$introduction" == 1 && -n "$compose" ]]; then
        erkunder_introduce_legacy "$host" "$compose" "$code"
        return $?
    fi
    error_ "Erkunder deploy gate FAIL — nothing stopped. Only POST 404 permits ERKUNDER_DEPLOY_EINFUEHRUNG=1; all other failures remain closed"
    return 1
}

# The old coordinator cannot close admission atomically. Stop only after a
# positive empty-volume check and check again before touching any place/proxy.
# Reuse the exact old image and mounts, independent of new Compose definitions.
erkunder_introduce_legacy() {
    local host="$1" compose="$2" code="$3"
    info "WARNING ERKUNDER-EINFUEHRUNG: POST /deploy/pruefen returned 404; no atomic admission lock; checking empty /arbeit before and after stop"
    rssh_run "$host" <<EOF
set -euo pipefail
cd '${REMOTE_REPO}'
image=\$(docker inspect --format '{{.Image}}' docker-erkunder-1)
printf '%s' '$code' | base64 -d | docker exec -i docker-erkunder-1 python - --legacy-idle
# Install recovery before stopping: even an ambiguous stop result needs restart.
restore_legacy() {
    rc=\$?
    if (( rc != 0 )); then
        echo 'CRITICAL ERKUNDER-EINFUEHRUNG failed; restarting old Leitstand, no group rollout' >&2
        docker compose ${compose} start erkunder || { echo 'CRITICAL old Leitstand restart FAILED' >&2; exit 2; }
    fi
    exit "\$rc"
}
trap restore_legacy EXIT
echo 'WARNING ERKUNDER-EINFUEHRUNG: pre-stop empty; stopping old Leitstand'
docker compose ${compose} stop erkunder
printf '%s' '$code' | base64 -d | docker run --rm -i --network none --volumes-from docker-erkunder-1:ro --entrypoint python "\$image" - --legacy-idle
trap - EXIT
echo 'WARNING ERKUNDER-EINFUEHRUNG: post-stop empty; introduction gate passed'
EOF
}
