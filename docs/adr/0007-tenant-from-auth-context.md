# ADR 0007 — Tenant identity comes from the Auth context, never the body

**Status:** Accepted (2026-05-14)
**Context:** Phase 2A introduced `tenants.category` (prod|staging|local) and
the `?mode=` filter on admin endpoints. The filter only works if every
tenant-scoped row carries a `tenant_id`. Apps were passing `tenantId: null`
when they did not have it in the caller scope, leaving the column unset and
silently bypassing the filter.

## Decision

For every endpoint that inserts a row into a tenant-scoped table
(`activities`, `feedback`, `invoices`, `billing_events`, …), the Bridge
derives `tenant_id` from the **authentication context** — never from the
request body for user-flows.

### Resolution rules

1. **User-JWT flow:** `tenant_id` is read from the JWT (which already
   carries it under `tenantId`). If the JWT is malformed / legacy and
   does not carry it, the Bridge falls back to looking up
   `users.tenant_id` for the `sub` user. Body `tenantId` is **ignored**
   to prevent tenant spoofing.
2. **Service-token flow:** there is no user context. The caller passes
   either `tenantId` *or* `actorUserId` in the request body:
   - `tenantId` → used directly (cross-tenant jobs that know the tenant).
   - `actorUserId` → the Bridge derives the tenant from that user's
     `users.tenant_id`. This supports apps that authenticate to the
     Bridge with a service token but log *on behalf of a signed-in user*
     (e.g. werking-report's `logAiActivity`, which already sends
     `actorUserId`). The app does not need to plumb tenant through.
   - Neither → `400 Bad Request`.
3. **Anything else:** `400 Bad Request`. No silent NULLs.

### Defense in depth

The contract is enforced at **three layers**:

| Layer | What it stops | Where |
|---|---|---|
| **Auth** | User-flows: tenant is derived automatically. App code does not need to plumb it through. | `src/api_auth/tenant_resolver.py::resolve_tenant_id` |
| **API** | Service-flows without `tenantId` in the body get an explicit 400 with a remediation message. | same helper, in every POST that writes a tenant-scoped table |
| **Storage** | `tenant_id NOT NULL` on `activities` and `feedback`. A future endpoint that forgets to call the resolver fails its first INSERT. | `docker/migrations/008_tenant_id_not_null.sql` |

If any one of the three is bypassed by a future bug, the others still catch
it. There is no path that produces a tenant-anonymous row.

## Consequences

### What apps had to change

**Nothing**, for the common user-flow case. Apps already send the JWT they
got from `/v1/auth/login`; the JWT carries `tenantId`; the Bridge reads
it. Apps that previously sent `tenantId: null` in the body now have that
field ignored — same result wire-side, fewer footguns.

For the rare service-token-only callers (background jobs running outside a
user session): they must now pass `tenantId` explicitly. The API tells
them so with a clear 400 message.

### What we deleted

Migration 008 removes historical anonymous rows that had neither
`tenant_id`, `actor_user_id`, nor any payload hint — those can never be
retroactively classified. 6 activities and 1 feedback at deploy time, all
generated before the Phase 2A platform layer existed. Authorised by Rafael
("aktuell arbeitet noch niemand in production", 2026-05-12).

### What the admin panel sees

After the deploy, **every new** activity / feedback row carries a real
tenant. The `?mode=` filter on the Platform Admin tabs now shows truthful
counts for prod / staging / local instead of silent zeros.

### What still has to happen elsewhere

- `/v1/invoices` and `/v1/billing/*` POSTs are next on the same pattern.
  They already take `tenantId` from the body; switching them over to
  `resolve_tenant_id()` is mechanical follow-up work.
- The `migrations/008` deletion is one-shot. Once applied, the contract
  prevents the situation from recurring.

## Alternatives considered

| | Why rejected |
|---|---|
| **(A) Patch every app** to plumb `tenant_id` through every caller chain | High blast radius, error-prone, has to be repeated for every new app. The information is already in the JWT — making apps re-send it is duplicate work. |
| **(B) Bridge soft-fallback only** (resolve when `tenantId` is NULL, else accept body) | Doesn't fix the spoofing vector. Body-supplied `tenantId` would still trump the JWT, which lets a misbehaving / compromised app log activities against any tenant. |
| **(C) Bridge hard-fail on missing `tenantId`** without an auth-context fallback | Forces every app to plumb the field through anyway, even though the JWT already carries it. Same work as (A) without the security upside. |

The chosen design is the union of the best parts: JWT is authoritative
(closes spoofing), body is required only for explicit cross-tenant flows
(service tokens), and the storage layer rejects nulls (catches everything
else).

## References

- Migration: `docker/migrations/008_tenant_id_not_null.sql`
- Helper: `src/api_auth/tenant_resolver.py`
- Migration 006 (the trigger): `docker/migrations/006_tenant_category.sql`
- UI consumer: `apps/partner-platform` — Platform Admin "Mode" pill (prod/staging/local)
