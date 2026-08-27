#!/usr/bin/env bash
# bridge-deploy.sh — Atomic idempotent multi-server Bridge deployment
#
# Usage: bridge-deploy.sh <server> [<service>...] [--dry-run] [--ack-foreign] [--force-prod-ahead]
#   server        = hetzner | server2 | both | prod-workers
#   service       = optional, default = all services for the server
#
#   prod-workers = the ADR-0009 worker-host (168.119.178.70): LLM worker
#     containers only, no nginx-LB, no Postgres, no platform-api, no public
#     smoke target (see docs/adr/0009-bridge-worker-host-separation.md).
#     Deliberately NOT part of `both` — it never runs implicitly alongside a
#     routine hetzner+server2 deploy.
#   --ack-foreign = proceed although this deploy also ships commits authored by
#                   other sessions (see phase_foreign_commit_gate). Without it,
#                   such a deploy ABORTS before touching the target.
#
# EXIT CODES:
#   0 = success (or dry-run completed)
#   1 = deployment failure (rollback attempted and succeeded), or the
#       foreign-commit gate aborted BEFORE any change was made
#   2 = critical failure (rollback itself failed — manual intervention required)
set -euo pipefail

# ============================================================================
# Config
# ============================================================================
HETZNER_HOST="49.12.72.66"
SERVER2_HOST="178.104.178.79"
# ADR-0009: worker-only host, addressed over Tailscale (tailnet hostname
# prod-workers-1) — no public function, not a customer-facing bridge.
WORKERHOST_HOST="100.93.143.105"
SSH_KEY="/root/.ssh/id_ed25519"
SSH_BASE_OPTS="-i ${SSH_KEY} -o ConnectTimeout=10 -o StrictHostKeyChecking=no -o BatchMode=yes"
REMOTE_REPO="/root/werkingflow-bridge"
# SHA the running images were built from, written by a finished deploy. Untracked
# on purpose (pre-flight allows untracked files) — it is host state, not source.
DEPLOYED_SHA_FILE="${REMOTE_REPO}/.bridge-deployed-sha"
# Per-SERVICE release manifest (commit + image ID per container), written by a
# finished deploy. Same "host state, not source" rule as DEPLOYED_SHA_FILE —
# untracked, never captured back into the repo. See write_release_manifest()
# for what it records and bridge-drift-check.sh for how it is used.
RELEASE_MANIFEST_FILE="${REMOTE_REPO}/.bridge-release-manifest.json"
HETZNER_COMPOSE="-f docker/docker-compose.yml -f docker/docker-compose-platform-overlay.yml"
SERVER2_COMPOSE="-f docker/docker-compose-prod.yml -f docker/docker-compose-prod-platform.yml"
WORKERHOST_COMPOSE="-f docker/docker-compose-worker-host.yml"
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
HETZNER_SVC_metrics_reader="wt-wrapper-metrics-reader"
HETZNER_SVC_platform_api="wt-platform-api"
# platform-api is deployed BEFORE nginx so the upstream resolves when nginx
# restarts. nginx is last so the new routing is live only after platform-api
# is healthy.
# privacy-service REMOVED here 2026-08-27: commit 2e273bf dropped the
# wt-privacy-pdf-service container from docker-compose.yml (dev now routes
# smart-anonymize/document-convert to the GPU instance, ~40x faster), but left
# it in these lists. `docker compose build privacy-service` then died with
# "no such service" in Phase 4 and auto-rolled-back every healthy service
# ahead of it — a deploy that could never succeed. The smoke profile is
# unaffected: it probes PRIVACY_SERVICE_URL, not this container.
HETZNER_ALL="platform-api nginx worker1 worker2 worker3 worker4 metrics-reader"
HETZNER_NEEDS_BUILD="platform-api nginx worker1 worker2 worker3 worker4 metrics-reader"

# Server-2: service → container name (use _ not - for var names)
SERVER2_SVC_nginx="wt-prod-lb"
# Replaces the old single worker-prod after commit ad14f82 split it into
# two-account workers (sahori + kurt) for ~2x rate-limit capacity.
SERVER2_SVC_worker_sahori="wt-prod-worker-sahori"
SERVER2_SVC_worker_kurt="wt-prod-worker-kurt"
# Pool-Erweiterung 2→4 (2026-08-18): die beiden restlichen Partner-Accounts.
SERVER2_SVC_worker_coach="wt-prod-worker-coach"
SERVER2_SVC_worker_erk="wt-prod-worker-erk"
# privacy-prod REMOVED 2026-06-26: Flair (~13GB) OOM'd this 7GB host; prod now
# routes smart-anonymize to the dev-bridge privacy service over Tailscale.
SERVER2_SVC_metrics_reader_prod="wt-prod-metrics-reader"
SERVER2_SVC_postgres_prod="bridge-postgres-prod"

# Bookkeeping DB for the migration gate (Phase 3.7). Both hosts run a container
# literally named "bridge-postgres-prod" — dev (hetzner) and prod (server2) are
# DIFFERENT machines with the SAME container name, so the name alone proves
# nothing about which database is being read. The gate is always addressed with
# the host it belongs to.
HETZNER_DB_CONTAINER="bridge-postgres-prod"
SERVER2_DB_CONTAINER="bridge-postgres-prod"
BRIDGE_DB_USER="bridge"
BRIDGE_DB_NAME="bridge"
SERVER2_SVC_platform_api="wt-prod-platform-api"
# postgres-prod zuerst (DB vor platform-api), platform-api vor nginx (Upstream-Resolve).
SERVER2_ALL="postgres-prod platform-api nginx worker-sahori worker-kurt worker-coach worker-erk metrics-reader-prod"
# nginx now BUILDS from Dockerfile.nginx-lb (OpenResty+Lua) — the same image as
# primary (ADR-0006 B/C). It was a pre-built nginx:alpine before the unification.
SERVER2_NEEDS_BUILD="platform-api nginx worker-sahori worker-kurt worker-coach worker-erk metrics-reader-prod"

