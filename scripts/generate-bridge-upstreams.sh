#!/usr/bin/env bash
# =============================================================================
# generate-bridge-upstreams.sh — the ONE source for the per-topology nginx
# upstreams include (ADR-0006 items B/C).
# =============================================================================
# The shared docker/nginx.conf (OpenResty + Lua, used by BOTH bridges) is
# topology-AGNOSTIC: it contains no worker names. The only pieces that legitimately
# differ between primary and production are the worker set and the backup host —
# and they live in exactly one generated include per bridge:
#
#   scripts/generate-bridge-upstreams.sh primary    -> docker/upstreams-primary.conf
#   scripts/generate-bridge-upstreams.sh production  -> docker/upstreams-prod.conf
#   scripts/generate-bridge-upstreams.sh all         (writes both, the default)
#
# nginx.conf does `include /tmp/upstreams.conf;` (the compose envsubst's the
# mounted include first, to resolve ${BRIDGE_BACKUP_HOST}). Because there is only
# ONE nginx.conf and the worker set is emitted from ONE script, a route can never
# again exist on one bridge and not the other (the werking-energy Phase-4 drift).
#
# The worker sets below are the VERIFIED host ground truth (ADR-0006 cutover
# table, re-verified live 2026-07-01):
#   primary    : worker1..4                 (metrics-reader BRIDGE_WORKERS default)
#   production : worker-sahori, worker-kurt, worker-coach, worker-erk
#                (metrics-reader BRIDGE_WORKERS env; 2->4 am 2026-08-18)
#
# Regenerate + commit when the worker set changes (rare). Do NOT hand-edit the
# generated files — the header marks them, and bridge-parity-check.sh compares the
# in-container copy against the repo copy to catch drift.
#
# --- Worker-host separation (ADR-0009) -------------------------------------
# A worker name is a Docker Compose SERVICE NAME by default, resolved via the
# embedded Docker DNS (127.0.0.11) to a container on the SAME host/network —
# that is why `server ${w}:8000` has always been enough. ADR-0009 moves prod
# workers onto a separate host (they no longer share a Docker network with
# the LB), so some names need an explicit network TARGET instead of relying
# on same-host DNS. PROD_WORKER_TARGETS is that override: empty by default
# (= today's exact behaviour, byte-identical generated output), populated
# per-worker only at the gated cutover described in ADR-0009. The worker's
# NAME (billing/account identity, read live from account-pool-state's
# `.worker` field — see pool_router.lua) never changes; only WHERE nginx
# connects to reach it does. Do not repurpose this table for anything else.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_DIR="${ROOT}/docker"

PRIMARY_WORKERS=(worker1 worker2 worker3 worker4)
PROD_WORKERS=(worker-sahori worker-kurt worker-coach worker-erk)
declare -A PRIMARY_WORKER_TARGETS=()
declare -A PROD_WORKER_TARGETS=()
# Example cutover entry (do not uncomment without ADR-0009's gated migration):
#   PROD_WORKER_TARGETS[worker-sahori]="100.93.143.105:8001"

# --- Topology table (single source of the per-bridge worker set) -------------
# primary: default pool has NO cross-host backup (dev/prod isolation — a dev
#          request must fail-fast, never spill onto the customer bridge). The
#          production-priority pool (claude_production) targets the PROD
#          BRIDGE FIRST, local dev workers are the overflow/backup path
#          (Rafael, 2026-08-31: his own dev-time usage saturates the dev
#          workers more than all partners combined; X-Priority:production
#          traffic — e.g. Energy/Safety in Railway production env — must
#          prefer the dedicated production capacity, not compete with that
#          load, and only spill onto the dev bridge if the prod bridge is
#          itself unreachable/exhausted). Reversed from the original
#          "dev-first, prod-backup" order.
# production: the default pool backs up to the dev bridge (Model-B resilience:
#          both prod workers exhausted/down -> dev bridge serves). claude_production
#          already targets the local prod workers first with the dev bridge as
#          backup — unchanged, this was already the wanted order.

