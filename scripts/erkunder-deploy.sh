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
    services_to_deploy=("${rest[@]}" "${group[@]}")
    info "Building shared Erkunder image once (GIT_COMMIT = host HEAD)"
    dry_rssh "$host" "cd ${REMOTE_REPO} && GIT_COMMIT=\$(git rev-parse HEAD) docker compose ${compose} build erkunder" || {
        error_ "Erkunder build failed; no Erkunder container changed"
        return 1
    }
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