# Worker-host (ADR-0009): service -> container name. Distinct container-name
# prefix (wt-worker-host-*) from server2's wt-prod-worker-* on purpose — the
# two are never the same container, even in logs/docker ps on different hosts,
# so provenance is unambiguous during the (still local-worker) cutover window
# when both may briefly exist.
WORKERHOST_SVC_worker_sahori="wt-worker-host-sahori"
WORKERHOST_SVC_worker_kurt="wt-worker-host-kurt"
WORKERHOST_SVC_worker_coach="wt-worker-host-coach"
WORKERHOST_SVC_worker_erk="wt-worker-host-erk"
WORKERHOST_ALL="worker-sahori worker-kurt worker-coach worker-erk"
WORKERHOST_NEEDS_BUILD="worker-sahori worker-kurt worker-coach worker-erk"

# State (reset per server in deploy_server)
ROLLBACK_SHA=""
DEPLOYED_SERVICES=()
# Phase 0 inspects THIS checkout, not a host — one verdict covers `both`.
TOOLING_GATE_DONE="false"

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

# Acknowledge that this deploy also ships commits authored by someone else
# (see phase_foreign_commit_gate). Deliberately NOT defaulting to true and
# deliberately not remembered anywhere: the whole point is that the decision is
# made per deploy, by someone who checked.
ACK_FOREIGN=false

# Consciously override the dev-first order gate (phase_prod_order_gate).
# Requires a typed confirmation at a TTY on top of the flag — a script or cron
# can never take this path silently.
FORCE_PROD_AHEAD=false

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --ack-foreign) ACK_FOREIGN=true ;;
        --force-prod-ahead) FORCE_PROD_AHEAD=true ;;
        hetzner|server2|both|prod-workers) SERVER="$arg" ;;
        *) SERVICES_ARG+=("$arg") ;;
    esac
done

if [[ -z "$SERVER" ]]; then
    echo "Usage: bridge-deploy.sh <hetzner|server2|both|prod-workers> [service...] [--dry-run] [--ack-foreign] [--force-prod-ahead]" >&2
    echo "  --ack-foreign      proceed even though the deploy ships commits by other authors" >&2
    echo "  --force-prod-ahead override the dev-first order gate (typed TTY confirmation required)" >&2
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
    hetzner)      acquire_deploy_lock "hetzner" 210 ;;
    server2)      acquire_deploy_lock "server2" 211 ;;
    both)         acquire_deploy_lock "hetzner" 210; acquire_deploy_lock "server2" 211 ;;
    prod-workers) acquire_deploy_lock "prod-workers" 212 ;;
esac

# ============================================================================
# Phase 0: Tooling freshness (read-only, local — runs once per invocation)
# ============================================================================
# Phase 2 fast-forwards the HOST to origin/develop; Phase 5 judges the result
# with the bridge_smoke.py sitting next to THIS file. Nothing kept those two in
# step, so a stale checkout could fail a healthy Bridge and auto-roll-back good
# commits. Delegated to check-tooling-freshness.sh, which is unit-tested in
# tests/deploy/test_tooling_freshness.sh — see there for the exact policy
# (behind = fatal, ahead/dirty = warning).
phase_tooling_freshness_gate() {
    [[ "$TOOLING_GATE_DONE" == "true" ]] && return 0
    step "Phase 0: Tooling freshness (local checkout)"

    local checker
    checker="$(dirname "${BASH_SOURCE[0]}")/check-tooling-freshness.sh"
    if [[ ! -x "$checker" ]]; then
        error_ "ABORTED: freshness checker missing or not executable: ${checker}"
        error_ "  It is the only thing standing between a stale smoke and an auto-rollback"
        error_ "  of healthy code. Restore it rather than deploying unguarded."
        return 1
    fi

    "$checker" || return 1

    TOOLING_GATE_DONE="true"
    return 0
}