# $1 = worker name, $2 = NAME (string) of the associative targets array in
# scope (e.g. "PROD_WORKER_TARGETS") -> "host:port", defaulting to the
# same-host-DNS target. Reads the array via indirect expansion rather than a
# nameref so nested callers (emit_upstream/emit_worker_map) never collide
# names with this function's own local (bash namerefs referencing a nameref
# of the same name in an enclosing scope raise "circular name reference").
resolve_target() {
    local worker="$1" arrname="$2" key
    key="${arrname}[${worker}]"
    if [[ -n "${!key+x}" ]]; then
        echo "${!key}"
    else
        echo "${worker}:8000"
    fi
}

emit_upstream() {
    # $1 = upstream name, $2 = keepalive,
    # $3 = "nobackup" | "local-first" | "remote-first"
    #        nobackup     -> local workers only, no cross-host entry at all
    #        local-first  -> local workers as regular servers, ${BRIDGE_BACKUP_HOST}
    #                        as nginx `backup` (only used once ALL locals fail)
    #        remote-first -> ${BRIDGE_BACKUP_HOST} as the regular server, local
    #                        workers marked `backup` (overflow only once the
    #                        remote bridge itself is down/exhausted)
    # $4 = targets-array NAME (string), then worker names
    local name="$1"; local keepalive="$2"; local mode="$3"; local targets_name="$4"; shift 4
    echo "upstream ${name} {"
    local w target
    case "$mode" in
        remote-first)
            echo "    server \${BRIDGE_BACKUP_HOST}:8000 weight=1 max_fails=1 fail_timeout=10s;"
            for w in "$@"; do
                target="$(resolve_target "$w" "$targets_name")"
                echo "    server ${target} backup max_fails=0;"
            done
            ;;
        local-first)
            for w in "$@"; do
                target="$(resolve_target "$w" "$targets_name")"
                echo "    server ${target} weight=1 max_fails=0;"
            done
            echo "    server \${BRIDGE_BACKUP_HOST}:8000 backup max_fails=1 fail_timeout=10s;"
            ;;
        nobackup)
            for w in "$@"; do
                target="$(resolve_target "$w" "$targets_name")"
                echo "    server ${target} weight=1 max_fails=0;"
            done
            ;;
        *)
            echo "ERROR: emit_upstream: unknown mode '$mode'" >&2
            return 1
            ;;
    esac
    echo "    keepalive ${keepalive};"
    echo "}"
}

# Emits the nginx `map` body (just the entries, caller wraps the map{} block)
# resolving a worker NAME to its network TARGET for the direct-worker debug
# route (docker/nginx.conf `location ~ ^/(worker[a-zA-Z0-9_-]+)/(.*)$`).
# Only emits a line when the target differs from the same-host-DNS default —
# an empty/no-override topology therefore emits a comment-only file, which is
# valid nginx and preserves today's `default $direct_worker:8000;` behaviour.
emit_worker_map() {
    local targets_name="$1"; shift
    local w target has_override=0
    for w in "$@"; do
        target="$(resolve_target "$w" "$targets_name")"
        if [[ "$target" != "${w}:8000" ]]; then
            echo "    ${w} ${target};"
            has_override=1
        fi
    done
    if [[ "$has_override" -eq 0 ]]; then
        echo "    # no remote workers for this topology — default \$direct_worker:8000 applies"
    fi
}

# NOTE (2026-07-30): the per-worker `map $target_worker $target_dest` that used
# to be emitted here has been REMOVED, together with the `upstream
# claude_unavail` sentinel it pointed at. $target_dest was never read — not in a
# proxy_pass, not anywhere — so the whole chain was dead config that read like
# live per-worker routing with a failure sentinel. That is the same hazard class
# as the rewrite-phase `if ($target_worker = "unavailable")` removed in d6066aa:
# configuration that describes a mechanism which does not run. Routing is done
# by pool_router.lua setting $target_worker, and the static `proxy_pass
# http://claude_workers` (static is required — a variable proxy_pass disables
# proxy_next_upstream). Do not reintroduce a map "for structural parity".

generate() {
    local id="$1"
    local -a workers
    local default_backup targets_name
    case "$id" in
        primary)
            workers=("${PRIMARY_WORKERS[@]}")
            default_backup="nobackup"   # isolation: default pool must not spill to prod
            targets_name="PRIMARY_WORKER_TARGETS"
            ;;
        production)
            workers=("${PROD_WORKERS[@]}")
            default_backup="local-first"   # Model-B: default pool backs up to dev bridge
            targets_name="PROD_WORKER_TARGETS"
            ;;
        *)
            echo "ERROR: unknown topology '$id' (primary|production)" >&2
            return 1
            ;;
    esac

    cat <<HEADER
