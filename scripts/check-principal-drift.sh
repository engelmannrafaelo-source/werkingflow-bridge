#!/usr/bin/env bash
# check-principal-drift.sh — fail-loud detector for service-principal drift
# between the independent per-host Bridge Postgres DBs.
#
# WHY: the two prod hosts run SEPARATE Postgres instances. Service principals
# (per-caller inbound identities) are written by the admin CRUD / rotate paths
# into whichever host's DB served the request — there is no cross-host sync. So a
# principal created/rotated on one host is UNKNOWN on the other, and a caller
# authenticating with that principal gets a 401 "Invalid API key" there. This
# already bit a deploy: the 'dev-tooling' principal existed on the primary but
# not on server2, so the deploy smoke (which auths as dev-tooling) failed on
# server2 and rolled the deploy back — mis-read at first as a service outage.
#
# This check compares the set of ACTIVE principals (name + token_hash fingerprint
# + scope) across all hosts and FAILS LOUD on any difference. Wire it into the
# deploy (or run it standalone) so principal drift is surfaced explicitly the
# moment it appears, instead of silently causing per-host 401s. It is READ-ONLY
# by design — auto-syncing auth rows across prod DBs is deliberately NOT done here
# (a human decides how to reconcile; see the SSoT-reconcile pattern used for
# AI_BRIDGE_API_KEY in bridge-deploy.sh Phase 3.5).
#
# Exit: 0 = all hosts consistent · 1 = drift detected · 2 = query error.
set -euo pipefail

# Hosts: keep in sync with bridge-deploy.sh (HETZNER_HOST / SERVER2_HOST).
HOSTS=("49.12.72.66" "178.104.178.79")
PG_CONTAINER="bridge-postgres-prod"

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15"
# Fingerprint an active principal by name + first 12 hex of its token_hash + its
# scope. Full token_hash is never printed (it is a credential digest).
QUERY="SELECT name || '|' || left(token_hash,12) || '|apps=' || array_to_string(allowed_apps,',') \
FROM service_principals WHERE active ORDER BY name;"

# Dev-only principals (principals-dev-only.txt) exist on the dev host by design
# and are left out of the comparison — named on every run instead of silenced.
DEV_ONLY_FILE="${DEV_ONLY_FILE:-$(dirname "${BASH_SOURCE[0]}")/principals-dev-only.txt}"
[[ -f "$DEV_ONLY_FILE" ]] || { echo "ERROR: dev-only list missing: $DEV_ONLY_FILE" >&2; exit 2; }
DEV_ONLY=$(sed -e 's/#.*//' -e 's/[[:space:]]//g' "$DEV_ONLY_FILE" | grep -v '^$' || true)

declare -A SNAP
LEAK=0
for h in "${HOSTS[@]}"; do
  out=$(sudo -n ssh $SSH_OPTS "root@${h}" \
        "docker exec ${PG_CONTAINER} psql -U bridge -d bridge -tAc \"${QUERY}\"" 2>&1) || {
    echo "ERROR: could not query service_principals on ${h}:" >&2
    echo "${out}" >&2
    exit 2
  }
  # A dev-only name on any other host is a leak, not by-design asymmetry.
  if [[ "$h" != "${HOSTS[0]}" ]]; then
    leaked=$(printf '%s\n' "$out" | awk -F'|' 'NR==FNR { if ($0 != "") skip[$0]=1; next } ($1 in skip) { print $1 }' <(printf '%s\n' "$DEV_ONLY") -)
    if [[ -n "$leaked" ]]; then
      echo "❌ DEV-ONLY PRINCIPAL ACTIVE ON ${h}: $(tr '\n' ' ' <<< "$leaked")— must not exist off the dev bridge (principals-dev-only.txt; e-pva-erkunder-prodort). Deactivate it there deliberately." >&2
      LEAK=1
    fi
  fi
  SNAP["$h"]="$(printf '%s\n' "$out" | grep -v '^[[:space:]]*$' \
    | awk -F'|' 'NR==FNR { if ($0 != "") skip[$0]=1; next } !($1 in skip)' <(printf '%s\n' "$DEV_ONLY") - \
    | sort)"
done

ref_host="${HOSTS[0]}"
drift=${LEAK:-0}
for h in "${HOSTS[@]:1}"; do
  if [[ "${SNAP[$ref_host]}" != "${SNAP[$h]}" ]]; then
    drift=1
    echo "⚠️  PRINCIPAL DRIFT between ${ref_host} and ${h}:" >&2
    echo "--- only on ${ref_host} ---" >&2
    comm -23 <(printf '%s\n' "${SNAP[$ref_host]}") <(printf '%s\n' "${SNAP[$h]}") >&2 || true
    echo "--- only on ${h} ---" >&2
    comm -13 <(printf '%s\n' "${SNAP[$ref_host]}") <(printf '%s\n' "${SNAP[$h]}") >&2 || true
  fi
done

if [[ $drift -ne 0 ]]; then
  echo "PRINCIPAL_DRIFT: hosts disagree on active service_principals — a caller valid on one host will 401 on another. Reconcile deliberately (do not ignore)." >&2
  exit 1
fi

if [[ -n "$DEV_ONLY" ]]; then
  echo "NOTE: dev-only principals excluded from parity (principals-dev-only.txt; Prod-Ort offen bei Rafael, e-pva-erkunder-prodort): $(tr '\n' ' ' <<< "$DEV_ONLY")" >&2
fi
n=$(printf '%s\n' "${SNAP[$ref_host]}" | grep -c . || true)
echo "PRINCIPAL_OK: all ${#HOSTS[@]} hosts agree on ${n} active service_principals."
exit 0