# ============================================================================
# Phase 0.5: Dev-first order gate — prod never runs ahead of dev
# ============================================================================
# Rafael (2026-08-20): the prod bridge was found running 5 commits AHEAD of the
# dev bridge — code reached paying customers before it had ever run on dev.
# This gate enforces the order: a deploy to a PROD target (server2,
# prod-workers) is only allowed when the target ref (origin/develop) is already
# RUNNING on the dev bridge. Proof is hetzner's .bridge-deployed-sha (written
# only by a finished deploy) — NOT the checkout HEAD, which can be
# fast-forwarded out-of-band without any container containing that code.
# "both" passes naturally: the hetzner leg deploys first and writes the marker
# the server2 leg then reads.
#
# Deliberately NO env-var bypass: the only exception path is --force-prod-ahead
# plus a typed confirmation at a TTY. An emergency is a human decision made
# consciously, not a flag a script sets.
phase_prod_order_gate() {
    local server_name="$1"
    case "$server_name" in server2|prod-workers) ;; *) return 0 ;; esac
    step "Phase 0.5: Dev-first order gate (${server_name})"

    local repo_dir target_sha dev_sha_raw dev_sha
    repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    # Target = what Phase 2 will fast-forward the host to: origin/develop.
    if ! git -C "$repo_dir" fetch --quiet origin develop; then
        error_ "ABORTED: git fetch origin develop failed — cannot determine the target ref."
        return 1
    fi
    target_sha="$(git -C "$repo_dir" rev-parse origin/develop)"

    dev_sha_raw="$(rssh "$HETZNER_HOST" "cat ${DEPLOYED_SHA_FILE} 2>/dev/null" | tr -d '[:space:]' || true)"
    if [[ ! "$dev_sha_raw" =~ ^[0-9a-f]{7,40}$ ]]; then
        error_ "ABORTED: cannot read the dev bridge's deployed SHA (${HETZNER_HOST}:${DEPLOYED_SHA_FILE})."
        error_ "  Without proof of what dev actually RUNS, this prod deploy is unverifiable."
        error_ "  Deploy hetzner first (which writes the marker), then retry."
        return 1
    fi
    dev_sha="$(git -C "$repo_dir" rev-parse --verify "${dev_sha_raw}^{commit}" 2>/dev/null || true)"
    if [[ -z "$dev_sha" ]]; then
        error_ "ABORTED: dev bridge's deployed SHA ${dev_sha_raw} is unknown to this repo (even after fetch)."
        error_ "  That itself is a provenance problem — investigate before deploying prod."
        return 1
    fi

    if git -C "$repo_dir" merge-base --is-ancestor "$target_sha" "$dev_sha"; then
        info "OK: target ${target_sha:0:9} already runs on the dev bridge (deployed: ${dev_sha:0:9})."
        return 0
    fi

    local behind
    behind="$(git -C "$repo_dir" rev-list --count "${dev_sha}..${target_sha}" 2>/dev/null || echo '?')"
    error_ "BLOCKED by the dev-first order gate: the target ref does NOT run on the dev bridge yet."
    error_ "  Target (origin/develop) : ${target_sha}"
    error_ "  Dev bridge runs         : ${dev_sha} (${behind} commit(s) behind the target)"
    error_ "  The order is deliberate (Rafael, 2026-08-20): deploy hetzner first, verify,"
    error_ "  THEN prod — or run 'bridge-deploy.sh both', which does exactly that."
    if [[ "$DRY_RUN" == "true" ]]; then
        warn "[DRY-RUN] A real run would ABORT here."
        return 0
    fi
    if [[ "$FORCE_PROD_AHEAD" == "true" ]]; then
        if [[ ! -t 0 ]]; then
            error_ "--force-prod-ahead requires a TTY (typed confirmation) — no silent bypass."
            return 1
        fi
        warn "--force-prod-ahead set: type exactly PROD-AHEAD to consciously override the gate."
        local reply
        read -r -p "Confirmation: " reply
        if [[ "$reply" == "PROD-AHEAD" ]]; then
            warn "Gate consciously overridden (--force-prod-ahead, confirmed at TTY)."
            return 0
        fi
        error_ "Wrong confirmation — aborting."
        return 1
    fi
    return 1
}

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

    # Provenance: what code do the RUNNING containers actually contain?
    # A checkout says what was pulled, not what was deployed — those drift apart the
    # moment someone pulls without deploying, and until images carried a commit label
    # the difference was only reconstructable from deploy logs and container age.
    # (Observed 2026-07-31 on server2: checkout 2a25c64, containers still running
    # 493dc59, unnoticed for days.) Reported, never fatal: images built before the
    # label existed have none, and a genuinely drifted host is precisely what the
    # deploy about to run is going to fix.
    info "Image provenance check..."
    local head_sha provenance
    head_sha=$(rssh "$host" "cd ${REMOTE_REPO} && git rev-parse HEAD")
    provenance=$(rssh "$host" 'docker inspect --format "{{.Name}} {{index .Config.Labels \"bridge.git.commit\"}}" $(docker ps --filter name=wt- -q) 2>/dev/null' || true)
    local unlabelled=0 drifted=0
    while read -r cname csha; do
        [[ -z "$cname" ]] && continue
        cname="${cname#/}"
        if [[ -z "$csha" ]]; then
            (( unlabelled++ ))
        elif [[ "$csha" != "$head_sha" ]]; then
            warn "  DRIFT: ${cname} runs ${csha:0:7}, checkout is ${head_sha:0:7}"
            (( drifted++ ))
        fi
    done <<< "$provenance"
    if (( drifted > 0 )); then
        warn "Provenance: ${drifted} container(s) run code other than the checkout"
    elif (( unlabelled > 0 )); then
        info "Provenance: ${unlabelled} container(s) predate the commit label — unverifiable until rebuilt"
    else
        info "Provenance OK: all running containers match ${head_sha:0:7}"
    fi
}

