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

# Reap our own children (the `python3 - <<PYEOF` blocks) if this script is
# interrupted or killed. Without this, an aborted run leaves the python child
# orphaned to systemd, where it can keep hammering the Bridge — the root of the
# /v1/users offset-flood incident (paired with the endpoint + safety-cap fixes).
trap 'pkill -P $$ 2>/dev/null || true' EXIT INT TERM

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
        # Tenant pre-exists — reconcile account_type so a tenant created
        # earlier as 'customer' does not keep its test users misclassified.
        # Only tenants declared in test-credentials.json are touched.
        try:
            pr = requests.patch(f"${BRIDGE_URL}/v1/tenants/{t['id']}",
                                json={'account_type': 'test'}, headers=headers, timeout=15)
        except requests.RequestException as e:
            print(f"  [FAIL]  Tenant {t['id']}: reconcile error — {e}", file=sys.stderr)
            ok = False
            continue
        if pr.status_code == 200:
            print(f"  [ok]    Reconciled account_type=test: {t['id']}")
        else:
            print(f"  [FAIL]  Tenant {t['id']}: reconcile PATCH HTTP {pr.status_code} — {pr.text}", file=sys.stderr)
            ok = False
    else:
        print(f"  [FAIL]  Tenant {t['id']}: HTTP {r.status_code} — {r.text}", file=sys.stderr)
        ok = False

sys.exit(0 if ok else 1)
PYEOF
log_success "Tenants seeded"

# ============================================================================
# PHASE 2b: SEED TENANT BILLING ADDRESS
#
# A tenant with an active subscription but no billing address cannot be
# invoiced (§11 UStG requires the recipient's address on every invoice).
# The Bridge `provision_subscription` gate rejects subscription provisioning
# unless billing-address is complete — so the seeder must populate it for
# every test tenant BEFORE Phase 5 (billing provision).
#
# Idempotent PATCH: re-seeds converge to the same address. Existing
# addresses with any user-modified field stay intact (PATCH only sets the
# fields it sends — every field present here overwrites silently, which is
# the desired behaviour for test tenants whose addresses are seed-owned).
# ============================================================================
phase_header "2b" "SEED TENANT BILLING ADDRESS"

python3 - <<PYEOF
import json, sys, requests

creds = json.load(open('${CREDENTIALS_JSON}'))
users = creds.get('users', {})
headers = {
    'Content-Type': 'application/json',
    'X-Bridge-Service-Token': '${BRIDGE_SERVICE_TOKEN}',
}

# Same tenant set as Phase 2 — declared + app-default.
declared_ids = set()
for u in users.values():
    tid = u.get('tenantId')
    if tid:
        declared_ids.add(tid)

tenant_ids = sorted(declared_ids)
default_tid = '${DEFAULT_TENANT_ID}'
if default_tid not in declared_ids:
    tenant_ids.insert(0, default_tid)

if not tenant_ids:
    print('  [warn]  No tenants to address')
    sys.exit(0)

# Sane test defaults — Austria-based, satisfies the Bridge billing-address
# completeness gate (name+street+city+postcode+country all required).
def default_address(tid: str) -> dict:
    return {
        'name':     f'Test Tenant {tid}',
        'street':   'Teststraße 1',
        'city':     'Wien',
        'postcode': '1010',
        'country':  'AT',
    }

ok = True
for tid in tenant_ids:
    try:
        r = requests.patch(
            f'${BRIDGE_URL}/v1/tenants/{tid}/billing-address',
            json=default_address(tid),
            headers=headers,
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"  [FAIL]  Billing address {tid}: request error — {e}", file=sys.stderr)
        ok = False
        continue

    if r.status_code == 200:
        print(f"  [ok]    Billing address set: {tid}")
    else:
        print(f"  [FAIL]  Billing address {tid}: HTTP {r.status_code} — {r.text}", file=sys.stderr)
        ok = False

sys.exit(0 if ok else 1)
PYEOF
log_success "Tenant billing addresses seeded"

# ============================================================================
# PHASE 3: SEED USERS
#
# Users without tenantId are assigned to the app-default tenant.
# All other users use their declared tenantId.
# ============================================================================
phase_header 3 "SEED USERS"

python3 - <<PYEOF
import json, sys, requests
from pathlib import Path

creds = json.load(open('${CREDENTIALS_JSON}'))
users = creds.get('users', {})
if not users:
    print("  [FAIL]  No users defined in test-credentials.json", file=sys.stderr)
    sys.exit(1)

# Load global registry (ADR-0006) for authoritative tenant lookup.
# Fall back to per-app tenantId declaration if registry is unavailable.
REGISTRY_PATH = Path('/root/projekte/werkingflow-production/tests/unified-tester/config/test-users.json')
registry_users = {}
if REGISTRY_PATH.exists():
    try:
        registry_users = json.loads(REGISTRY_PATH.read_text()).get('users', {})
        print(f"  [info]  Loaded global registry: {len(registry_users)} entries")
    except Exception as e:
        print(f"  [warn]  Could not load global registry: {e} — falling back to per-app tenantId")
