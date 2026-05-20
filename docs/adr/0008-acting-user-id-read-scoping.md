# ADR 0008 — Acting-user identity scopes read access for the customer self-service proxy

**Status:** Accepted (2026-05-18)
**Extends:** ADR 0007 (tenant identity from auth context)

## Context

The customer self-service portal (Kunden-Kontobereich) communicates with the Bridge
through a host-app server-side proxy. The proxy authenticates with a service token
(shared secret, `X-Bridge-Service-Token`) and adds an `X-User-ID` header identifying
the currently logged-in customer.

Before this ADR, the Bridge treated every service-token request as implicitly admin
(`is_admin=True`). That was correct for machine-to-machine operator calls (budget
deductions, Mollie webhook, CUI Platform Admin) but not for the proxy pattern: a
proxy call for customer Alice with `X-User-ID: alice` would still return data for
any user, because the `is_admin` guard bypassed every self-scoping check.

## Decision

### 1. Acting-user identity in `AuthClaims`

`AuthClaims` gains `acting_user_id: Optional[str]`:

| Caller | `acting_user_id` | Meaning |
|--------|-----------------|---------|
| User JWT | `= user_id` (JWT `sub`) | User acts for themselves |
| Service token + `X-User-ID` | `= X-User-ID` header value | Proxy acting for customer |
| Service token without `X-User-ID` | `None` | Operator / background job |

### 2. `X-User-ID` is only honoured for service-token callers

With a user JWT, the JWT `sub` is the acting identity and `X-User-ID` is **silently
ignored**. Honouring it would be an identity-spoofing vector — the same principle
as ADR 0007 ("body `tenantId` is ignored for JWT callers").

### 3. `is_operator` property

`AuthClaims.is_operator` is True when the credential grants unrestricted cross-user
access:

- Service token with `acting_user_id is None` → operator.
- User JWT with `is_admin=True` → operator (future-proofing; `isAdmin` is not yet
  issued by `sign_jwt`, see known bug in `identity/jwt_utils.py`).

### 4. `require_self_or_admin` updated

The dependency now checks `acting_user_id` **before** `is_admin`:

```python
if claims.is_service and claims.acting_user_id is not None:
    # Hard scope: proxy may only access the proxied user's data.
    if claims.acting_user_id != path_user_id:
        raise HTTPException(403, ...)
    return claims
if claims.is_admin:       # operator or admin JWT
    return claims
if claims.user_id == path_user_id:  # user JWT self-access
    return claims
raise HTTPException(403, ...)
```

Without this early check a service token (is_admin=True) would always pass the
`is_admin` guard, rendering the proxy pattern insecure.

### 5. Inline scoping in other self-service endpoints

Endpoints that previously checked `if not claims.is_admin: scope to claims.user_id`
are updated to check `if not claims.is_operator: scope to claims.effective_user_id`.

Affected: `GET /v1/invoices`, `GET /v1/invoices/{id}`, `/{id}/html`, `/{id}/pdf`,
`POST /{id}/send`, `GET /v1/activity/query`, `GET /v1/app-licenses`.

### 6. Tenant-scoped endpoints (`/v1/tenants/{id}/billing-address`)

Tenant endpoints cannot use `effective_user_id` directly (they scope by tenant, not
user). A new `_check_tenant_access` helper resolves the acting user's `tenant_id`
from the DB when `acting_user_id is not None`, then compares it to the path
`tenant_id`. Operators bypass this check.

## Consequences

### What changed

- `AuthClaims` gets `acting_user_id` (backward-compatible: default `None`).
- `require_self_or_admin` correctly blocks a proxy token from accessing a different
  user's data, even though the token's `is_admin` is True.
- Operator service tokens (no `X-User-ID`) are **completely unaffected** — they
  pass every check exactly as before. The CUI Platform Admin Panel continues to work.
- `require_service_token` (used by `POST /v1/budget/topup/credit`, machine-only
  endpoints) does **not** accept `X-User-ID`. It always returns `acting_user_id=None`.

### What is not changed

- `resolve_tenant_id` and write-tenant resolution (ADR 0007) — untouched.
- The `PATCH /v1/users/{user_id}` self-update endpoint is already gated by
  `require_self_or_admin` and picks up the fix automatically.

### Known open items

- `sign_jwt` in `identity/jwt_utils.py` never emits `isAdmin`. Admin JWT operator
  access (`is_operator=True` for user JWTs) is therefore currently dead code. This
  is a separate bug, tracked in the spec (`kundenbereich-spec-abgeglichen.md §6`).

## References

- `src/api_auth/deps.py` — `AuthClaims`, `require_self_or_admin`, `require_jwt_or_service`
- `src/api_auth/tenant_resolver.py` — `get_tenant_of_user` (newly exported)
- `src/db/admin_routes.py` — `_check_tenant_access`
- ADR 0007: `docs/adr/0007-tenant-from-auth-context.md`
- Spec: `/root/orchestrator/workspaces/devops/kundenbereich-spec-abgeglichen.md §11`
