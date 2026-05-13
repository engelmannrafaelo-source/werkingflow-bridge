#!/usr/bin/env bash
# Bridge health-check — fires every 2 min via cron. Logs every probe; alerts
# only on transition healthy → unhealthy (to avoid flooding the inbox).
#
# Alert channel: CUI inbox via the dev-server's /api/mission/send endpoint.
# Falls silently back to a marker file if the dev-server is unreachable —
# the next probe will alert again when CUI comes back, so no events lost.
#
# State machine:
#   STATE_FILE absent     → first run, suppress alert, write 'healthy'
#   healthy → unhealthy   → ALERT, write 'unhealthy:<ts>'
#   unhealthy → healthy   → CLEAR-message, write 'healthy'
#   unchanged             → silent

set -euo pipefail

readonly BRIDGE_URL="http://localhost:8000"
readonly STATE_FILE="/var/run/bridge-health.state"
readonly LOG_FILE="/var/log/bridge-health.log"
readonly TIMEOUT=10

log() { printf "[%s] %s\n" "$(date -Is)" "$*" >> "${LOG_FILE}"; }

probe() {
    # Returns 0 if all three core endpoints respond 2xx.
    local h db m
    h=$(curl -s -m "${TIMEOUT}" -o /dev/null -w "%{http_code}" "${BRIDGE_URL}/health"           || echo "000")
    db=$(curl -s -m "${TIMEOUT}" -o /dev/null -w "%{http_code}" "${BRIDGE_URL}/v1/db/health"   || echo "000")
    m=$(curl -s -m "${TIMEOUT}" -o /dev/null -w "%{http_code}" "${BRIDGE_URL}/v1/models"        || echo "000")
    log "probe /health=${h} /v1/db/health=${db} /v1/models=${m}"
    [ "${h}" = "200" ] && [ "${db}" = "200" ] && [ "${m}" = "200" ]
}

send_alert() {
    local subject="$1"; shift
    local body="$*"
    # Try CUI inbox via dev-server (Tailscale IP). Falls silently on connect error.
    local dev_cui="http://100.112.98.39:4005/api/mission/send"
    curl -s -m 5 -o /dev/null -w "" -X POST "${dev_cui}" \
        -H "Content-Type: application/json" \
        -d "$(printf '{"to":"rafael","subject":%s,"message":%s}' \
              "$(printf '%s' "${subject}" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')" \
              "$(printf '%s' "${body}"    | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')")" \
        || log "alert delivery failed for: ${subject}"
}

prior_state="$(cat "${STATE_FILE}" 2>/dev/null || echo 'unknown')"

if probe; then
    case "${prior_state}" in
        healthy|unknown)
            echo "healthy" > "${STATE_FILE}"
            ;;
        unhealthy:*)
            log "RECOVERED — clearing prior alert"
            send_alert "Bridge health: RECOVERED" "Hetzner bridge is back to healthy on all three probes. Prior state: ${prior_state}"
            echo "healthy" > "${STATE_FILE}"
            ;;
    esac
else
    case "${prior_state}" in
        unhealthy:*)
            log "still unhealthy (${prior_state}) — suppressing duplicate alert"
            ;;
        *)
            local_ts="$(date -Is)"
            log "TRANSITION healthy → unhealthy at ${local_ts}"
            send_alert "Bridge health: UNHEALTHY" "Hetzner bridge is failing one of /health, /v1/db/health, /v1/models. Last probe at ${local_ts}. Check container status: docker ps --filter name=eco-wrapper"
            echo "unhealthy:${local_ts}" > "${STATE_FILE}"
            ;;
    esac
fi