else:
    print(f"  [warn]  Global registry not found at {REGISTRY_PATH} — falling back to per-app tenantId")

headers = {
    'Content-Type': 'application/json',
    'X-Bridge-Service-Token': '${BRIDGE_SERVICE_TOKEN}',
}
default_tenant_id = '${DEFAULT_TENANT_ID}'
ok = True

def _canonical_tenant(u: dict) -> str:
    """Resolve canonical tenant: registry > per-app tenantId > app default."""
    email = u.get('email', '').lower()
    reg = registry_users.get(email)
    if reg and reg.get('tenant'):
        return reg['tenant']
    return u.get('tenantId') or default_tenant_id

# Pre-fetch existing users (email -> {id, tenant_id}) so a 409 can be reconciled via PATCH.
existing = {}
offset = 0
while True:
    try:
        rr = requests.get('${BRIDGE_URL}/v1/users', params={'limit': 200, 'offset': offset},
                          headers=headers, timeout=15)
    except requests.RequestException as e:
        print(f"  [FAIL]  Pre-fetch of existing users failed: {e}", file=sys.stderr)
        sys.exit(1)
    if rr.status_code != 200:
        print(f"  [FAIL]  GET /v1/users HTTP {rr.status_code}: {rr.text}", file=sys.stderr)
        sys.exit(1)
    page = rr.json()
    if not page:
        break
    for x in page:
        existing[x['email'].lower()] = {'id': x['id'], 'tenant_id': x.get('tenant_id')}
    if len(page) < 200:
        break
    offset += 200
    # Safety cap: the user DB is small (thousands at most). If pagination runs
    # past this, the server is ignoring 'offset' (infinite-page bug) — fail loud
    # instead of looping forever and flooding the Bridge.
    if offset > 100000:
        print(f"  [FAIL]  /v1/users paginated past offset {offset} without terminating "
              f"— server likely ignoring 'offset'. Aborting to avoid infinite flood.",
              file=sys.stderr)
        sys.exit(1)

def _verify_user_exists(email):
    """GET-verify a single user by email via a fresh paginated /v1/users scan.

    Returns the user dict if the Bridge actually holds the row, else None.
    Used to disambiguate a 500 from POST /v1/users that may have created the
    user anyway (see the 500-handling branch in the create loop). Reuses the
    same pagination + offset safety-cap pattern as the pre-fetch above; the
    pre-fetched `existing` dict is NOT consulted because a 500-but-created row
    is created DURING this run, after the pre-fetch already ran.
    """
    target = email.lower()
    off = 0
    while True:
        try:
            vr = requests.get('${BRIDGE_URL}/v1/users', params={'limit': 200, 'offset': off},
                              headers=headers, timeout=15)
        except requests.RequestException as e:
            print(f"  [FAIL]  500-verify GET /v1/users failed: {e}", file=sys.stderr)
            return None
        if vr.status_code != 200:
            print(f"  [FAIL]  500-verify GET /v1/users HTTP {vr.status_code}", file=sys.stderr)
            return None
        pg = vr.json()
        if not pg:
            return None
        for x in pg:
            if x['email'].lower() == target:
                return x
        if len(pg) < 200:
            return None
        off += 200
        if off > 100000:
            print(f"  [FAIL]  500-verify paginated past offset {off} — aborting", file=sys.stderr)
            return None

def _ensure_email_verified(uid, key):
    """Flip users.email_verified via the operator-only PATCH field.

    Login hard-blocks unverified users (Bridge identity/routes.py), and seeded
    test users have no reachable inbox to click a verification link — without
    this flip every fresh seed produces login-blocked users (observed 2026-07-03
    with the konto-portal users; same durability class as app-license grants).
    Safe by construction: this seeder only processes test-credentials.json
    identities (account_type=test tenants), never real customers.
    """
    try:
        vr = requests.patch(f"${BRIDGE_URL}/v1/users/{uid}",
                            json={'email_verified': True}, headers=headers, timeout=15)
    except requests.RequestException as e:
        print(f"  [FAIL]  User {key}: email_verified PATCH error — {e}", file=sys.stderr)
        return False
    if vr.status_code != 200:
        print(f"  [FAIL]  User {key}: email_verified PATCH HTTP {vr.status_code} — {vr.text}",
              file=sys.stderr)
        return False
    return True

