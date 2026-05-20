#!/usr/bin/env bash
# Bridge DB Setup — reproduzierbares Anlegen des Schemas + Test-Daten.
#
# Phasen:
#   1. Health Check  — Postgres erreichbar via GET /v1/db/health
#   2. Migrations    — bin/bridge-migrate.sh (fail-fast)
#   3. Tenants Seed  — POST /v1/tenants (idempotent via 409)
#   4. Users Seed    — POST /v1/users   (idempotent via 409)
#   5. Verification  — GET  /v1/users?account_type=test
#
# Voraussetzungen:
#   - BRIDGE_SERVICE_TOKEN muss als Umgebungsvariable gesetzt sein
#   - BRIDGE_URL (optional, default: http://localhost:8000)
#   - Docker-Container bridge-postgres-prod muss laufen (für Migrations)
#   - python3 + requests
#
# Usage:
#   BRIDGE_SERVICE_TOKEN=<token> ./bridge-setup-db.sh
#   BRIDGE_SERVICE_TOKEN=<token> ./bridge-setup-db.sh --verify-only
#
# NOTE: Login-Verifikation (POST /v1/auth/login) fehlt noch — dieser Endpoint
# existiert auf der Bridge noch nicht. Verifikation prüft nur User-Existenz
# via GET /v1/users. Wird nachgezogen sobald /v1/auth/login verfügbar ist.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CREDENTIALS_JSON="${REPO_ROOT}/config/test-credentials.json"
MIGRATE_SCRIPT="${SCRIPT_DIR}/bridge-migrate.sh"

BRIDGE_URL="${BRIDGE_URL:-http://localhost:8000}"
VERIFY_ONLY=false

for arg in "$@"; do
  case "${arg}" in
    --verify-only) VERIFY_ONLY=true ;;
    *) printf "ERROR: Unknown argument: %s\n" "${arg}" >&2; exit 1 ;;
  esac
done

# ---- colours ----------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
BOLD='\033[1m'; RESET='\033[0m'
log_info()    { printf "  ${BLUE}[info]${RESET}  %s\n" "$*"; }
log_success() { printf "  ${GREEN}[ok]${RESET}    %s\n" "$*"; }
log_warn()    { printf "  ${YELLOW}[warn]${RESET}  %s\n" "$*"; }
log_error()   { printf "  ${RED}[FAIL]${RESET}  %s\n" "$*" >&2; }
phase_header() { printf "\n${BOLD}=== Phase %s: %s ===${RESET}\n" "$1" "$2"; }

# ---- guards -----------------------------------------------------------------
if [ -z "${BRIDGE_SERVICE_TOKEN:-}" ]; then
  log_error "BRIDGE_SERVICE_TOKEN is not set — cannot authenticate against admin endpoints"
  log_error "Set it in the environment before running this script:"
  log_error "  BRIDGE_SERVICE_TOKEN=<token> $0"
  exit 1
fi

if ! command -v python3 &>/dev/null; then
  log_error "python3 not found — required for HTTP calls and JSON parsing"
  exit 1
fi

python3 -c "import requests" 2>/dev/null || {
  log_error "Python 'requests' library not found — run: pip3 install requests"
  exit 1
}

[ -f "${CREDENTIALS_JSON}" ] || {
  log_error "test-credentials.json not found: ${CREDENTIALS_JSON}"
  exit 1
}

[ -f "${MIGRATE_SCRIPT}" ] || {
  log_error "bridge-migrate.sh not found: ${MIGRATE_SCRIPT}"
  exit 1
}

echo ""
printf "${BOLD}Bridge DB Setup${RESET}\n"
printf "  URL:         %s\n" "${BRIDGE_URL}"
printf "  Credentials: %s\n" "${CREDENTIALS_JSON}"
printf "  Verify-only: %s\n" "${VERIFY_ONLY}"

# ============================================================================
# PHASE 1: HEALTH CHECK
# ============================================================================
phase_header 1 "HEALTH CHECK"

log_info "GET ${BRIDGE_URL}/v1/db/health ..."
HEALTH_RESPONSE=$(curl -sf --max-time 10 "${BRIDGE_URL}/v1/db/health" 2>&1) || {
  log_error "Health check failed — is the Bridge running at ${BRIDGE_URL}?"
  log_error "Response: ${HEALTH_RESPONSE:-<no response>}"
  exit 1
}

