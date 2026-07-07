#!/usr/bin/env bash
# sync-principals.sh — distribute service_principals as a UNION across all
# bridge hosts (Rafael-Entscheid 2026-07-07, nach dem Energy-Phase-4-Incident).
#
# MODEL: the two (later N) bridge hosts form ONE failover fabric — nginx's
# claude_production pool cascades cross-host (prod workers first, then dev
# workers, only then the 429 stop sign). For that to work, EVERY host's
# kassenhäuschen must recognize EVERY valid vignette: each host's
# service_principals table must contain the union of all principals.
# Naming convention since the 2026-07-07 reconcile: env-scoped names
# ('energy-dev', 'energy-prod', ...) so each name maps to exactly ONE token.
#
# WHAT THIS DOES (conservative by design):
#   * reads active principals from all hosts
#   * FAILS LOUD if the same name carries different token_hash/scope on
#     different hosts (a rotation landed on one host only — a human must
#     decide which token wins; this script never guesses about auth material)
#   * inserts rows whose NAME is entirely absent on a host (never updates,
#     never deletes, never reactivates — deactivation stays a deliberate
#     per-incident human action and shows up in check-principal-drift.sh)
#
# Runs standalone or as bridge-deploy.sh Phase 3.6 (before the drift check,
# which afterwards acts as the verification that the union converged).
#
# Exit: 0 = converged (possibly after inserts) · 1 = conflict, human needed
#       · 2 = query/connection error.
set -euo pipefail

# Hosts: keep in sync with bridge-deploy.sh / check-principal-drift.sh.
HOSTS=("49.12.72.66" "178.104.178.79")
PG_CONTAINER="bridge-postgres-prod"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15"
DRY_RUN="${DRY_RUN:-false}"

psql_on() { # host, sql-on-stdin
  sudo -n ssh $SSH_OPTS "root@${1}" \
    "docker exec -i ${PG_CONTAINER} psql -U bridge -d bridge -tA -v ON_ERROR_STOP=1"
}

# ── 1. Collect: active rows per host, plus ALL names per host (any state, so
#      we never re-insert a deliberately deactivated principal). ─────────────
# Row format: name<TAB>token_hash<TAB>token_prefix<TAB>apps<TAB>paths<TAB>cap
ACTIVE_SQL="SELECT name || E'\t' || token_hash || E'\t' || token_prefix || E'\t' ||
array_to_string(allowed_apps, ',') || E'\t' || array_to_string(allowed_paths, ',') || E'\t' ||
COALESCE(monthly_cap_eur::text, 'NULL') FROM service_principals WHERE active ORDER BY name;"
ALLNAMES_SQL="SELECT name FROM service_principals ORDER BY name;"

declare -A ACTIVE ALLNAMES
for h in "${HOSTS[@]}"; do
  ACTIVE["$h"]=$(printf '%s' "$ACTIVE_SQL" | psql_on "$h") || { echo "ERROR: query failed on $h" >&2; exit 2; }
  ALLNAMES["$h"]=$(printf '%s' "$ALLNAMES_SQL" | psql_on "$h") || { echo "ERROR: query failed on $h" >&2; exit 2; }
done

# ── 2. Union by name + conflict detection. ──────────────────────────────────
declare -A UNION   # name → full row
conflict=0
for h in "${HOSTS[@]}"; do
  while IFS= read -r row; do
    [[ -z "$row" ]] && continue
    name="${row%%$'\t'*}"
    if [[ -n "${UNION[$name]:-}" && "${UNION[$name]}" != "$row" ]]; then
      conflict=1
      echo "❌ CONFLICT for principal '${name}': hosts disagree on token/scope." >&2
      echo "   Never auto-resolved — rotate/reconcile deliberately, then re-run." >&2
    else
      UNION["$name"]="$row"
    fi
  done <<< "${ACTIVE[$h]}"
done
if [[ $conflict -ne 0 ]]; then
  echo "PRINCIPAL_SYNC_CONFLICT: no rows written." >&2
  exit 1
fi

# ── 3. Insert missing names per host (transaction, idempotent). ─────────────
total_inserts=0
for h in "${HOSTS[@]}"; do
  stmt="BEGIN;"
  n=0
  for name in "${!UNION[@]}"; do
    grep -qxF "$name" <<< "${ALLNAMES[$h]}" && continue
    IFS=$'\t' read -r _ hash prefix apps paths cap <<< "${UNION[$name]}"
    stmt+="
INSERT INTO service_principals (name, token_hash, token_prefix, allowed_apps, allowed_paths, monthly_cap_eur, active)
VALUES ('${name}', '${hash}', '${prefix}', '{${apps}}', '{${paths}}', ${cap}, true)
ON CONFLICT (name) DO NOTHING;"
    n=$((n+1))
  done
  stmt+="
COMMIT;"
  if (( n == 0 )); then
    echo "  ${h}: complete (nothing to insert)"
    continue
  fi
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "  ${h}: [DRY-RUN] would insert ${n} principal(s)"
  else
    printf '%s' "$stmt" | psql_on "$h" > /dev/null || { echo "ERROR: insert failed on $h" >&2; exit 2; }
    echo "  ${h}: inserted ${n} principal(s)"
    total_inserts=$((total_inserts + n))
  fi
done

echo "PRINCIPAL_SYNC_OK: union of ${#UNION[@]} active principals on ${#HOSTS[@]} hosts (${total_inserts} inserted)."
exit 0
