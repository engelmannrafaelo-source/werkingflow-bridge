# shellcheck shell=bash
# Deploy gate in front of a platform-api recreation (BR8). Sourced by
# bridge-deploy.sh.
#
# Why: since ADR-0011 a platform-api is load-bearing for jobs that run on the
# OTHER bridge's workers. A job with origin dev that runs on a prod worker asks
# the DEV platform-api for pin, identity and budget. On 10.10.2026 04:09:18Z a
# dev deploy recreated that platform-api (about 3 s without an answer) while
# exactly such a research job was asking. It died for good (BR7). The worker
# idle wait in deploy_one_service cannot see this: it only counts the workers
# of the bridge being deployed.
#
# What: before the first recreation of a deploy that touches platform-api (or,
# on server2, its database postgres-prod), ask (read-only, from inside a worker of
# the bridge being deployed, see scripts/platform_api_job_gate.py) both the
# local store (all active jobs) and every peer store (active jobs whose budget
# home is this bridge). Wait while any are active, at most
# PLATFORM_API_JOBGATE_WAIT_S seconds (default 900, 0 = check once). Then:
#   idle                        → recreate
#   still busy / not provable   → refuse; platform-api is NOT touched
#   endpoint missing (HTTP 404) → refuse, unless PLATFORM_API_JOBGATE_EINFUEHRUNG=1
#     (one-time introduction while a bridge still runs an image from before BR8;
#     loud WARN, and only for 404, never for timeouts or other errors)
#
# The code fix makes a job that falls into the gap anyway retryable
# (ProviderConfigTemporarilyUnavailable → job parked). This gate keeps the gap
# away from running jobs in the first place.
#
# Not rolling: platform-api has a fixed container_name and a fixed host port
# (8300 on the tailnet), so two instances cannot run side by side without a
# proxy in front. The gap stays about 3 s; the gate decides WHEN it happens.

PLATFORM_API_JOBGATE_POLL_S="${PLATFORM_API_JOBGATE_POLL_S:-10}"

# Services whose recreation leaves the platform-api without an answer: the
# platform-api itself and, where it runs next to it, its database.
#   $1 = server prefix (HETZNER | SERVER2 | WORKERHOST), rest = services
# Returns 0 when the gate must run before Phase 4.
platform_api_job_gate_needed() {
    local prefix="$1"; shift
    local svc
    for svc in "$@"; do
        case "${prefix}:${svc}" in
            HETZNER:platform-api|SERVER2:platform-api|SERVER2:postgres-prod) return 0 ;;
        esac
    done
    return 1
}

# Where to run the probe: a worker of the bridge whose platform-api is about to
# be recreated (it has that bridge's origin, token and peer list).
#   $1 = server prefix (HETZNER | SERVER2)
# Prints "<host> <container-candidates...>".
platform_api_job_gate_probe_site() {
    case "$1" in
        HETZNER)
            echo "${HETZNER_HOST} ${HETZNER_SVC_worker1} ${HETZNER_SVC_worker2} ${HETZNER_SVC_worker3} ${HETZNER_SVC_worker4}"
            ;;
        SERVER2)
            # Prod workers live on the worker host (ADR-0009), not on server2.
            echo "${WORKERHOST_HOST} ${WORKERHOST_SVC_worker_sahori} ${WORKERHOST_SVC_worker_kurt} ${WORKERHOST_SVC_worker_coach} ${WORKERHOST_SVC_worker_erk}"
            ;;
        *)
            return 1
            ;;
    esac
}

# One probe run. Output goes to stdout; returns the probe's exit code
# (0 idle, 1 busy, 2 not provable, 3 endpoint missing).
platform_api_job_gate_probe() {
    local host="$1"; shift
    local container
    for container in "$@"; do
        if rssh "$host" "docker inspect --format '{{.State.Running}}' '${container}' 2>/dev/null" \
                | grep -qx true; then
            local rc=0
            rssh "$host" "docker exec -i '${container}' python3 -" \
                < "$(dirname "${BASH_SOURCE[0]}")/platform_api_job_gate.py" || rc=$?
            return "$rc"
        fi
    done
    echo "no running worker among: $*"
    return 2
}

# Returns 0 = go ahead and recreate, 3 = refused (platform-api untouched).
#   $1 = server prefix (HETZNER | SERVER2)
platform_api_job_gate() {
    local prefix="$1"
    local site
    if ! site=$(platform_api_job_gate_probe_site "$prefix"); then
        error_ "platform-api job gate: no probe site for ${prefix} — refusing recreation"
        return 3
    fi
    local wait_s="${PLATFORM_API_JOBGATE_WAIT_S:-900}"
    if [[ ! "$wait_s" =~ ^[0-9]+$ ]]; then
        error_ "PLATFORM_API_JOBGATE_WAIT_S='${wait_s}' is not a number of seconds"
        return 3
    fi
    local deadline=$(( $(date +%s) + wait_s ))
    local out rc
    # shellcheck disable=SC2086
    while :; do
        rc=0
        out=$(platform_api_job_gate_probe $site 2>&1) || rc=$?
        while IFS= read -r line; do info "  job gate: $line"; done <<< "$out"
        if [[ "$DRY_RUN" == "true" ]]; then
            info "[DRY-RUN] platform-api job gate result rc=${rc} (0 idle, 1 busy, 2 not provable, 3 endpoint missing) — no wait in dry-run"
            return 0
        fi
        case "$rc" in
            0)
                info "platform-api job gate: no active job depends on this platform-api — recreating"
                return 0
                ;;
            1)
                if (( $(date +%s) >= deadline )); then
                    error_ "platform-api job gate: jobs still depend on this platform-api after ${wait_s}s — refusing recreation (platform-api untouched)"
                    return 3
                fi
                info "platform-api job gate: active jobs depend on this platform-api; waiting ${PLATFORM_API_JOBGATE_POLL_S}s..."
                sleep "$PLATFORM_API_JOBGATE_POLL_S"
                ;;
            3)
                if [[ "${PLATFORM_API_JOBGATE_EINFUEHRUNG:-0}" == "1" ]]; then
                    warn "platform-api job gate: at least one platform-api has no job endpoint yet (image before BR8) — PLATFORM_API_JOBGATE_EINFUEHRUNG=1, recreating WITHOUT seeing those jobs"
                    return 0
                fi
                error_ "platform-api job gate: at least one platform-api has no job endpoint yet (HTTP 404, image before BR8), so jobs there cannot be seen. Refusing recreation. One-time introduction in a quiet window: PLATFORM_API_JOBGATE_EINFUEHRUNG=1"
                return 3
                ;;
            *)
                error_ "platform-api job gate: could not prove that no job depends on this platform-api (rc=${rc}) — refusing recreation (platform-api untouched)"
                return 3
                ;;
        esac
    done
}