created = reconciled = 0
for key, u in users.items():
    tenant_id = _canonical_tenant(u)
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
        created += 1
        print(f"  [ok]    Created: {key} ({u['email']}) → tenant={tenant_id}")
        new_uid = (r.json() or {}).get('id')
        if not new_uid or not _ensure_email_verified(new_uid, key):
            ok = False
    elif r.status_code == 409:
        # User pre-exists. Reconcile password + role + tenant_id so every
        # re-seed converges to the registry's canonical identity (ADR-0006).
        # Before ADR-0006 the 409-PATCH only updated password+role, leaving
        # tenant frozen at the first-seeded value — which caused the 3
        # tenant_mismatch failures in werking-energy Layer-0.
        existing_entry = existing.get(u['email'].lower())
        if not existing_entry:
            print(f"  [FAIL]  User {key}: 409 but not in user list — cannot reconcile", file=sys.stderr)
            ok = False
            continue
        uid = existing_entry['id']
        current_tenant = existing_entry.get('tenant_id')
        patch_body = {'password': u.get('password'), 'role': u.get('role', 'user'),
                      'email_verified': True}
        # Add tenant_id to PATCH only when it differs from the current Bridge value.
        # Avoids spurious DB writes on clean re-seeds, while still fixing stale tenants.
        if current_tenant != tenant_id:
            patch_body['tenant_id'] = tenant_id
        try:
            pr = requests.patch(
                f"${BRIDGE_URL}/v1/users/{uid}",
                json=patch_body,
                headers=headers, timeout=15,
            )
        except requests.RequestException as e:
            print(f"  [FAIL]  User {key}: reconcile request error — {e}", file=sys.stderr)
            ok = False
            continue
        if pr.status_code == 200:
            reconciled += 1
            tenant_note = f" (tenant: {current_tenant!r} → {tenant_id!r})" if 'tenant_id' in patch_body else ""
            print(f"  [ok]    Reconciled password/role/tenant: {key} ({u['email']}){tenant_note}")
        else:
            print(f"  [FAIL]  User {key}: reconcile PATCH HTTP {pr.status_code} — {pr.text}", file=sys.stderr)
            ok = False
    elif r.status_code == 500:
        # Known Bridge create-hook bug: POST /v1/users can return 500 AFTER the
        # users row is already committed (a post-insert step fails post-commit,
        # see src/db/admin_routes.py create_user — any non-unique PostgresError
        # surfaces as a generic 500). The 500 is therefore AMBIGUOUS: the user
        # may well exist. Verify the REAL state with a fresh GET before treating
        # it as fatal (validation-before-fail — NOT a silent fallback): only a
        # genuinely-absent user is a real error. This unblocks repeatable runs
        # of the DSGVO delete user (e.g. gdpr-delete@energy-test.com), which is
        # re-created after every deletion test and otherwise fails the whole
        # seed at this 500 even though login + delete prove the user exists.
        #
        # ROOT FIX is Bridge-lane (POST /v1/users must not 500 when it actually
        # created the user — the post-insert path in create_user) and has been
        # routed to Rafael. This seeder-side GET-verify is the defensive guard.
        verified = _verify_user_exists(u['email'])
        if verified:
            created += 1
            print(f"  [warn]  Bridge 500-but-created, known Bridge create-hook bug "
                  f"— user verified present, continuing: {key} ({u['email']}) → tenant={tenant_id}")
            v_uid = verified.get('id') if isinstance(verified, dict) else None
            if not v_uid or not _ensure_email_verified(v_uid, key):
                ok = False
        else:
            print(f"  [FAIL]  User {key}: HTTP 500 and user NOT present on GET-verify "
                  f"— real failure — {r.text}", file=sys.stderr)
            ok = False
    else:
        print(f"  [FAIL]  User {key}: HTTP {r.status_code} — {r.text}", file=sys.stderr)
        ok = False

print(f"\n  {created} created, {reconciled} reconciled")
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
    # Safety cap (see pre-fetch loop): fail loud if the server ignores 'offset'
    # instead of looping forever and flooding the Bridge.
    if offset > 100000:
        print(f"  [FAIL]  /v1/users paginated past offset {offset} without terminating "
              f"— server likely ignoring 'offset'. Aborting to avoid infinite flood.",
              file=sys.stderr)
        sys.exit(1)
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

# ============================================================================
# PHASE 5: BILLING PROVISION
#
# Per test user:
#   1. POST /v1/billing/subscription/provision — active subscription (no Mollie)
#   2. POST /v1/budget/topup/credit           — generous test credit
#
# Subscription provision is idempotent (existing active sub returned as-is).
# Top-up is additive, not idempotent — calling the seeder multiple times adds
# more credits, which is harmless in test environments.
# ============================================================================
phase_header 5 "BILLING PROVISION"

python3 - <<PYEOF
import json, sys, requests

creds = json.load(open('${CREDENTIALS_JSON}'))
users = creds.get('users', {})

headers = {
    'Content-Type': 'application/json',
    'X-Bridge-Service-Token': '${BRIDGE_SERVICE_TOKEN}',
}

# One paid (non-trial) plan per app — must match src/budget/plans.py
APP_PLANS = {
    'werking-report':  'report-standard',
    'werking-energy':  'energy-project',
    'werking-safety':  'safety-project',
    'werking-noise':   'noise-tbd',
    'engelmann':       'engelmann-custom',
}
plan_id = APP_PLANS.get('${APP}')
if not plan_id:
    print(f"  [warn]  No plan configured for app '${APP}' — skipping billing provision")
    sys.exit(0)

