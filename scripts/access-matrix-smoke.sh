#!/usr/bin/env bash
# access-matrix-smoke.sh — proves that a REAL customer could log in and reach
# their app, not just that the Bridge answered 200.
#
# WHY THIS EXISTS (Rafael, 17.08.2026)
# -------------------------------------------------------------------------
# Bridge deploys have repeatedly locked apps out at the entitlement layer while
# every existing check stayed green: Bridge login 200, license visible in the
# admin cockpit — and the app's own /dashboard still 307'd to
# "no license"/"expired" for a real user (lesson from 2026-08-13, see the
# global CLAUDE.md section "App-Zugaenge & Abos"). The Bridge's own
# bridge_smoke.py (called from bridge-deploy.sh) only proves the Bridge's own
# endpoints are reachable — it authenticates as nobody in particular and never
# walks an app's login → entitlement → dashboard chain. That chain is the one
# that has actually broken. This script closes that gap.
#
# A Bridge-login 200 is NOT proof of access — it is also 200 for a user whose
# entitlement is broken (see the App-Zugaenge doctrine). The only valid proof
# is the real app's protected page returning 200 for a real, entitled session.
#
# WHAT IT DOES, per app (report / energy / noise — the three apps whose
# frontend authenticates through the Bridge; engelmann is Supabase-auth and
# out of scope; werking-safety is not in active focus, see global CLAUDE.md):
#   1. Resolve the app's production URL from packages/config/env-contracts.json
#      `production_url` — the SAME field orchestrator/bin/deploy-production's
#      own login smoke uses. Deliberately NOT `frontend_urls.prod`: those
#      custom-domain entries (e.g. report.werkingflow.com) do not resolve in
#      public DNS as of 2026-08-17 (verified: `dig @8.8.8.8` → NXDOMAIN at the
#      .com zone, no delegation) — aspirational config, not live routing (see
#      memory project_werking_tools_domain_stale_autoassign_off). Trusting it
#      would make this script fail loud for the wrong reason.
#   2. Resolve a canary user's credentials from Infisical <app>/prod
#      SMOKE_LOGIN_EMAIL / SMOKE_LOGIN_PASSWORD — the SAME convention
#      orchestrator/bin/deploy-production already established for its
#      single-app login smoke. NEVER invented here, NEVER an active user's
#      password, NEVER a freshly created account (that is a commercial act —
#      gated on Rafael, see global CLAUDE.md "App-Zugaenge & Abos").
#   3. POST the canary's credentials to the app's own /api/auth/login,
#      capture the wf-session cookie exactly like a browser would
#      (packages/auth/src/session-cookie.ts — SESSION_COOKIE_NAME=wf-session,
#      identical across report/energy/noise).
#   4. GET the app's protected page WITH that cookie and require a real,
#      un-redirected HTTP 200. A 30x to /login or /sortiment?reason=... means
#      the login worked but the entitlement did not — exactly the failure
#      mode that stayed invisible for days on 2026-08-13.
#
# An app with NO canary configured is NOT silently skipped as a pass — it
# is reported as UNVERIFIED and keeps the whole matrix from being green,
# because "we never checked" is not the same claim as "it works". Provisioning
# a missing canary is a decision for Rafael (see global CLAUDE.md), not this
# script — it documents the gap and stops there.
#
# CALLING CONTRACT (fixed by orchestrator/bin/deploy-production's "bridge"
# path, commits a204fd2+db817b0 — see memory deploy-production-bridge-gate;
# do NOT rename this script or its argument shape without updating that
# caller too):
#   scripts/access-matrix-smoke.sh <hetzner|server2>            # human output
#   scripts/access-matrix-smoke.sh <hetzner|server2> --json     # machine-readable
#
# The host argument identifies which bridge host was just deployed — it is
# validated and logged, but does NOT change which apps get probed: report/
# energy/noise each have exactly one production entitlement configuration
# (whichever bridge their prod AI_BRIDGE_URL currently points to), so the
# real login→dashboard chain already exercises whatever is actually serving
# that app right now. There is no separate "hetzner-flavoured" or
# "server2-flavoured" app entitlement to probe differently.
#
# EXIT CODES
#   0 = every app has a canary AND its login→dashboard chain returned a real 200
#   1 = at least one app FAILED (canary rejected, or entitled page not reachable)
#       or is UNVERIFIED (no canary configured in Infisical)
#   2 = the check itself could not run (bad usage, Infisical unreachable,
#       contract file missing/unparseable, curl/network plumbing broken) —
#       distinct from "1" because it says nothing about whether access works
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_CONTRACTS="/root/projekte/werkingflow-production/packages/config/env-contracts.json"

