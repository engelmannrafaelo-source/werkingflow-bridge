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

# P1 (SEC 18.11.2026, Auftrag 692bcf6c): the peer half of the gate is asked
# from the PEER's own side. For a dev (HETZNER) deploy the question "which jobs
# in the prod store have dev as budget home?" is asked over the regular SSH
# path into a prod WORKER on the prod worker host, against that worker's own
# platform-api with that worker's own credential (in its process only). No prod
# credential lives on dev, none travels in argv or logs. Binding is exact:
#   host      : `hostname` on the SSH target == PLATFORM_API_JOBGATE_REMOTE_HOSTNAME
#   container : exact name from the WORKERHOST_SVC_* list, running
#   origin    : BRIDGE_ORIGIN_ID inside the container == "prod" (checked by the probe)
# Anything else (SSH failure, timeout, wrong host/container/origin, malformed
# answer) is an error and refuses the recreation. active OR waiting > 0 is busy.
# Fixed on purpose (not overridable from the environment): the exact host the prod store is asked from.
readonly PLATFORM_API_JOBGATE_REMOTE_HOSTNAME="prod-workers-1"
PLATFORM_API_JOBGATE_REMOTE_TIMEOUT_S=60
# A-GP-3 (Review P1): LOCAL deadline around every remote call of the remote
# probe (hostname, inspect, exec). A hanging SSH session ends as rc 124 = error.
PLATFORM_API_JOBGATE_SSH_FRIST_S="${PLATFORM_API_JOBGATE_SSH_FRIST_S:-90}"
# rc of deploy_one_service when the second gate round refuses (A-GP-1).
JOBGATE_VOR_RECREATE_RC=5

# mit_frist <s> <command...> — runs a command or shell FUNCTION with a local
# deadline (coreutils `timeout` cannot run functions). stdin is passed on.
# Returns the command's rc, or 124 whenever the deadline hit (also when the
# command happened to end at the same moment: fail-closed).
# The watchdog ends the subshell AND its direct children in ONE kill: ending
# only the children first let a function resume and return 0 (race, found by
# the shell test). Timeout is detected by a marker, not by the rc.
mit_frist() {
    local frist="$1"; shift
    local marke
    marke=$(mktemp -d) || return 124
    ( "$@" ) 0<&0 &
    local pid=$!
    # The watchdog must not hold the caller's stdout (a $(...) would wait for it).
    ( command sleep "$frist"; : > "$marke/abgelaufen"; kill -TERM "$pid" $(pgrep -P "$pid") 2>/dev/null ) </dev/null >/dev/null 2>&1 &
    local wache=$!
    local rc=0
    wait "$pid" || rc=$?
    # Same rule for the watchdog: end it together with its sleep, or a freed
    # watchdog would still write the marker.
    kill -TERM "$wache" $(pgrep -P "$wache") 2>/dev/null; wait "$wache" 2>/dev/null
    [[ -e "$marke/abgelaufen" ]] && rc=124
    rm -f "$marke/abgelaufen"; rmdir "$marke" 2>/dev/null
    return "$rc"
}

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

# Peers asked from their own side (P1) and where:
#   $1 = server prefix. Prints "<peer> <own-origin-there> <home-origin> <host> <containers...>"
#   or nothing (no remote peer for this prefix: the local probe asks every peer itself).
platform_api_job_gate_remote_site() {
    case "$1" in
        HETZNER)
            echo "prod prod dev ${WORKERHOST_HOST} ${WORKERHOST_SVC_worker_sahori} ${WORKERHOST_SVC_worker_kurt} ${WORKERHOST_SVC_worker_coach} ${WORKERHOST_SVC_worker_erk}"
            ;;
    esac
}

# One probe run. Output goes to stdout; returns the probe's exit code
# (0 idle, 1 busy, 2 not provable, 3 endpoint missing, 4 remote peer pending).
#   $1 = host, $2 = extra probe arguments ("" or "--peer-remote <name>"), rest = containers
platform_api_job_gate_probe() {
    local host="$1" extra="$2"; shift 2
    local container
    for container in "$@"; do
        if rssh "$host" "docker inspect --format '{{.State.Running}}' '${container}' 2>/dev/null" \
                | grep -qx true; then
            local rc=0
            rssh "$host" "docker exec -i '${container}' python3 - ${extra}" \
                < "$(dirname "${BASH_SOURCE[0]}")/platform_api_job_gate.py" || rc=$?
            return "$rc"
        fi
    done
    echo "no running worker among: $*"
    return 2
}

