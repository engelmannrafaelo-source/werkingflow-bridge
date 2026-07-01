#!/bin/bash
# =============================================================================
# Bridge deploy-gate: CURRENCY + PARITY check  (ADR-0006 item F)
# =============================================================================
# Guards the root cause behind the werking-energy Phase-4 incident: a bridge
# host whose live nginx config has DRIFTED from the repo (hand-edited on the box,
# or the box sitting behind origin/develop). Run this BEFORE a deploy — or wire
# it in as a pre-deploy phase — so drift fails loud here, not in production.
#
#   scripts/bridge-parity-check.sh <hetzner|server2>
#
# Exit 0 = in sync. Exit != 0 = drift found (message says which of the 3 checks).
#
# THREE checks, all fail-loud (no silent fallback):
#   1. CURRENCY  — host repo HEAD == origin/develop  (box is not behind / ahead)
#   2. CLEAN     — host repo working tree has no uncommitted edits under docker/
#                  (catches "edited the file on the box" before it is back-ported)
#   3. MOUNT     — the shared route includes actually loaded INSIDE the running
#                  nginx container are byte-identical to the host repo files
#                  (catches a stale mount / docker cp / wrong file on disk)
#
# Only the shared, drift-prone SOURCE files are checked (the route includes) —
# NOT the per-env nginx.conf/nginx-prod.conf, which legitimately differ.
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SERVER="${1:-}"
case "$SERVER" in
    hetzner)
        HOST="49.12.72.66"
        NGINX_CONTAINER="wt-wrapper-lb"
        ;;
    server2)
        HOST="178.104.178.79"
        NGINX_CONTAINER="wt-prod-lb"
        ;;
    *)
        echo "Usage: bridge-parity-check.sh <hetzner|server2>" >&2
        exit 2
        ;;
esac
REMOTE_REPO="/root/werkingflow-bridge"

# The shared, single-sourced route includes (ADR-0006 item A). These are the
# files that MUST be identical repo↔container↔both-bridges. Add new shared
# includes here as they are introduced.
SHARED_INCLUDES=(
    "routes-metrics-reader.conf"
    "routes-platform-api.conf"
)

log()  { echo "[parity:$SERVER] $*"; }
fail() { echo "[parity:$SERVER] FAIL: $*" >&2; FAILED=1; }
FAILED=0

rssh() { ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 "root@${HOST}" "$@"; }

log "host=${HOST} repo=${REMOTE_REPO} container=${NGINX_CONTAINER}"

# --- Check 1: CURRENCY (deployed surface) ------------------------------------
# The box must have the docker/ tree it would deploy. Scoped to docker/ on
# purpose: that IS the deployed surface (compose + nginx + shared includes).
# A docs- or scripts-only commit on develop does NOT make the bridge stale, so
# it must not trip this gate. If docker/ at host HEAD != docker/ at origin/develop
# → the box is behind on files that actually ship. The full-HEAD delta is shown
# as info only.
log "check 1/3: currency (docker/ at host HEAD == docker/ at origin/develop)"
currency=$(rssh "cd ${REMOTE_REPO} && git fetch -q origin develop 2>/dev/null; \
    host_head=\$(git rev-parse HEAD); origin_develop=\$(git rev-parse origin/develop); \
    if git diff --quiet HEAD origin/develop -- docker/; then docker_state=SAME; else docker_state=DIFF; fi; \
    echo \"\${host_head} \${origin_develop} \${docker_state}\"") \
    || fail "cannot read git state on host"
if [ -n "${currency:-}" ]; then
    host_head=$(echo "$currency" | awk '{print $1}')
    origin_develop=$(echo "$currency" | awk '{print $2}')
    docker_state=$(echo "$currency" | awk '{print $3}')
    if [ "$docker_state" = "SAME" ]; then
        log "  OK  docker/ current (host ${host_head:0:12}$( [ "$host_head" != "$origin_develop" ] && echo ", non-docker commits ahead on origin — ok"))"
    else
        fail "docker/ differs: host HEAD ${host_head:0:12} is behind origin/develop ${origin_develop:0:12} on deployed files — git pull before deploy"
    fi
fi

# --- Check 2: CLEAN ----------------------------------------------------------
# A MODIFIED TRACKED file under docker/ = a live edit that has not gone through
# the repo — exactly how nginx-prod.conf drifted. That FAILS the gate.
# Untracked files (`??`, e.g. leftover *.bak) are cruft, not runtime drift
# (check 3 proves what nginx actually loaded) — they only WARN.
log "check 2/3: no modified tracked files under docker/"
dirty=$(rssh "cd ${REMOTE_REPO} && git status --porcelain -- docker/ 2>/dev/null") \
    || fail "cannot read git status on host"
tracked_mods=$(printf '%s\n' "${dirty:-}" | grep -vE '^\?\?|^!!' | sed '/^$/d' || true)
untracked=$(printf '%s\n' "${dirty:-}" | grep -E '^\?\?' | sed '/^$/d' || true)
if [ -n "$tracked_mods" ]; then
    fail "uncommitted edits to TRACKED files under docker/ on the host:"
    while IFS= read -r line; do echo "        $line" >&2; done <<< "$tracked_mods"
else
    log "  OK  no modified tracked files under docker/"
fi
if [ -n "$untracked" ]; then
    n=$(printf '%s\n' "$untracked" | wc -l | tr -d ' ')
    log "  WARN ${n} untracked file(s) under docker/ (cruft, not drift; e.g. $(printf '%s' "$untracked" | head -1 | awk '{print $2}'))"
fi

# --- Check 3: MOUNT ----------------------------------------------------------
# The file the running nginx actually loaded must equal the host repo file.
# Compares sha256 of the in-container copy vs the on-disk repo copy.
log "check 3/3: in-container includes == host repo includes"
for inc in "${SHARED_INCLUDES[@]}"; do
    sums=$(rssh "
        repo_sum=\$(sha256sum ${REMOTE_REPO}/docker/${inc} 2>/dev/null | awk '{print \$1}')
        cont_sum=\$(docker exec ${NGINX_CONTAINER} sha256sum /etc/nginx/${inc} 2>/dev/null | awk '{print \$1}')
        echo \"\${repo_sum:-MISSING_REPO} \${cont_sum:-MISSING_CONTAINER}\"
    ") || { fail "cannot compare ${inc} on host"; continue; }
    repo_sum="${sums%% *}"; cont_sum="${sums##* }"
    if [ "$repo_sum" = "MISSING_REPO" ]; then
        fail "${inc}: not present in host repo (${REMOTE_REPO}/docker/${inc})"
    elif [ "$cont_sum" = "MISSING_CONTAINER" ]; then
        fail "${inc}: not mounted in ${NGINX_CONTAINER} at /etc/nginx/${inc}"
    elif [ "$repo_sum" != "$cont_sum" ]; then
        fail "${inc}: container copy != repo copy (repo ${repo_sum:0:12} vs container ${cont_sum:0:12})"
    else
        log "  OK  ${inc} (${repo_sum:0:12})"
    fi
done

echo
if [ "$FAILED" -eq 0 ]; then
    log "PARITY OK — repo, working tree, and running container are in sync."
    exit 0
else
    log "PARITY FAILED — resolve drift above before deploying."
    exit 1
fi