HOST_ARG="${1:-}"
case "$HOST_ARG" in
    hetzner|server2) ;;
    *)
        echo "Usage: access-matrix-smoke.sh <hetzner|server2> [--json]" >&2
        exit 2
        ;;
esac
shift
JSON_OUT=false
[[ "${1:-}" == "--json" ]] && JSON_OUT=true

log()  { [[ "$JSON_OUT" == "true" ]] || echo "[access-matrix:${HOST_ARG}] $*"; }
fail() { [[ "$JSON_OUT" == "true" ]] || echo "[access-matrix:${HOST_ARG}] FAIL: $*" >&2; }

if [[ ! -f "$ENV_CONTRACTS" ]]; then
    echo "[access-matrix] ABORT: contract file missing: ${ENV_CONTRACTS}" >&2
    exit 2
fi

# shellcheck source=/root/.infisical/infisical-api.sh
if ! source /root/.infisical/infisical-api.sh 2>/dev/null; then
    echo "[access-matrix] ABORT: cannot source /root/.infisical/infisical-api.sh" >&2
    exit 2
fi

# app_key | infisical get_infisical_ws_id key | protected path proving real access
#
# report/energy have a dedicated /dashboard. noise has NO /dashboard route at
# all (verified against its App Router tree) — its landing page "/" is itself
# behind the auth+entitlement middleware (not in PUBLIC_ROUTES), so "/" is the
# correct protected target there. Hardcoded here deliberately: this is the one
# per-app fact this script needs and it changes only on a conscious routing
# decision, not on every deploy — an SSoT lookup would be over-engineering for
# three lines.
APPS=(
    "werking-report|werking-report|/dashboard"
    "werking-energy|werking-energy|/dashboard"
    "werking-noise|werking-noise|/"
)

results_json="["
overall_rc=0
first_json=true