# Remote (peer-side) probe, P1. Returns 0 idle, 1 busy, 2 error (fail-closed).
#   $1 = own origin over there (e.g. prod), $2 = home origin asked for (e.g. dev),
#   $3 = host, rest = allowed containers (exact names)
platform_api_job_gate_remote_probe() {
    local eigen="$1" heimat="$2" host="$3"; shift 3
    local seen_host container state rc
    seen_host=$(mit_frist "$PLATFORM_API_JOBGATE_SSH_FRIST_S" rssh "$host" "hostname" 2>/dev/null) || { echo "remote ${eigen}: ssh to ${host} failed or timed out"; return 2; }
    if [[ "$seen_host" != "$PLATFORM_API_JOBGATE_REMOTE_HOSTNAME" ]]; then
        echo "remote ${eigen}: host ${host} is '${seen_host}', expected '${PLATFORM_API_JOBGATE_REMOTE_HOSTNAME}'"
        return 2
    fi
    for container in "$@"; do
        [[ "$container" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || { echo "remote ${eigen}: bad container name"; return 2; }
        state=$(mit_frist "$PLATFORM_API_JOBGATE_SSH_FRIST_S" rssh "$host" "docker inspect --format '{{.Name}} {{.State.Running}}' '${container}' 2>/dev/null" 2>/dev/null) || continue
        [[ "$state" == "/${container} true" ]] || continue
        rc=0
        mit_frist "$PLATFORM_API_JOBGATE_SSH_FRIST_S" rssh "$host" "timeout ${PLATFORM_API_JOBGATE_REMOTE_TIMEOUT_S} docker exec -i '${container}' python3 - --eigen '${eigen}' --heimat '${heimat}'" \
            < "$(dirname "${BASH_SOURCE[0]}")/platform_api_job_gate_heimat.py" || rc=$?
        case "$rc" in
            0|1) return "$rc" ;;
            *) echo "remote ${eigen}: probe in ${container} rc=${rc}"; return 2 ;;
        esac
    done
    echo "remote ${eigen}: no running container among: $* on ${seen_host}"
    return 2
}

# Combined verdict of the local probe and the remote peer probes (P1).
# Prints the probe output; returns 0 idle, 1 busy, 2 not provable, 3 endpoint missing.
#   $1 = server prefix
platform_api_job_gate_round() {
    local prefix="$1" site remote extra="" rc_local=0 rc_remote rc_all
    local peer eigen heimat rhost
    site=$(platform_api_job_gate_probe_site "$prefix") || { echo "no probe site for ${prefix}"; return 2; }
    remote=$(platform_api_job_gate_remote_site "$prefix")
    if [[ -n "$remote" ]]; then
        read -r peer eigen heimat rhost _ <<< "$remote"
        extra="--peer-remote ${peer}"
    fi
    # shellcheck disable=SC2086
    platform_api_job_gate_probe ${site%% *} "$extra" ${site#* } || rc_local=$?
    if [[ -z "$remote" ]]; then
        # rc 4 without a configured remote probe can never be a release.
        [[ "$rc_local" == 4 ]] && { echo "local probe wants a remote peer probe, none configured"; return 2; }
        return "$rc_local"
    fi
    rc_remote=0
    # shellcheck disable=SC2086
    platform_api_job_gate_remote_probe "$eigen" "$heimat" "$rhost" $(cut -d' ' -f5- <<< "$remote") || rc_remote=$?
    # Precedence like the probe itself: error > busy > missing > idle.
    # The local answer must be 4 (peer pending) or busy/missing/error; a local 0
    # while a remote peer is configured means the peer was not named -> error.
    case "$rc_local" in
        0) echo "local probe did not mark peer ${peer} as remote"; return 2 ;;
        2) return 2 ;;
    esac
    [[ "$rc_remote" == 2 ]] && return 2
    [[ "$rc_local" == 1 || "$rc_remote" == 1 ]] && return 1
    [[ "$rc_local" == 3 ]] && return 3
    [[ "$rc_local" == 4 && "$rc_remote" == 0 ]] && return 0
    rc_all=2
    return "$rc_all"
}

# Read-only record right AFTER the recreation (P1/BR7): which jobs are active or
# parked now. Never changes the deploy result; every listed job is handed to its
# existing owner by the operator (no drain claim, no blind retry).
#   $1 = server prefix
platform_api_job_gate_nachher() {
    local prefix="$1" out rc=0
    out=$(platform_api_job_gate_round "$prefix" 2>&1) || rc=$?
    while IFS= read -r line; do info "  JOBGATE-NACHHER: $line"; done <<< "$out"
    case "$rc" in
        0) info "JOBGATE-NACHHER: idle (no active/waiting job depends on this platform-api)" ;;
        1) warn "JOBGATE-NACHHER: jobs active or parked after the recreation — follow each listed job up with its owner (BR7 parking is retryable, no blind retry)" ;;
        *) warn "JOBGATE-NACHHER: state not provable after the recreation (rc=${rc}) — report, follow up by hand" ;;
    esac
    return 0
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
    while :; do
        rc=0
        out=$(platform_api_job_gate_round "$prefix" 2>&1) || rc=$?
        while IFS= read -r line; do info "  job gate: $line"; done <<< "$out"
        if [[ "$DRY_RUN" == "true" ]]; then
            info "[DRY-RUN] platform-api job gate result rc=${rc} (0 idle, 1 busy, 2 not provable, 3 endpoint missing) — no wait in dry-run; a real run would $([[ "$rc" == 0 ]] && echo recreate || echo 'NOT recreate')"
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