# Is this app's plan slot-based (interval='project', e.g. energy-project)? Such plans
# gate real job-creation on manual_project_credits SLOTS that only exist after a
# RELEASED order (no Mollie in test). A provisioned subscription does NOT grant slots,
# so without seeding them every project-plan test user is correctly NO_CREDITS and all
# credit-gated scenarios are untestable. Read the LIVE plan (DB truth) — no hardcoding.
# Fail-loud: a plans-fetch error must not silently skip the slot seeding below.
plan_interval = None
try:
    _pr = requests.get('${BRIDGE_URL}/v1/billing/plans', headers=headers, timeout=15)
    if _pr.status_code != 200:
        print(f"  [FAIL]  GET /v1/billing/plans HTTP {_pr.status_code}", file=sys.stderr)
        sys.exit(1)
    _raw = _pr.json()
    _items = _raw.get('plans') if isinstance(_raw, dict) and 'plans' in _raw else (
        list(_raw.values()) if isinstance(_raw, dict) else _raw)
    for _p in (_items or []):
        if isinstance(_p, dict) and _p.get('id') == plan_id:
            plan_interval = _p.get('interval')
            break
except requests.RequestException as e:
    print(f"  [FAIL]  GET /v1/billing/plans failed: {e}", file=sys.stderr)
    sys.exit(1)

# Pre-fetch user IDs (email -> id)
user_ids = {}
offset = 0
while True:
    try:
        r = requests.get('${BRIDGE_URL}/v1/users',
                         params={'limit': 200, 'offset': offset},
                         headers=headers, timeout=15)
    except requests.RequestException as e:
        print(f"  [FAIL]  GET /v1/users failed: {e}", file=sys.stderr)
        sys.exit(1)
    if r.status_code != 200:
        print(f"  [FAIL]  GET /v1/users HTTP {r.status_code}", file=sys.stderr)
        sys.exit(1)
    page = r.json()
    if not page:
        break
    for x in page:
        user_ids[x['email'].lower()] = x['id']
    if len(page) < 200:
        break
    offset += 200
    # Safety cap: the user DB is small (thousands at most). If pagination runs
    # past this, the server is ignoring 'offset' (infinite-page bug) — fail loud
    # instead of looping forever and flooding the Bridge.
    if offset > 100000:
        print(f"  [FAIL]  /v1/users paginated past offset {offset} without terminating "
              f"— server likely ignoring 'offset'. Aborting to avoid infinite flood.",
              file=sys.stderr)
        sys.exit(1)

TEST_TOPUP_EUR = 500.0  # generous test credit
TEST_PROJECT_SLOTS = 50  # generous project-credit slots for slot-based (interval='project') plans

