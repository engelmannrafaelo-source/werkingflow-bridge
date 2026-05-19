#!/usr/bin/env bash
# seed-bridge-users.sh <app>
#
# Seeds the central Bridge DB with test tenants + users from a production app's
# config/test-credentials.json. Idempotent (409 = already exists → ok).
#
# Tenant strategy:
#   - All tenants declared in credentials get account_type=test (idempotent).
#   - Users with no tenantId are assigned to the app-default tenant (<app>-test).
#   - The default tenant is also created with account_type=test.
#
# Prerequisites:
#   BRIDGE_SERVICE_TOKEN  — X-Bridge-Service-Token for admin endpoints
#   BRIDGE_URL            — optional, default http://localhost:8000
#
# Usage:
#   BRIDGE_SERVICE_TOKEN=<token> ./seed-bridge-users.sh werking-report
#   BRIDGE_SERVICE_TOKEN=<token> ./seed-bridge-users.sh werking-energy
#   BRIDGE_SERVICE_TOKEN=<token> BRIDGE_URL=http://49.12.72.66:8000 ./seed-bridge-users.sh werking-report

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS_ROOT="/root/projekte/werkingflow-production/apps"

# ---- Argument check ---------------------------------------------------------
if [ $# -ne 1 ]; then
  printf "Usage: %s <app>\n" "$(basename "$0")" >&2
  printf "  <app>  One of: werking-report, werking-energy, werking-safety, ...\n" >&2
  exit 1
fi

APP="$1"
CREDENTIALS_JSON="${APPS_ROOT}/${APP}/config/test-credentials.json"
BRIDGE_URL="${BRIDGE_URL:-http://localhost:8000}"
DEFAULT_TENANT_ID="${APP}-test"

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
  log_error "BRIDGE_SERVICE_TOKEN is not set"
  log_error "Usage: BRIDGE_SERVICE_TOKEN=<token> $0 <app>"
  exit 1
fi

if ! command -v python3 &>/dev/null; then
  log_error "python3 not found"
  exit 1
fi
python3 -c "import requests" 2>/dev/null || {
  log_error "Python 'requests' not found — run: pip3 install requests"
  exit 1
}

if [ ! -f "${CREDENTIALS_JSON}" ]; then
  log_error "test-credentials.json not found: ${CREDENTIALS_JSON}"
  exit 1
fi

echo ""
printf "${BOLD}Bridge User Seed — ${APP}${RESET}\n"
printf "  URL:         %s\n" "${BRIDGE_URL}"
printf "  Credentials: %s\n" "${CREDENTIALS_JSON}"
printf "  Default tenant: %s\n" "${DEFAULT_TENANT_ID}"

# ============================================================================
# PHASE 1: HEALTH CHECK
# ============================================================================
phase_header 1 "HEALTH CHECK"

log_info "GET ${BRIDGE_URL}/v1/db/health ..."
HEALTH_RESPONSE=$(curl -sf --max-time 10 "${BRIDGE_URL}/v1/db/health" 2>&1) || {
  log_error "Health check failed — is the Bridge running at ${BRIDGE_URL}?"
  exit 1
}
DB_STATUS=$(echo "${HEALTH_RESPONSE}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
if [ "${DB_STATUS}" != "healthy" ]; then
  log_error "DB health status: '${DB_STATUS}' (expected 'healthy')"
  exit 1
fi
log_success "DB healthy"

# ============================================================================
# PHASE 2: SEED TENANTS
#
# Strategy:
#   1. Collect all unique tenantIds from users (those with an explicit tenantId).
#   2. Add the app-default tenant (for users that have no tenantId).
#   3. Create each tenant with account_type=test (idempotent via 409).
# ============================================================================
phase_header 2 "SEED TENANTS"

python3 - <<PYEOF
import json, sys, requests

creds = json.load(open('${CREDENTIALS_JSON}'))
users = creds.get('users', {})
headers = {
    'Content-Type': 'application/json',
    'X-Bridge-Service-Token': '${BRIDGE_SERVICE_TOKEN}',
}

# Collect declared tenantIds from users + add app-default for users without tenantId
declared_ids = set()
has_users_without_tenant = False

for key, u in users.items():
    tid = u.get('tenantId')
    if tid:
        declared_ids.add(tid)
    else:
        has_users_without_tenant = True

# Default tenant always created (even if all users have explicit tenantIds,
# it may be needed for future users or re-seeds).
tenants_to_create = []
for tid in sorted(declared_ids):
    tenants_to_create.append({'id': tid, 'name': tid, 'account_type': 'test'})

# App-default tenant (for users without tenantId)
default_tid = '${DEFAULT_TENANT_ID}'
if default_tid not in declared_ids:
    tenants_to_create.insert(0, {'id': default_tid, 'name': f'${APP} test tenant', 'account_type': 'test'})

if not tenants_to_create:
    print('  [warn]  No tenants to create')
    sys.exit(0)

ok = True
for t in tenants_to_create:
    payload = {'id': t['id'], 'name': t['name'], 'account_type': t['account_type']}
    try:
        r = requests.post('${BRIDGE_URL}/v1/tenants', json=payload, headers=headers, timeout=15)
    except requests.RequestException as e:
        print(f"  [FAIL]  Tenant {t['id']}: request error — {e}", file=sys.stderr)
        ok = False
        continue

    if r.status_code == 201:
        print(f"  [ok]    Created tenant: {t['id']} (account_type=test)")
    elif r.status_code == 409:
        print(f"  [ok]    Already exists (idempotent): {t['id']}")
    else:
        print(f"  [FAIL]  Tenant {t['id']}: HTTP {r.status_code} — {r.text}", file=sys.stderr)
        ok = False

sys.exit(0 if ok else 1)
PYEOF
log_success "Tenants seeded"

# ============================================================================
# PHASE 3: SEED USERS
#
# Users without tenantId are assigned to the app-default tenant.
# All other users use their declared tenantId.
# ============================================================================
phase_header 3 "SEED USERS"

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
default_tenant_id = '${DEFAULT_TENANT_ID}'
ok = True

for key, u in users.items():
    tenant_id = u.get('tenantId') or default_tenant_id
    payload = {
        'email':     u['email'],
        'name':      u['name'],
        'tenant_id': tenant_id,
        'password':  u.get('password'),
        'role':      u.get('role', 'user'),
    }
    try:
        r = requests.post('${BRIDGE_URL}/v1/users', json=payload, headers=headers, timeout=15)
    except requests.RequestException as e:
        print(f"  [FAIL]  User {key}: request error — {e}", file=sys.stderr)
        ok = False
        continue

    if r.status_code == 201:
        print(f"  [ok]    Created: {key} ({u['email']}) → tenant={tenant_id}")
    elif r.status_code == 409:
        print(f"  [ok]    Already exists (idempotent): {key} ({u['email']})")
    else:
        print(f"  [FAIL]  User {key}: HTTP {r.status_code} — {r.text}", file=sys.stderr)
        ok = False

sys.exit(0 if ok else 1)
PYEOF
log_success "Users seeded"

# ============================================================================
# PHASE 4: VERIFICATION
#
# Check that all expected users are reachable in the Bridge.
# Only verifies emails declared in test-credentials.json.
# ============================================================================
phase_header 4 "VERIFICATION"

python3 - <<PYEOF
import json, sys, requests

creds = json.load(open('${CREDENTIALS_JSON}'))
expected_emails = {u['email'] for u in creds.get('users', {}).values()}

headers = {'X-Bridge-Service-Token': '${BRIDGE_SERVICE_TOKEN}'}
found_emails = set()
offset = 0
page_size = 200
while True:
    try:
        r = requests.get(
            '${BRIDGE_URL}/v1/users',
            params={'account_type': 'test', 'limit': page_size, 'offset': offset},
            headers=headers,
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"  [FAIL]  GET /v1/users failed: {e}", file=sys.stderr)
        sys.exit(1)

    if r.status_code != 200:
        print(f"  [FAIL]  GET /v1/users returned HTTP {r.status_code}: {r.text}", file=sys.stderr)
        sys.exit(1)

    page = r.json()
    if not page:
        break
    found_emails.update(u['email'] for u in page)
    if len(page) < page_size:
        break
    offset += page_size
app_expected = expected_emails
app_found    = app_expected & found_emails
missing      = app_expected - found_emails

for email in sorted(app_found):
    print(f"  [ok]    {email}")
for email in sorted(missing):
    print(f"  [FAIL]  Missing: {email}", file=sys.stderr)

print(f"\n  Users found: {len(app_found)}/{len(app_expected)}")
sys.exit(0 if not missing else 1)
PYEOF

log_success "Verification passed"

echo ""
printf "${GREEN}${BOLD}Bridge user seed complete — ${APP}${RESET}\n"
echo ""
exit 0