# ============================================================================
# Phase 2: Code update — sets global ROLLBACK_SHA
# ============================================================================
# ============================================================================
# Phase 2a: Foreign-commit gate — never ship someone else's work unnoticed
# ============================================================================
# `git pull --ff-only origin develop` deploys the TIP of develop, i.e. every
# commit on it — not just the ones the person running the deploy wrote. Several
# sessions push to this branch in parallel, so a deploy routinely carries other
# people's commits to production, silently. Observed 2026-07-30: a deploy of
# four logging/observability fixes would also have shipped another session's
# billing-pricing and registration changes; it was noticed only because an
# unfamiliar rollback SHA happened to catch the eye.
#
# Nothing here is git's fault — later commits build on earlier ones, so the
# coupling is real and cherry-picking around it would put a state on production
# that exists in no branch. The fix is not to decouple but to make the coupling
# VISIBLE and require a decision: fail fast, list exactly whose commits ride
# along, and continue only on an explicit --ack-foreign.
#
# Deployer identity = local `git config user.name` (the same identity that
# authors commits here). Unknown identity or an unreadable log means we cannot
# make the judgement — and then we ask rather than assume, because assuming
# "all mine" is the failure mode this gate exists to prevent.
phase_foreign_commit_gate() {
    local host="$1" checkout_sha="$2" to_sha="$3"

    # Compare against what is RUNNING, not against the checkout. Those differ
    # whenever someone fast-forwards the checkout without deploying — which is
    # not hypothetical: on server2 the checkout stood two commits ahead of the
    # images, so a checkout-based gate reported "clear" while the very commits
    # it exists to surface were about to be built in. Fall back to the checkout
    # only when no marker exists yet, and say so.
    # Resolve the marker through git rev-parse: SHAs must be compared as commits,
    # not as strings. A short SHA in the marker (hand-seeded, or copied from a
    # log line) would otherwise never equal the 40-char checkout HEAD and fake a
    # permanent "checkout is ahead" warning — a guard that cries wolf gets
    # ignored, which is the one failure mode it cannot afford.
    local from_sha raw_marker
    raw_marker="$(rssh "$host" "cat ${DEPLOYED_SHA_FILE} 2>/dev/null || true" | tr -d '[:space:]')"
    from_sha=""
    if [[ -n "$raw_marker" ]]; then
        from_sha="$(rssh "$host" "cd ${REMOTE_REPO} && git rev-parse --verify '${raw_marker}^{commit}' 2>/dev/null || true" | tr -d '[:space:]')"
        if [[ -z "$from_sha" ]]; then
            error_ "Deployed-SHA marker on ${host} is '${raw_marker}', which is not a commit in ${REMOTE_REPO}."
            error_ "  Refusing to guess what is running. Fix or delete ${DEPLOYED_SHA_FILE} and re-run."
            return 1
        fi
    fi
    if [[ -z "$from_sha" ]]; then
        from_sha="$checkout_sha"
        warn "Foreign-commit gate: no deployed-SHA marker on ${host} yet — falling back to the"
        warn "  checkout HEAD (${checkout_sha:0:7}). If the checkout was moved without deploying,"
        warn "  commits already in it are invisible here. The marker is written after this deploy."
    elif [[ "$from_sha" != "$checkout_sha" ]]; then
        warn "Foreign-commit gate: checkout (${checkout_sha:0:7}) is ahead of what is deployed"
        warn "  (${from_sha:0:7}) — someone pulled without deploying. Comparing against the"
        warn "  DEPLOYED state, so those commits are included below."
    fi

    local deployer
    deployer="$(git config user.name 2>/dev/null || true)"

    local log_out
    if ! log_out=$(rssh "$host" "cd ${REMOTE_REPO} && git log --no-merges --format='%h|%an|%s' ${from_sha}..${to_sha}"); then
        error_ "Cannot list commits ${from_sha}..${to_sha} on ${host} — refusing to deploy blind."
        error_ "  A deploy that cannot say WHAT it ships is exactly what this gate prevents."
        return 1
    fi

    local foreign=()
    local own_count=0
    while IFS='|' read -r sha author subject; do
        [[ -z "$sha" ]] && continue
        if [[ -n "$deployer" && "$author" == "$deployer" ]]; then
            own_count=$((own_count + 1))
        else
            foreign+=("${sha}  ${author}  ${subject}")
        fi
    done <<< "$log_out"

    if [[ ${#foreign[@]} -eq 0 ]]; then
        info "Foreign-commit gate: ${own_count} commit(s), all authored by '${deployer:-<unset>}' — clear"
        return 0
    fi

    warn "This deploy also ships ${#foreign[@]} commit(s) NOT authored by '${deployer:-<unset, cannot classify>}':"
    local line
    for line in "${foreign[@]}"; do
        warn "    ${line}"
    done
    [[ ${own_count} -gt 0 ]] && warn "  (plus ${own_count} of your own)"

    if [[ "$ACK_FOREIGN" == "true" ]]; then
        warn "  --ack-foreign given → proceeding with the commits listed above."
        return 0
    fi

    error_ "ABORTED: refusing to ship other sessions' commits without an explicit decision."
    error_ "  These are not necessarily unfinished — but only a human knows whether they are"
    error_ "  ready for THIS environment. Verify with their author, then re-run with:"
    error_ "      $(basename "${BASH_SOURCE[0]}") <server> --ack-foreign"
    error_ "  Nothing was changed on ${host}: no pull, no build, no container touched."
    error_ "  Still deployed: ${from_sha:0:7}   (checkout sits at ${checkout_sha:0:7})"
    return 1
}

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

    phase_foreign_commit_gate "$host" "${ROLLBACK_SHA}" "${remote_sha}" || return 1

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

    # ADR-0009 worker-host: no nginx service in this compose at all (workers
    # only) — the shared nginx.conf lives on hetzner/server2, not here. Compose
    # syntax above is the whole of Phase 3 for this topology.
    if [[ "$compose" == *"worker-host"* ]]; then
        info "No nginx service on this host (worker-host topology) — nginx validation N/A"
        return 0
    fi

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
    local nginx_conf upstreams_conf worker_map_conf envsubst_vars add_hosts
    nginx_conf="docker/nginx.conf"
    envsubst_vars='$BRIDGE_BACKUP_HOST $BRIDGE_ID'
    if [[ "$compose" == *"prod"* ]]; then
        upstreams_conf="docker/upstreams-prod.conf"
        worker_map_conf="docker/worker-map-prod.conf"
    else
        upstreams_conf="docker/upstreams-primary.conf"
        worker_map_conf="docker/worker-map-primary.conf"
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
    -v ${REMOTE_REPO}/${worker_map_conf}:/tmp/worker-map.conf:ro \
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

    # Build if this service has a Dockerfile.
    # GIT_COMMIT is resolved ON THE HOST (escaped \$) at build time, not passed in from
    # here: the checkout is already at the intended commit in both paths — after the pull
    # in phase_code_update, and after the `git reset --hard` in phase_rollback. That makes
    # the image label true by construction instead of by a variable someone must thread
    # through correctly.
    if service_needs_build "$svc" "$build_list"; then
        info "Building ${svc}..."
        dry_rssh "$host" "cd ${REMOTE_REPO} && GIT_COMMIT=\$(git rev-parse HEAD) docker compose ${compose} build --no-cache ${svc} 2>&1" || {
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
    # going live. Endpoint coverage is enforced by the bridge_smoke_coverage.py
    # pre-flight above — which, until 2026-07-31, this comment claimed while the
    # validator was called from nowhere.
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
        # SMOKE_CAPACITY = exit 0, but NOT a clean pass: one or more endpoints
        # were refused by the account-capacity gate before reaching the
        # deployed code, so they stayed UNVERIFIED. Deliberately not a
        # rollback (every build gets that same 429 while the Anthropic
        # accounts sit at their weekly wall), but it must never read as green.
        if grep -q '^SMOKE_CAPACITY:' <<< "$smoke_out"; then
            warn "Smoke test PASSED for ${label} WITH UNVERIFIED ENDPOINTS (pool capacity unavailable)"
            warn "  Re-run the affected probes once the pool recovers — see the SMOKE_CAPACITY line above."
            return 0
        fi
        # SMOKE_DEPENDENCY = exit 0, same contract as SMOKE_CAPACITY: the
        # privacy-pdf-service these endpoints proxy to was proven unreachable,
        # so they stayed UNVERIFIED. Deliberately not a rollback — the previous
        # image proxies to the same PRIVACY_SERVICE_URL and fails identically,
        # so rolling back only swaps a good image out during an unrelated
        # outage (2026-08-01). It must never read as green either.
        if grep -q '^SMOKE_DEPENDENCY:' <<< "$smoke_out"; then
            warn "Smoke test PASSED for ${label} WITH UNVERIFIED ENDPOINTS (privacy-service unreachable)"
            warn "  This is a DEPENDENCY outage, not a regression in this build. Fix the"
            warn "  privacy service, then re-run the affected probes — see the SMOKE_DEPENDENCY line above."
            return 0
        fi
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

def is_pool_exhausted(resp) -> bool:
    """True iff the router REFUSED this call because no account was eligible.

    First-party signal: @pool_exhausted_response stamps X-Bridge-Capacity, so we
    read the router's own verdict instead of re-deriving eligibility here.
    """
    if resp.status_code != 429:
        return False
    if resp.headers.get("X-Bridge-Capacity") == "pool_exhausted":
        return True
    try:
        return (resp.json().get("error") or {}).get("bridge_type") == "pool_exhausted"
    except Exception:
        return False


def eligible_account_count() -> int:
    """How many accounts the router could currently route to (-1 = unknown).

    Mirrors pick_weighted_account() in docker/lua/pool_router.lua. Used ONLY to
    diagnose a <2-unique-worker result — never to decide the happy path — so a
    drift between the two costs a wrong diagnosis, not a wrong pass/fail on a
    healthy pool.
    """
    try:
        r = requests.get(f"{url}/v1/metrics/account-pool-state", headers=headers, timeout=20)
        accounts = (r.json() or {}).get("accounts") or {}
    except Exception:
        return -1
    return sum(
        1 for a in accounts.values()
        if a.get("available")
        and (a.get("cooldown_remaining_s") or 0) == 0
        and (a.get("effective_cap_tokens") or 0) > (a.get("current_in_flight_tokens") or 0)
        and (a.get("weekly_percent") or 0) < 96
    )


workers_hit = []
refused = 0
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
    if is_pool_exhausted(r):
        refused += 1
        note = " (router refused: pool exhausted — no worker reached)"
    elif r.status_code == 429:
        note = " (429 worker rate-limited — counts as hit)"
    else:
        note = ""
    print(f"  call {i+1}/8: worker={worker} HTTP {r.status_code}{note}")
    if worker and worker != "unknown":
        workers_hit.append(worker)

unique_workers = set(workers_hit)
distribution   = dict(Counter(workers_hit))
print(f"Distribution: {len(unique_workers)}/4 unique workers — {distribution}"
      + (f", {refused} refused (pool exhausted)" if refused else ""))

if len(unique_workers) >= 2:
    print(f"DIST_OK: {len(unique_workers)} unique workers hit — {distribution}")
    sys.exit(0)

# Fewer than 2 workers hit. That is only a ROUTER defect if the router actually
# had something to spread across. With 0 or 1 eligible account, sending every
# call to the one account (or refusing all of them) is the correct behaviour —
# failing here would report infrastructure STATE as a code defect, the same
# confusion that made the deploy smoke roll back good builds (f32c801).
eligible = eligible_account_count()

# A refusal is the router stating, at call time, that nothing was eligible.
# `eligible` is sampled seconds later and the pool moves fast (a single account
# can appear and vanish between calls — observed live 2026-07-30), so refusals
# are the authoritative signal and the later count is reported as context, not
# used to contradict them. Deciding on that race would produce exactly the kind
# of flaky red the smoke fix (f32c801) removed.
if refused or (0 <= eligible < 2):
    facts = [f"{len(unique_workers)} worker(s) hit"]
    if refused:
        facts.append(f"{refused}/8 refused by the router (pool_exhausted)")
    facts.append(f"{eligible} account(s) eligible when sampled afterwards"
                 if eligible >= 0 else "eligibility unknown (state probe failed)")
    print(
        f"DIST_SKIP: load-spreading is not assertable right now — "
        f"{'; '.join(facts)}. With no spare account, refusing calls or "
        f"concentrating them on the only account with headroom is the CORRECT "
        f"behaviour, not a router defect. Re-run once >=2 accounts have headroom.",
        # STDOUT, not stderr: the caller captures only stdout into $dist_out
        # (the `) 2>&1` sits outside the command substitution), and it decides
        # pass/skip/fail by grepping that variable. Emitting the marker on
        # stderr meant the skip was printed into the deploy log but invisible to
        # the check — so the test still reported FAILED on both of today's
        # rollouts. The marker has to live on the stream the caller reads.
    )
    sys.exit(0)

print(
    f"DIST_FAIL: only {len(unique_workers)} unique worker(s) hit across 8 calls "
    f"(need >=2): {sorted(unique_workers)}, while {eligible} account(s) were "
    f"eligible and none were refused. Pool router is not distributing load — "
    f"check pool_router.lua tie-breaker logic.",
    file=sys.stderr,
)
sys.exit(1)
PYEOF
    ) 2>&1
    rc_dist=$?

    while IFS= read -r line; do info "  dist: ${line}"; done <<< "$dist_out"

    # DIST_SKIP = the assertion was not applicable (pool exhausted / <2 eligible
    # accounts). Not a pass and not a failure: reported loudly, never silently.
    if echo "$dist_out" | grep -q 'DIST_SKIP:'; then
        warn "Distribution test NOT ASSERTABLE — pool has no spare accounts right now (see dist: lines above)"
        return 0
    fi

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
            # git reset --hard ran above, so HEAD on the host is the rollback SHA:
            # the rebuilt image gets labelled with the code it actually contains.
            rssh "$host" "cd ${REMOTE_REPO} && GIT_COMMIT=\$(git rev-parse HEAD) docker compose ${compose} build --no-cache ${svc} 2>&1" || {
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
# Phase 3.7: migration gate.
#
# This script deliberately does NOT apply migrations — a schema change on a
# production database stays a conscious, separate act. But deploying code that
# expects a column the database does not have is exactly the failure this gate
# exists to prevent, so the deploy stops instead of guessing.
#
# Compares docker/migrations/*.sql in the freshly updated remote checkout against
# the schema_migrations bookkeeping table:
#   MISSING — file present, never applied      -> abort
#   DRIFT   — applied sha256 != current file   -> abort
#   ORPHAN  — applied, file gone from the repo -> warn (history, not a blocker)
#
# Runs after code_update (new migration files are present) and before any
# container is touched, so aborting here leaves the running system as it was.
phase_migration_gate() {
    local host="$1" db_container="$2"

    # Hosts with no local Postgres (ADR-0009 worker-host) have nothing to
    # gate — the schema this deploy's code expects is checked where the DB
    # actually lives (server2). Not a skip of the check; there is no
    # applicable target here.
    if [[ -z "$db_container" ]]; then
        step "Phase 3.7: Migration gate — N/A (${host} has no local database)"
        return 0
    fi

    step "Phase 3.7: Migration gate (${db_container} on ${host})"

    if [[ "$DRY_RUN" == "true" ]]; then
        info "DRY-RUN: migration gate skipped"
        return 0
    fi

    local out rc
    out=$(rssh_run "$host" <<REMOTE
set -uo pipefail
cd "${REMOTE_REPO}/docker/migrations" 2>/dev/null || { echo "GATE_ERROR no migrations directory"; exit 3; }

if ! docker exec ${db_container} psql -U ${BRIDGE_DB_USER} -d ${BRIDGE_DB_NAME} -tAc \
        "select to_regclass('public.schema_migrations')" 2>/dev/null | grep -q schema_migrations; then
    echo "GATE_ERROR schema_migrations not readable in ${db_container}"
    exit 3
fi

docker exec ${db_container} psql -U ${BRIDGE_DB_USER} -d ${BRIDGE_DB_NAME} -tAc \
    "select filename || ' ' || sha256 from schema_migrations" 2>/dev/null | grep . | sort > /tmp/.mg_db
for f in *.sql; do printf '%s %s\n' "\$f" "\$(sha256sum "\$f" | cut -d' ' -f1)"; done | sort > /tmp/.mg_files

fail=0
while read -r f s; do
    db_line=\$(grep "^\$f " /tmp/.mg_db || true)
    if [ -z "\$db_line" ]; then
        echo "MISSING \$f"; fail=1
    elif [ "\$(echo "\$db_line" | awk '{print \$2}')" != "\$s" ]; then
        echo "DRIFT \$f"; fail=1
    fi
done < /tmp/.mg_files

while read -r f s; do
    [ -f "\$f" ] || echo "ORPHAN \$f"
done < /tmp/.mg_db

echo "CHECKED \$(wc -l < /tmp/.mg_files)"
exit \$fail
REMOTE
)
    rc=$?

    if (( rc == 3 )); then
        error_ "Migration gate could not run: $(echo "$out" | grep GATE_ERROR || echo "$out")"
        error_ "Refusing to deploy without a verifiable schema state."
        return 1
    fi

    echo "$out" | grep '^ORPHAN ' | while read -r _ f; do
        warn "Applied migration no longer in repo: ${f} (history — not blocking)"
    done

    if (( rc != 0 )); then
        error_ "Database schema does not match the code being deployed:"
        echo "$out" | grep -E '^(MISSING|DRIFT) ' | while read -r kind f; do
            case "$kind" in
                MISSING) error_ "  not applied : ${f}" ;;
                DRIFT)   error_ "  changed     : ${f} (applied version differs from the file)" ;;
            esac
        done
        error_ "Apply them deliberately, then re-run the deploy:"
        error_ "  ssh root@${host} 'docker exec -i ${db_container} psql -U ${BRIDGE_DB_USER} -d ${BRIDGE_DB_NAME}' < docker/migrations/<file>.sql"
        return 1
    fi

    info "Migration gate OK — $(echo "$out" | awk '/^CHECKED/{print $2}') migrations match the database"
    return 0
}

# ============================================================================
# Release manifest — records what SHOULD be running, per service, per host
# ============================================================================
# Complements DEPLOYED_SHA_FILE (one commit for the whole host) with a
# per-SERVICE record: container name, image ID, commit, timestamp. A partial
# deploy (`bridge-deploy.sh hetzner nginx`) only touches nginx's entry — the
# other services keep their last-recorded state, because they are genuinely
# still running what they last recorded.
#
# This is the "should be running" half of drift detection. The "is running"
# half is a live `docker inspect` against the container; bridge-drift-check.sh
# compares the two so an out-of-band change (manual restart onto a stale
# image, hand-pulled image, host-level container recreate) is visible within
# minutes via a cron, instead of only surfacing at the next deploy's Phase 1
# provenance check (which only runs when someone deploys — see the 2026-07-31
# server2 incident referenced there: checkout ahead of running images for
# days, unnoticed).
#
# Host-local and gitignored on purpose — like DEPLOYED_SHA_FILE, this is
# runtime STATE, not architecture, and must never be captured back into the
# repo (the exact "edit on Hetzner → capture drift back" anti-pattern ADR-0006
# exists to kill). Written best-effort: a failure here must never fail an
# otherwise-successful deploy, matching the DEPLOYED_SHA_FILE write.
write_release_manifest() {
    local host="$1" server_name="$2" server_prefix="$3" current_sha="$4"
    shift 4
    local services=("$@")
    [[ ${#services[@]} -eq 0 ]] && return 0

    local pairs="" svc c
    for svc in "${services[@]}"; do
        c=$(container_for_svc "$server_prefix" "$svc")
        pairs+="${svc} ${c}"$'\n'
    done
    # base64-transport: container/service names are safe today, but this
    # avoids ever having to reason about heredoc-quoting edge cases as names
    # change (same defensive move as phase_reconcile_worker_key's key_b64).
    local pairs_b64
    pairs_b64=$(printf '%s' "$pairs" | base64 -w0)

    local result
    result=$(rssh_run "$host" <<EOF
MANIFEST_FILE="${RELEASE_MANIFEST_FILE}"
DEPLOYED_SHA="${current_sha}"
SVC_PAIRS_B64="${pairs_b64}"
SERVER_NAME="${server_name}"
python3 - "\$MANIFEST_FILE" "\$DEPLOYED_SHA" "\$SVC_PAIRS_B64" "\$SERVER_NAME" <<'PYEOF'
import base64, datetime, json, os, subprocess, sys, tempfile

manifest_file, commit, pairs_b64, server_name = sys.argv[1:5]
pairs = base64.b64decode(pairs_b64).decode()
now = datetime.datetime.now(datetime.timezone.utc).isoformat()

try:
    with open(manifest_file) as f:
        manifest = json.load(f)
except Exception:
    manifest = {}
manifest["server_name"] = server_name
services = manifest.setdefault("services", {})

for line in pairs.splitlines():
    line = line.strip()
    if not line:
        continue
    svc, container = line.split(" ", 1)
    try:
        image_id = subprocess.run(
            ["docker", "inspect", "--format", "{{.Image}}", container],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout.strip()
    except Exception as e:
        print(f"MANIFEST_WARN {svc}: docker inspect failed: {e}", file=sys.stderr)
        continue
    if not image_id:
        print(f"MANIFEST_WARN {svc}: empty image id from docker inspect", file=sys.stderr)
        continue
    services[svc] = {
        "container": container,
        "image_id": image_id,
        "deployed_commit": commit,
        "deployed_at": now,
    }

manifest["updated_at"] = now

d = os.path.dirname(manifest_file) or "."
fd, tmp = tempfile.mkstemp(dir=d, prefix=".manifest.")
try:
    with os.fdopen(fd, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, manifest_file)
except Exception:
    os.unlink(tmp)
    raise
print("MANIFEST_OK")
PYEOF
EOF
    ) 2>&1
    local rc=$?
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        case "$line" in
            MANIFEST_OK) ;;
            MANIFEST_WARN*) warn "  ${line#MANIFEST_WARN }" ;;
            *) info "  manifest: ${line}" ;;
        esac
    done <<< "$result"
    if [[ $rc -ne 0 ]] || ! echo "$result" | grep -q 'MANIFEST_OK'; then
        warn "could not write release manifest on ${host} — drift detection may be stale until the next deploy"
        return 1
    fi
    return 0
}

deploy_server() {
    local server_name="$1"
    local host compose all_services build_list server_prefix db_container

    case "$server_name" in
        hetzner)
            host="$HETZNER_HOST"
            compose="$HETZNER_COMPOSE"
            all_services="$HETZNER_ALL"
            build_list="$HETZNER_NEEDS_BUILD"
            server_prefix="HETZNER"
            db_container="${HETZNER_DB_CONTAINER}"
            ;;
        server2)
            host="$SERVER2_HOST"
            compose="$SERVER2_COMPOSE"
            all_services="$SERVER2_ALL"
            build_list="$SERVER2_NEEDS_BUILD"
            server_prefix="SERVER2"
            db_container="${SERVER2_DB_CONTAINER}"
            ;;
        prod-workers)
            host="$WORKERHOST_HOST"
            compose="$WORKERHOST_COMPOSE"
            all_services="$WORKERHOST_ALL"
            build_list="$WORKERHOST_NEEDS_BUILD"
            server_prefix="WORKERHOST"
            db_container=""   # no local DB — phase_migration_gate is a no-op
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

    # === Phase 0 === (local, before anything reaches a host)
    phase_tooling_freshness_gate || return 1

    # === Phase 0.5 === dev-first order gate (prod targets only, local + one
    # read-only SSH to hetzner) — evaluated PER deploy_server call so that
    # "both" reads the marker AFTER its hetzner leg has finished.
    phase_prod_order_gate "$server_name" || return 1

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

    # === Phase 3.7: schema must match the code about to be deployed ===
    # Last point at which no container has been touched. Failure resets the code
    # to the pre-deploy SHA, exactly like Phase 3.5.
    phase_migration_gate "$host" "$db_container" || {
        error_ "Migration gate failed — reverting code to ${ROLLBACK_SHA}"
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
    elif [[ "$server_name" == "server2" ]]; then
        phase_smoke_test "server2" "http://${SERVER2_HOST}:8000" "X-Priority: production" || {
            error_ "Smoke test failed for server2 — rolling back"
            if [[ ${#DEPLOYED_SERVICES[@]} -gt 0 ]]; then
                phase_rollback "$host" "$compose" "$ROLLBACK_SHA" "$build_list" "${DEPLOYED_SERVICES[@]}"
                local rc=$?
                (( rc == 2 )) && return 2
            fi
            return 1
        }
    else
        # prod-workers (ADR-0009): no nginx here, so no public /v1 endpoint to
        # run bridge_smoke.py against — that suite exercises the FULL
        # request path (nginx routing + Lua pool-router + a worker), which
        # only exists once server2's nginx is cut over to point at this host
        # (a separate, still-gated step; see the ADR). Per-container health
        # was already proven in Phase 4 (deploy_one_service's health wait) —
        # that is the applicable bar for this topology today.
        step "Phase 5: Smoke test — N/A (${server_name} has no public endpoint yet)"
        info "Per-worker health already verified in Phase 4 (${DEPLOYED_SERVICES[*]})."
        info "Full request-path smoke only applies after the nginx cutover — see ADR-0009."
    fi

    # === Phase 7: Success report ===
    step "SUCCESS: ${server_name}"
    info "Deployed services : ${DEPLOYED_SERVICES[*]}"
    info "Pre-deploy SHA    : ${ROLLBACK_SHA}"
    if [[ "$DRY_RUN" == "false" ]]; then
        local current_sha
        current_sha=$(rssh "$host" "cd ${REMOTE_REPO} && git rev-parse HEAD")
        info "Current SHA       : ${current_sha}"
        # Record what was actually BUILT INTO THE IMAGES. The checkout HEAD is
        # not a usable stand-in: it can be fast-forwarded out-of-band without a
        # deploy, and then it claims code is live that no container contains —
        # observed on server2, where the checkout sat two commits ahead of the
        # running images. The foreign-commit gate compares against this marker,
        # so it must be written by the only thing that knows: a finished deploy.
        rssh "$host" "printf '%s\n' '${current_sha}' > ${DEPLOYED_SHA_FILE}" \
            || warn "could not record deployed SHA on ${host} — the foreign-commit gate will fall back to the checkout HEAD next time"
        write_release_manifest "$host" "$server_name" "$server_prefix" "$current_sha" "${DEPLOYED_SERVICES[@]}"
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

# ============================================================================
# Pre-flight: smoke COVERAGE (local, before any host is touched)
# ============================================================================
# phase_smoke_test's own comment has claimed since it was written that
# "Endpoint coverage is enforced by bridge_smoke_coverage.py". It was not:
# the validator existed, exited 1 for two uncovered routes, and was called
# from nowhere. A documented gate that does not run is the same defect class
# as the nginx capacity gate that never fired (d6066aa) and the dead
# $target_dest map (9116da0) — configuration describing a mechanism that
# isn't there. Three instances in one codebase makes it a habit, so this one
# gets wired in rather than described.
#
# Local and BEFORE the hosts: this is static analysis of routes vs. probes, it
# needs no server, and a coverage hole should stop the deploy before anything
# has been pulled, built or recreated. Fail fast, cheaply.
#
# What it protects: a new /v1 route that is neither probed nor explicitly
# excluded ships completely untested — which is exactly how /v1/document/convert
# returned 415 on every PDF for weeks while /health stayed green.
_coverage_script="$(dirname "${BASH_SOURCE[0]}")/bridge_smoke_coverage.py"
if [[ -f "$_coverage_script" ]]; then
    step "Pre-flight: smoke coverage"
    if _coverage_out=$(python3 "$_coverage_script" 2>&1); then
        while IFS= read -r line; do info "  coverage: ${line}"; done <<< "$_coverage_out"
    else
        while IFS= read -r line; do error_ "  coverage: ${line}"; done <<< "$_coverage_out"
        error_ "ABORTED: every functional /v1 route must be probed by bridge_smoke.py or"
        error_ "  listed in its EXCLUDED map with a reason. An unprobed route ships untested."
        error_ "  Nothing was touched on any host."
        exit 1
    fi
else
    warn "bridge_smoke_coverage.py not found next to bridge-deploy.sh — coverage NOT enforced"
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
    prod-workers)
        deploy_server "prod-workers"
        ;;
esac

info "=== bridge-deploy.sh finished successfully ==="
exit 0