ok = True
for key, u in users.items():
    email = u['email'].lower()

    # Billing-free test users: skip ALL billing provisioning (subscription,
    # top-up credit, project-credit slots). Declared via "billingFree": true
    # in the per-app test-credentials.json.
    #
    # Why this exists: any billing record blocks the Bridge hard-delete
    #   DELETE /v1/users/{id} → 409 ForeignKeyViolation, because
    #   subscriptions/credit_purchases are ON DELETE RESTRICT and
    #   manual_project_credits.user_id has no ON DELETE clause (PostgreSQL
    #   default NO ACTION). closeAccount() calls exactly that endpoint, so a
    #   provisioned user can never exercise the DSGVO Art.17 account-deletion
    #   path. The dedicated delete/GDPR user must stay billing-free so its
    #   delete scenarios (account-data-backend, account-loeschen-ui, flow-dsgvo)
    #   can run end-to-end. App-license grants (Phase 6) are unaffected — those
    #   are ON DELETE CASCADE and never block deletion.
    if u.get('billingFree'):
        print(f"  [info]  Billing-free user — skipping subscription/credits: {key} ({u['email']})")
        continue

    uid = user_ids.get(email)
    if not uid:
        print(f"  [FAIL]  User {key} ({u['email']}): not found in Bridge — skipped", file=sys.stderr)
        ok = False
        continue

    # 1. Provision active subscription
    try:
        r = requests.post(
            '${BRIDGE_URL}/v1/billing/subscription/provision',
            json={'userId': uid, 'planId': plan_id, 'seats': 1},
            headers=headers,
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"  [FAIL]  User {key}: provision request error — {e}", file=sys.stderr)
        ok = False
        continue

    if r.status_code in (200, 201):
        sub = r.json()
        status = sub.get('status', '?')
        print(f"  [ok]    Subscription {status} ({plan_id}): {key} ({u['email']})")
    else:
        print(f"  [FAIL]  User {key}: provision HTTP {r.status_code} — {r.text}", file=sys.stderr)
        ok = False
        continue

    # 2. Top-up credit (additive — multiple seeds accumulate, harmless for tests)
    try:
        r = requests.post(
            '${BRIDGE_URL}/v1/budget/topup/credit',
            json={'userId': uid, 'amountEur': TEST_TOPUP_EUR},
            headers=headers,
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"  [FAIL]  User {key}: topup request error — {e}", file=sys.stderr)
        ok = False
        continue

    if r.status_code == 200:
        balance = r.json().get('newBalance', '?')
        print(f"  [ok]    Top-up +EUR {TEST_TOPUP_EUR} → balance EUR {balance}: {key}")
    else:
        print(f"  [FAIL]  User {key}: topup HTTP {r.status_code} — {r.text}", file=sys.stderr)
        ok = False

    # 3. Project-credit SLOTS — ONLY for slot-based (interval='project') plans.
    #    Mirrors a real purchase: create a pending order, then release it (operator),
    #    which is the one path that INSERTs manual_project_credits — the slots the
    #    job-creation gate (canCreateProject) actually checks. Idempotent: skip when
    #    the user already has available slots so repeated seeds don't pile up.
    if plan_interval == 'project':
        try:
            cr = requests.get(
                f'${BRIDGE_URL}/v1/users/{uid}/project-credits',
                headers=headers, timeout=15,
            )
        except requests.RequestException as e:
            print(f"  [FAIL]  User {key}: project-credit check error — {e}", file=sys.stderr)
            ok = False
            continue
        has_slots = cr.status_code == 200 and any(
            c.get('available', 0) > 0
            for c in cr.json().get('credits', [])
            if c.get('planId') == plan_id
        )
        if has_slots:
            print(f"  [ok]    Project-credits already present ({plan_id}): {key}")
            continue
        # create pending order (X-User-ID = the user we order for)
        try:
            co = requests.post(
                '${BRIDGE_URL}/v1/billing/order/invoice',
                json={'planId': plan_id, 'quantity': TEST_PROJECT_SLOTS},
                headers={**headers, 'X-User-ID': uid},
                timeout=20,
            )
        except requests.RequestException as e:
            print(f"  [FAIL]  User {key}: order create error — {e}", file=sys.stderr)
            ok = False
            continue
        if co.status_code not in (200, 201):
            print(f"  [FAIL]  User {key}: order create HTTP {co.status_code} — {co.text}", file=sys.stderr)
            ok = False
            continue
        order_id = co.json().get('id') or co.json().get('orderId')
        if not order_id:
            print(f"  [FAIL]  User {key}: order create returned no id — {co.text}", file=sys.stderr)
            ok = False
            continue
        # release it → INSERT manual_project_credits slots
        try:
            rel = requests.post(
                f'${BRIDGE_URL}/v1/admin/orders/{order_id}/release',
                json={'note': 'test-seed project credits (seed-bridge-users.sh)'},
                headers=headers, timeout=20,
            )
        except requests.RequestException as e:
            print(f"  [FAIL]  User {key}: order release error — {e}", file=sys.stderr)
            ok = False
            continue
        if rel.status_code in (200, 201):
            print(f"  [ok]    Project-credits +{TEST_PROJECT_SLOTS} slots ({plan_id}): {key}")
        else:
            print(f"  [FAIL]  User {key}: order release HTTP {rel.status_code} — {rel.text}", file=sys.stderr)
            ok = False

sys.exit(0 if ok else 1)
PYEOF
log_success "Billing provisioned"

# ============================================================================
# PHASE 6: APP LICENSE GRANT
#
# The login JWT's `appLicenses` claim is read straight from the `app_licenses`
# table (identity/routes.py). Provisioning a subscription (Phase 5) does NOT
# create an app_license, so without this phase every seeded user is missing the
# license that route guards require → pool-wide 403 caps on L1/L2 tests, and
# any manual backfill is silently lost on the next re-seed. This phase makes
# the grant durable and idempotent.
#
# POST /v1/users/{id}/app-licenses {appId, planId:'trial', startDate:today, endDate:null, seats:1}
#   - payload is camelCase per the DEPLOYED Bridge contract (appId/startDate required)
#   - admin scope (X-Bridge-Service-Token, no X-User-ID)
#   - idempotent on (userId, appId): a re-seed updates and returns created:false
# ============================================================================
phase_header 6 "APP LICENSE GRANT"

python3 - <<PYEOF
import json, sys, requests

creds = json.load(open('${CREDENTIALS_JSON}'))
users = creds.get('users', {})

headers = {
    'Content-Type': 'application/json',
    'X-Bridge-Service-Token': '${BRIDGE_SERVICE_TOKEN}',
}

app_id = '${APP}'
from datetime import datetime, timezone
today = datetime.now(timezone.utc).date().isoformat()