for entry in "${APPS[@]}"; do
    IFS='|' read -r app infisical_key protected_path <<< "$entry"

    base_url=$(python3 -c "
import json
d = json.load(open('${ENV_CONTRACTS}'))
a = d.get('apps', {}).get('${app}', {})
print(a.get('production_url') or '')
" 2>/dev/null)
    if [[ -z "$base_url" ]]; then
        fail "${app}: no production_url in ${ENV_CONTRACTS} — cannot probe"
        overall_rc=1
        [[ "$JSON_OUT" == "true" ]] && { [[ "$first_json" == "true" ]] || results_json+=","; first_json=false; results_json+="{\"app\":\"${app}\",\"status\":\"ERROR\",\"reason\":\"no production_url\"}"; }
        continue
    fi

    ws_id=$(get_infisical_ws_id "$infisical_key" 2>/dev/null)
    smoke_email=""; smoke_pw=""
    if [[ -n "$ws_id" ]]; then
        smoke_email=$(infisical_get_secret "$ws_id" prod SMOKE_LOGIN_EMAIL 2>/dev/null | tr -d '[:space:]' || true)
        smoke_pw=$(infisical_get_secret "$ws_id" prod SMOKE_LOGIN_PASSWORD 2>/dev/null | tr -d '[:space:]' || true)
    fi

    if [[ -z "$smoke_email" || -z "$smoke_pw" ]]; then
        fail "${app}: UNVERIFIED — no SMOKE_LOGIN_EMAIL/PASSWORD in Infisical ${infisical_key}/prod." \
             "This app is NOT covered by the access matrix until a canary user with a real, active" \
             "subscription is provisioned (Rafael decision, see global CLAUDE.md 'App-Zugaenge & Abos')."
        overall_rc=1
        [[ "$JSON_OUT" == "true" ]] && { [[ "$first_json" == "true" ]] || results_json+=","; first_json=false; results_json+="{\"app\":\"${app}\",\"status\":\"UNVERIFIED\",\"reason\":\"no canary configured\"}"; }
        continue
    fi

    cookiejar=$(mktemp)

    login_payload=$(python3 -c 'import json,sys; print(json.dumps({"email":sys.argv[1],"password":sys.argv[2]}))' "$smoke_email" "$smoke_pw")
    login_resp=$(curl -sS -c "$cookiejar" -w $'\n%{http_code}' --max-time 20 \
        -X POST "${base_url}/api/auth/login" \
        -H "Content-Type: application/json" -d "$login_payload" 2>&1) || login_resp=$'\n000'
    login_code=$(printf '%s' "$login_resp" | tail -1)
    login_body=$(printf '%s' "$login_resp" | sed '$d')

    if ! echo "$login_body" | grep -q '"success":true'; then
        fail "${app}: login as ${smoke_email} REJECTED (HTTP ${login_code}) — canary credentials stale," \
             "or auth chain (route/Bridge) broken. Response: $(echo "$login_body" | head -c 200)"
        overall_rc=1
        [[ "$JSON_OUT" == "true" ]] && { [[ "$first_json" == "true" ]] || results_json+=","; first_json=false; results_json+="{\"app\":\"${app}\",\"status\":\"FAIL\",\"reason\":\"login rejected HTTP ${login_code}\"}"; }
        rm -f "$cookiejar"
        continue
    fi

    dash_resp=$(curl -sS -b "$cookiejar" -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 20 \
        "${base_url}${protected_path}" 2>&1) || dash_resp="000 "
    dash_code=$(echo "$dash_resp" | awk '{print $1}')
    dash_redirect=$(echo "$dash_resp" | cut -d' ' -f2-)
    rm -f "$cookiejar"

    if [[ "$dash_code" == "200" ]]; then
        log "${app}: OK — ${smoke_email} logged in and reached ${protected_path} (HTTP 200)"
        [[ "$JSON_OUT" == "true" ]] && { [[ "$first_json" == "true" ]] || results_json+=","; first_json=false; results_json+="{\"app\":\"${app}\",\"status\":\"OK\"}"; }
    else
        fail "${app}: login OK but ${protected_path} did NOT return 200 (HTTP ${dash_code}" \
             "$( [[ -n "$dash_redirect" ]] && echo "→ ${dash_redirect}")) — entitlement broken for a" \
             "logged-in user. This is exactly the 2026-08-13 failure class: Bridge/login green, app closed."
        overall_rc=1
        [[ "$JSON_OUT" == "true" ]] && { [[ "$first_json" == "true" ]] || results_json+=","; first_json=false; results_json+="{\"app\":\"${app}\",\"status\":\"FAIL\",\"reason\":\"protected path HTTP ${dash_code}\",\"redirect\":\"${dash_redirect}\"}"; }
    fi
done

results_json+="]"

if [[ "$JSON_OUT" == "true" ]]; then
    echo "$results_json"
else
    echo
    if [[ "$overall_rc" -eq 0 ]]; then
        log "ACCESS MATRIX GREEN — every app has a canary and a real entitled user reaches it."
    else
        log "ACCESS MATRIX NOT GREEN — see FAIL/UNVERIFIED lines above."
    fi
fi

exit "$overall_rc"