# =============================================================================
# AUTO-GENERATED by scripts/generate-bridge-upstreams.sh ${id} — DO NOT EDIT
# =============================================================================
# The per-topology worker set for the SHARED docker/nginx.conf (ADR-0006 B/C).
# This is the ONLY nginx difference between the two bridges. \${BRIDGE_BACKUP_HOST}
# is resolved by the compose envsubst before nginx loads this file.
#   topology : ${id}
#   workers  : ${workers[*]}
# Regenerate with the generator; never hand-edit. bridge-parity-check.sh verifies
# the in-container copy matches this repo file.
# =============================================================================

HEADER

    echo "# Round-robin default pool (used by /health, Lua-routed /v1 SSE, default /)."
    emit_upstream claude_workers 32 "$default_backup" "$targets_name" "${workers[@]}"
    echo

    echo "# Production-priority pool (X-Priority: production)."
    if [ "$id" = "primary" ]; then
        echo "# Targets the PROD bridge (\${BRIDGE_BACKUP_HOST}) FIRST — local dev"
        echo "# workers are the overflow path, used only if the prod bridge itself"
        echo "# is unreachable/exhausted (Rafael, 2026-08-31: production-priority"
        echo "# traffic must not compete with dev-time load for dev-worker capacity)."
        emit_upstream claude_production 16 remote-first "$targets_name" "${workers[@]}"
    else
        echo "# Local prod workers first, \${BRIDGE_BACKUP_HOST} (the dev bridge) is the"
        echo "# overflow path — already the wanted order, unchanged."
        emit_upstream claude_production 16 local-first "$targets_name" "${workers[@]}"
    fi
}

generate_worker_map() {
    local id="$1"
    local -a workers
    local targets_name
    case "$id" in
        primary)    workers=("${PRIMARY_WORKERS[@]}"); targets_name="PRIMARY_WORKER_TARGETS" ;;
        production) workers=("${PROD_WORKERS[@]}");    targets_name="PROD_WORKER_TARGETS" ;;
        *) echo "ERROR: unknown topology '$id' (primary|production)" >&2; return 1 ;;
    esac

    cat <<HEADER
# =============================================================================
# AUTO-GENERATED by scripts/generate-bridge-upstreams.sh ${id} — DO NOT EDIT
# =============================================================================
# nginx \`map \$direct_worker \$worker_target\` body for the direct-worker debug
# route (docker/nginx.conf). Resolves a worker NAME to its network TARGET when
# that worker lives on a separate host (ADR-0009); same-host workers fall
# through to the map's own \`default \$direct_worker:8000;\`, unchanged.
#   topology : ${id}
# =============================================================================
HEADER
    emit_worker_map "$targets_name" "${workers[@]}"
}

main() {
    local target="${1:-all}"
    case "$target" in
        primary)
            generate primary > "${OUT_DIR}/upstreams-primary.conf"; echo "[gen] wrote docker/upstreams-primary.conf"
            generate_worker_map primary > "${OUT_DIR}/worker-map-primary.conf"; echo "[gen] wrote docker/worker-map-primary.conf"
            ;;
        production)
            generate production > "${OUT_DIR}/upstreams-prod.conf"; echo "[gen] wrote docker/upstreams-prod.conf"
            generate_worker_map production > "${OUT_DIR}/worker-map-prod.conf"; echo "[gen] wrote docker/worker-map-prod.conf"
            ;;
        all)
            generate primary > "${OUT_DIR}/upstreams-primary.conf"; echo "[gen] wrote docker/upstreams-primary.conf"
            generate_worker_map primary > "${OUT_DIR}/worker-map-primary.conf"; echo "[gen] wrote docker/worker-map-primary.conf"
            generate production > "${OUT_DIR}/upstreams-prod.conf"; echo "[gen] wrote docker/upstreams-prod.conf"
            generate_worker_map production > "${OUT_DIR}/worker-map-prod.conf"; echo "[gen] wrote docker/worker-map-prod.conf"
            ;;
        *) echo "Usage: generate-bridge-upstreams.sh [primary|production|all]" >&2; exit 1 ;;
    esac
}

main "$@"