# Pre-fetch user IDs (email -> id) — same pattern as Phase 5.
user_ids = {}
offset = 0
while True:
    try:
        r = requests.get('${BRIDGE_URL}/v1/users',
                         params={'limit': 200, 'offset': offset},
                         headers=headers, timeout=15)
    except requests.RequestException as e:
        print(f"  [FAIL]  GET /v1/users failed: {e}", file=sys.stderr)
        sys.exit(1)
    if r.status_code != 200:
        print(f"  [FAIL]  GET /v1/users HTTP {r.status_code}", file=sys.stderr)
        sys.exit(1)
    page = r.json()
    if not page:
        break
    for x in page:
        user_ids[x['email'].lower()] = x['id']
    if len(page) < 200:
        break
    offset += 200
    # Safety cap: the user DB is small (thousands at most). If pagination runs
    # past this, the server is ignoring 'offset' (infinite-page bug) — fail loud
    # instead of looping forever and flooding the Bridge.
    if offset > 100000:
        print(f"  [FAIL]  /v1/users paginated past offset {offset} without terminating "
              f"— server likely ignoring 'offset'. Aborting to avoid infinite flood.",
              file=sys.stderr)
        sys.exit(1)

ok = True
for key, u in users.items():
    email = u['email'].lower()
    uid = user_ids.get(email)
    if not uid:
        print(f"  [FAIL]  User {key} ({u['email']}): not found in Bridge — skipped", file=sys.stderr)
        ok = False
        continue
    try:
        r = requests.post(
            f"${BRIDGE_URL}/v1/users/{uid}/app-licenses",
            json={'appId': app_id, 'planId': 'trial', 'startDate': today, 'endDate': None, 'seats': 1},
            headers=headers,
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"  [FAIL]  User {key}: app-license request error — {e}", file=sys.stderr)
        ok = False
        continue
    if r.status_code in (200, 201):
        created = r.json().get('created')
        verb = 'granted' if created else 'already present (refreshed)'
        print(f"  [ok]    App-license {verb} ({app_id}/trial): {key} ({u['email']})")
    else:
        print(f"  [FAIL]  User {key}: app-license HTTP {r.status_code} — {r.text}", file=sys.stderr)
        ok = False

sys.exit(0 if ok else 1)
PYEOF
log_success "App licenses granted"

# ============================================================================
# PHASE 7: UNIVERSAL INTERACTIVE USER
#
# ONE shared human-login user (interactive@werkingflow.com, role owner) that
# works in ALL WerkING apps (energy/report/safety/noise/engelmann). It lives
# in its own tenant ("interactive-user", account_type=customer) so test-tenant
# sweeps never affect it, and it has active app_licenses for EVERY app so
# Rafael can stay logged in across all apps simultaneously without triggering
# the single-active-session rule (each app reads its own session-generation
# store; this user is never touched by the test runner).
#
# WHY app_licenses for ALL apps on every seed run:
#   The user must be fully licensed after any single seed invocation, not only
#   after seeding all five apps. Granting is idempotent (created:false = refreshed).
#
# User details are stored in apps/<app>/config/interactive-credentials.json
# for the CUI Login-Add-In picker (tester:false → highlighted green). The
# picker file is the SSoT for the credentials; this phase keeps Bridge in sync.
#
# Billing (Steps D–G, added 2026-06-15):
#   D. Billing address  — PATCH idempotent; required before subscription provision.
#   E. Subscription     — provision_subscription idempotent per plan; plans that
#                         don't exist in the DB are skipped (fail-loud on fetch).
#   F. Top-up credit    — additive (same as Phase 5); harmless on re-seed.
#   G. Project slots    — idempotent guard (skip when slots already present).
# ============================================================================
phase_header 7 "UNIVERSAL INTERACTIVE USER"

python3 - <<PYEOF
import json, sys, requests
from datetime import datetime, timezone

headers = {
    'Content-Type': 'application/json',
    'X-Bridge-Service-Token': '${BRIDGE_SERVICE_TOKEN}',
}

INTERACTIVE_EMAIL    = 'interactive@werkingflow.com'
INTERACTIVE_PASSWORD = 'InterAktiv2026!'
INTERACTIVE_NAME     = 'Rafael (Interactive — alle Apps)'
INTERACTIVE_ROLE     = 'owner'
INTERACTIVE_TENANT   = 'interactive-user'
ALL_APP_IDS          = ['werking-energy', 'werking-report', 'werking-safety', 'werking-noise', 'engelmann']

today = datetime.now(timezone.utc).date().isoformat()
ok = True

# Step A: ensure tenant (idempotent)
try:
    r = requests.post('${BRIDGE_URL}/v1/tenants', json={
        'id': INTERACTIVE_TENANT,
        'name': 'Interactive User (Rafael)',
        'account_type': 'customer',
    }, headers=headers, timeout=15)
except requests.RequestException as e:
    print(f"  [FAIL]  Tenant create error: {e}", file=sys.stderr)
    sys.exit(1)

if r.status_code == 201:
    print(f"  [ok]    Tenant created: {INTERACTIVE_TENANT}")
elif r.status_code == 409:
    print(f"  [ok]    Tenant exists: {INTERACTIVE_TENANT}")
