# Separate application and administrative service grants

A service credential is not an operator merely because it omits X-User-ID.
`service_principals.scopes` is the authority for administrative API access:

- An application principal has exactly one `app:<app-id>` scope and the same
  single `allowed_apps` entry. App and path assertions cannot expand that grant.
- An administrative principal has exactly `bridge:admin` and no allowed apps.
  It cannot make application AI requests. Only it can create/rotate principals,
  grant subscriptions, issue arbitrary user JWTs or administer credentials.
- The existing shared `BRIDGE_SERVICE_TOKEN` on **Bridge infrastructure** identifies
  `bridge:internal`. It can call internal worker/platform seams; it is not an
  administrative principal. Never distribute this secret to applications.
- The independent sandbox broker retains only `provider-credential:read`.
  Neither application nor administrative credentials substitute for its token.

User login/reset flows still require their user proof (password, signed reset or
verification token). User JWTs may manage their own developer tokens/passwords;
service-token callers at those management routes must have the administrative
scope. A JWT role or isAdmin bit never substitutes for the admin principal.
Self-scoped profile editing cannot change passwords, roles, tenant membership,
verification or provider configuration without the administrative grant.

`GET /v1/auth/service-scope` exposes only the authenticated caller's name, app list
and scopes, never token material. Energy verifies both configured Bridge tokens
before API/worker/pipeline/frontend startup. Unavailable or older introspection,
missing credentials and privileged, unknown, wildcard or foreign grants all fail
startup. Checking an environment variable's name alone cannot establish its rights.

Migration 068 adds scopes without promoting any existing identity to admin.
Single-app identities acquire only their existing app grant. Wildcard/multi-app
identities remain without service grants and require explicit reprovisioning.
The database constraint and creation validator forbid mixed administration/app
grants. Missing scope columns in an older platform response fail loudly.

Bootstrap uses `scripts/provision-admin-principal.py` in a separate administrative
provisioning environment with DB access and a fresh Infisical-supplied secret.
It stores only a digest. There is no bootstrap HTTP route or shared-secret admin
fallback. Keep the admin credential solely in the admin Infisical workspace,
never in Energy, Report, other apps or ordinary workers. Provision administrators
separately per Bridge environment; principal union sync excludes them. Scope
metadata participates in application principal sync and drift checks.

Before rollout: apply the migration; provision the separate administrator; rotate
all app service credentials to individual single-app principals; remove the old
shared infrastructure credential from app environments and rotate that secret too;
update admin tooling to BRIDGE_ADMIN_TOKEN; deploy the Bridge scope endpoint before
starting Energy. AI_BRIDGE_API_KEY in Energy must also be an Energy-only principal.
Enable principal enforcement on both Bridges. Old global AI keys are not valid
Energy startup credentials. These are deployment prerequisites, not live changes
performed by this implementation.