DB_STATUS=$(echo "${HEALTH_RESPONSE}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null \
  || echo "")
if [ "${DB_STATUS}" != "healthy" ]; then
  log_error "DB health status: '${DB_STATUS}' (expected 'healthy')"
  log_error "Response: ${HEALTH_RESPONSE}"
  exit 1
fi
log_success "DB healthy"

# ============================================================================
# PHASE 2: MIGRATIONS
# ============================================================================
phase_header 2 "MIGRATIONS"

if [ "${VERIFY_ONLY}" = true ]; then
  log_info "Skipping migrations (--verify-only)"
else
  log_info "Running bridge-migrate.sh ..."
  if ! bash "${MIGRATE_SCRIPT}"; then
    log_error "bridge-migrate.sh failed — see output above"
    exit 1
  fi
  log_success "Migrations applied"
fi

# ============================================================================
# PHASE 3: SEED TENANTS
# ============================================================================
phase_header 3 "SEED TENANTS"

if [ "${VERIFY_ONLY}" = true ]; then
  log_info "Skipping tenant seeding (--verify-only)"
else
  python3 - <<PYEOF
import json, sys, requests

creds = json.load(open('${CREDENTIALS_JSON}'))
tenants = creds.get('tenants', {})
if not tenants:
    print("  [FAIL]  No tenants defined in test-credentials.json", file=sys.stderr)
    sys.exit(1)

headers = {
    'Content-Type': 'application/json',
    'X-Bridge-Service-Token': '${BRIDGE_SERVICE_TOKEN}',
}
ok = True

for key, t in tenants.items():
    payload = {
        'id': t['id'],
        'name': t['name'],
        'account_type': t.get('account_type', 'test'),
    }
    try:
        r = requests.post('${BRIDGE_URL}/v1/tenants', json=payload, headers=headers, timeout=15)
    except requests.RequestException as e:
        print(f"  [FAIL]  Tenant {key}: request error — {e}", file=sys.stderr)
        ok = False
        continue

    if r.status_code == 201:
        print(f"  [ok]    Created tenant: {key} ({t['id']})")
    elif r.status_code == 409:
        print(f"  [ok]    Already exists (idempotent): {key} ({t['id']})")
    else:
        print(f"  [FAIL]  Tenant {key}: HTTP {r.status_code} — {r.text}", file=sys.stderr)
        ok = False

sys.exit(0 if ok else 1)
PYEOF
  log_success "Tenants seeded"
fi

# ============================================================================
# PHASE 4: SEED USERS
# ============================================================================
phase_header 4 "SEED USERS"

if [ "${VERIFY_ONLY}" = true ]; then
  log_info "Skipping user seeding (--verify-only)"
else
  python3 - <<PYEOF
import json, sys, requests

creds = json.load(open('${CREDENTIALS_JSON}'))
users = creds.get('users', {})
if not users:
    print("  [FAIL]  No users defined in test-credentials.json", file=sys.stderr)
    sys.exit(1)

headers = {
    'Content-Type': 'application/json',
    'X-Bridge-Service-Token': '${BRIDGE_SERVICE_TOKEN}',
}
ok = True

for key, u in users.items():
    payload = {
        'email': u['email'],
        'name': u['name'],
        'tenant_id': u['tenant_id'],
        'password': u.get('password'),
    }
    try:
        r = requests.post('${BRIDGE_URL}/v1/users', json=payload, headers=headers, timeout=15)
    except requests.RequestException as e:
        print(f"  [FAIL]  User {key}: request error — {e}", file=sys.stderr)
        ok = False
        continue

    if r.status_code == 201:
        print(f"  [ok]    Created user: {key} ({u['email']})")
    elif r.status_code == 409:
        print(f"  [ok]    Already exists (idempotent): {key} ({u['email']})")
    else:
        print(f"  [FAIL]  User {key}: HTTP {r.status_code} — {r.text}", file=sys.stderr)
        ok = False

sys.exit(0 if ok else 1)
PYEOF
  log_success "Users seeded"
fi

# ============================================================================
# PHASE 5: VERIFICATION
# ============================================================================
phase_header 5 "VERIFICATION"

log_info "GET ${BRIDGE_URL}/v1/users?account_type=test ..."

python3 - <<PYEOF
import json, sys, requests

creds = json.load(open('${CREDENTIALS_JSON}'))
expected_emails = {u['email'] for u in creds.get('users', {}).values()}

headers = {'X-Bridge-Service-Token': '${BRIDGE_SERVICE_TOKEN}'}
try:
    r = requests.get(
        '${BRIDGE_URL}/v1/users',
        params={'account_type': 'test'},
        headers=headers,
        timeout=15,
    )
except requests.RequestException as e:
    print(f"  [FAIL]  GET /v1/users failed: {e}", file=sys.stderr)
    sys.exit(1)

if r.status_code != 200:
    print(f"  [FAIL]  GET /v1/users returned HTTP {r.status_code}: {r.text}", file=sys.stderr)
    sys.exit(1)

found_emails = {u['email'] for u in r.json()}
missing = expected_emails - found_emails
ok = not missing

for u in r.json():
    if u['email'] in expected_emails:
        print(f"  [ok]    {u['email']}  (id={u['id']})")

for email in sorted(missing):
    print(f"  [FAIL]  Missing: {email}", file=sys.stderr)

# NOTE: Login-Verifikation (POST /v1/auth/login) ist hier ausgelassen —
# der Endpoint existiert auf der Bridge noch nicht (kommt in einer späteren Phase).
print(f"\n  Users found: {len(found_emails & expected_emails)}/{len(expected_emails)}")
sys.exit(0 if ok else 1)
PYEOF

log_success "Verification passed"

echo ""
printf "${GREEN}${BOLD}Bridge DB setup complete.${RESET}\n"
echo ""
exit 0