else:
    print(f"  [FAIL]  Tenant HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
    sys.exit(1)

# Step B: create user (idempotent — 409 = already exists)
try:
    r = requests.post('${BRIDGE_URL}/v1/users', json={
        'email': INTERACTIVE_EMAIL,
        'name':  INTERACTIVE_NAME,
        'tenant_id': INTERACTIVE_TENANT,
        'password': INTERACTIVE_PASSWORD,
        'role': INTERACTIVE_ROLE,
    }, headers=headers, timeout=15)
except requests.RequestException as e:
    print(f"  [FAIL]  User create error: {e}", file=sys.stderr)
    sys.exit(1)

uid = None
if r.status_code in (200, 201):
    data = r.json()
    uid = (data.get('id') or (data.get('user') or {}).get('id'))
    print(f"  [ok]    Interactive user created: id={uid}")
elif r.status_code == 409:
    print(f"  [ok]    Interactive user already exists — resolving id")
else:
    print(f"  [FAIL]  User create HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
    sys.exit(1)

# Resolve uid if not returned by create
if not uid:
    offset = 0
    while True:
        try:
            rr = requests.get('${BRIDGE_URL}/v1/users',
                              params={'limit': 200, 'offset': offset},
                              headers=headers, timeout=15)
        except requests.RequestException as e:
            print(f"  [FAIL]  GET /v1/users error: {e}", file=sys.stderr)
            sys.exit(1)
        if rr.status_code != 200:
            print(f"  [FAIL]  GET /v1/users HTTP {rr.status_code}", file=sys.stderr)
            sys.exit(1)
        page = rr.json()
        if not page:
            break
        for u in page:
            if u['email'].lower() == INTERACTIVE_EMAIL.lower():
                uid = u['id']
                break
        if uid or len(page) < 200:
            break
        offset += 200
        if offset > 100000:
            print(f"  [FAIL]  /v1/users paginated past offset {offset} — aborting", file=sys.stderr)
            sys.exit(1)
    if not uid:
        print(f"  [FAIL]  Interactive user not found after create", file=sys.stderr)
        sys.exit(1)
    print(f"  [ok]    Interactive user id resolved: {uid}")

# Step C: grant app_licenses for ALL apps (idempotent)
for app_id in ALL_APP_IDS:
    try:
        r = requests.post(
            f'${BRIDGE_URL}/v1/users/{uid}/app-licenses',
            json={'appId': app_id, 'planId': 'trial', 'startDate': today, 'endDate': None, 'seats': 1},
            headers=headers,
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"  [FAIL]  app-license {app_id}: {e}", file=sys.stderr)
        ok = False
        continue
    if r.status_code in (200, 201):
        created = r.json().get('created')
        verb = 'granted' if created else 'already present (refreshed)'
        print(f"  [ok]    app-license {verb}: {app_id}")
    else:
        print(f"  [FAIL]  app-license {app_id}: HTTP {r.status_code} — {r.text[:200]}", file=sys.stderr)
        ok = False

# Step D: billing address for interactive-user tenant (idempotent PATCH)
# Required before subscription provisioning — provision_subscription rejects
# tenants that have no complete billing address.
try:
    r = requests.patch(
        f'${BRIDGE_URL}/v1/tenants/{INTERACTIVE_TENANT}/billing-address',
        json={
            'name':     'Interactive User (Rafael)',
            'street':   'Teststraße 1',
            'city':     'Wien',
            'postcode': '1010',
            'country':  'AT',
        },
        headers=headers,
        timeout=15,
    )
except requests.RequestException as e:
    print(f"  [FAIL]  Billing address PATCH error: {e}", file=sys.stderr)
    sys.exit(1)
if r.status_code == 200:
    print(f"  [ok]    Billing address set: {INTERACTIVE_TENANT}")
else:
    print(f"  [FAIL]  Billing address HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
    sys.exit(1)

# Step E: provision active subscriptions (one per billable plan that exists in DB).
# Read live plan catalog — no hardcoding. Plans that don't exist in DB are skipped
# (not an error: safety has no plan yet). provision_subscription is idempotent.
#
# INTERACTIVE_APP_PLANS mirrors Phase 5's APP_PLANS restricted to the interactive
# user's apps. Update when new plans are added to the catalog.
INTERACTIVE_APP_PLANS = {
    'werking-report': 'report-standard',
    'werking-energy': 'energy-project',
    'werking-noise':  'noise-tbd',
    'engelmann':      'engelmann-custom',
    # werking-safety: plan 'safety-project' not yet in DB → omitted; add when available
}
try:
    pr = requests.get('${BRIDGE_URL}/v1/billing/plans', headers=headers, timeout=15)
    if pr.status_code != 200:
        print(f"  [FAIL]  GET /v1/billing/plans HTTP {pr.status_code}", file=sys.stderr)
        sys.exit(1)
    raw = pr.json()
    items = raw.get('plans') if isinstance(raw, dict) and 'plans' in raw else (
        list(raw.values()) if isinstance(raw, dict) else raw)
    live_plan_intervals = {p['id']: p.get('interval') for p in (items or []) if isinstance(p, dict)}
except requests.RequestException as e:
    print(f"  [FAIL]  GET /v1/billing/plans failed: {e}", file=sys.stderr)
    sys.exit(1)

TEST_TOPUP_EUR    = 500.0  # generous test credit
TEST_PROJECT_SLOTS = 50   # generous project-credit slots for slot-based plans

for app_id, plan_id in INTERACTIVE_APP_PLANS.items():
    if plan_id not in live_plan_intervals:
        print(f"  [warn]  Plan '{plan_id}' not in DB — skipping {app_id}")
        continue
    try:
        r = requests.post(
            '${BRIDGE_URL}/v1/billing/subscription/provision',
            json={'userId': uid, 'planId': plan_id, 'seats': 1},
            headers=headers,
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"  [FAIL]  provision {plan_id}: {e}", file=sys.stderr)
        ok = False
        continue
    if r.status_code in (200, 201):
        status = r.json().get('status', '?')
        print(f"  [ok]    Subscription {status} ({plan_id}): {app_id}")
    else:
        print(f"  [FAIL]  provision {plan_id}: HTTP {r.status_code} — {r.text[:200]}", file=sys.stderr)
        ok = False

# Step F: top-up credit — additive (same as Phase 5; harmless on re-seed)
try:
    r = requests.post(
        '${BRIDGE_URL}/v1/budget/topup/credit',
        json={'userId': uid, 'amountEur': TEST_TOPUP_EUR},
        headers=headers,
        timeout=15,
    )
except requests.RequestException as e:
    print(f"  [FAIL]  Top-up request error: {e}", file=sys.stderr)
    ok = False
    r = None
if r is not None:
    if r.status_code == 200:
        balance = r.json().get('newBalance', '?')
        print(f"  [ok]    Top-up +EUR {TEST_TOPUP_EUR} → balance EUR {balance}")
    else:
        print(f"  [FAIL]  Top-up HTTP {r.status_code} — {r.text[:200]}", file=sys.stderr)
        ok = False

# Step G: project-credit SLOTS — only for slot-based (interval='project') plans.
# Mirrors Phase 5 pattern: check existing → create order → release.
# Idempotent guard: skip when any available slot already exists.
for app_id, plan_id in INTERACTIVE_APP_PLANS.items():
    if live_plan_intervals.get(plan_id) != 'project':
        continue
    try:
        cr = requests.get(
            f'${BRIDGE_URL}/v1/users/{uid}/project-credits',
            headers=headers, timeout=15,
        )
    except requests.RequestException as e:
        print(f"  [FAIL]  project-credits check ({plan_id}): {e}", file=sys.stderr)
        ok = False
        continue
    has_slots = cr.status_code == 200 and any(
        c.get('available', 0) > 0
        for c in cr.json().get('credits', [])
        if c.get('planId') == plan_id
    )
    if has_slots:
        print(f"  [ok]    Project-credits already present ({plan_id}): {app_id}")
        continue
    try:
        co = requests.post(
            '${BRIDGE_URL}/v1/billing/order/invoice',
            json={'planId': plan_id, 'quantity': TEST_PROJECT_SLOTS},
            headers={**headers, 'X-User-ID': uid},
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  [FAIL]  order create ({plan_id}): {e}", file=sys.stderr)
        ok = False
        continue
    if co.status_code not in (200, 201):
        print(f"  [FAIL]  order create ({plan_id}): HTTP {co.status_code} — {co.text[:200]}", file=sys.stderr)
        ok = False
        continue
    order_id = co.json().get('id') or co.json().get('orderId')
    if not order_id:
        print(f"  [FAIL]  order create ({plan_id}): no id in response — {co.text[:200]}", file=sys.stderr)
        ok = False
        continue
    try:
        rel = requests.post(
            f'${BRIDGE_URL}/v1/admin/orders/{order_id}/release',
            json={'note': 'test-seed project credits (interactive user, seed-bridge-users.sh)'},
            headers=headers, timeout=20,
        )
    except requests.RequestException as e:
        print(f"  [FAIL]  order release ({plan_id}): {e}", file=sys.stderr)
        ok = False
        continue
    if rel.status_code in (200, 201):
        print(f"  [ok]    Project-credits +{TEST_PROJECT_SLOTS} slots ({plan_id}): {app_id}")
    else:
        print(f"  [FAIL]  order release ({plan_id}): HTTP {rel.status_code} — {rel.text[:200]}", file=sys.stderr)
        ok = False

sys.exit(0 if ok else 1)
PYEOF
log_success "Universal interactive user provisioned (interactive@werkingflow.com)"

echo ""
printf "${GREEN}${BOLD}Bridge user seed complete — ${APP}${RESET}\n"
echo ""
exit 0
