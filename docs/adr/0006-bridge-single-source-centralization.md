# ADR-0006: Bridge Single-Source Centralization (kill dev/prod config drift)

**Status:** ACCEPTED — items A + B + C **live & verified on BOTH bridges** (single shared
OpenResty+Lua nginx.conf); prod cutover DONE (see 2026-07-02 progress entry).
**Date:** 2026-07-01 (cutover verified 2026-07-02)
**Author:** werking-energy session (root-cause of the Phase-4 "ZERO Anthropic accounts" prod incident)
**Affects:** AI-Bridge deployments (primary/dev host `49.12.72.66`, production host `178.104.178.79`) and every app whose pipeline reads a Bridge metrics endpoint (werking-energy, -safety, -report, engelmann, platform).

---

## Context — the incident that exposed it

Every werking-energy report died in **Phase 4** (Formula Validation) with
`Pre-flight: Bridge pool-state reports ZERO Anthropic accounts. Bridge configuration broken.`
— deterministically, on **all** runs, on any dataset size.

Root cause chain:
1. The phase-4 preflight (`pipeline/shared/preflight.py`, since `ebb58c00c`) reads
   `GET /v1/metrics/account-pool-state` and expects the **aggregate** shape
   `{"accounts": {name: {...}}}`.
2. The **primary** host (`49.12.72.66`) returns that aggregate (nginx routes the path to
   the `metrics-reader` aggregator). The **production** host (`178.104.178.79`) returns a
   **flat single-worker** object `{"worker","account","available",...}` with NO `accounts`
   key — because its nginx config does **not** route that path to `metrics-reader`; it falls
   through to a worker.
3. → `data.get("accounts", {})` is empty on prod → false "ZERO accounts" → Phase 4 exit=1.

The workers/aggregator are the **same image** (`Dockerfile.worker`, `metrics-reader`). The
divergence is **entirely** in the nginx layer, which is **not** built from one source.

## Current state (what actually exists)

- **Half-centralized already:** `scripts/generate-bridge-compose.sh` takes `BRIDGE_ID=primary|production`
  and generates `docker-compose.generated.yml` + `upstreams.generated.conf` from `secrets/workers/*.txt`.
  Worker services, upstreams and hardware sizing are single-source. ✅ Good foundation.
- **Routes are NOT generated:** nginx `location` blocks live in two hand-maintained files:
  - `docker/nginx.conf` — dev/primary: 652 lines, 32 locations, OpenResty **+ Lua pool-router** (15 lua refs).
  - `docker/nginx-prod.conf` — production: 308 lines, 12 locations, plain **nginx:alpine, NO Lua**.
  Prod's metrics-reader regex allow-lists 8 endpoints but **omits `account-pool-state` and
  `sandbox-observed-rate-limit`** → the drift.
- **Prod is edited live, then back-ported:** git history shows
  `0bcf64f chore(bridge): capture Hetzner prod config drift into repo (SSoT)` and
  `91f753d chore(prod): commit live prod config`. The repo is **not** the source of truth for prod.
- **Five parallel compose files:** `docker-compose.yml`, `-prod.yml`, `.bedrock.yml`,
  `-platform-overlay.yml`, `-prod-platform.yml` — no `extends`/`include`; hand-parallel.
- **Two nginx bases:** OpenResty+Lua (primary) vs plain nginx:alpine (production) → the
  **customer-facing** server lacks the intelligent Lua account router.

## Decision — one repo, one `BRIDGE_ID`, everything else identical by construction

Extend the existing generator pattern to the **whole** stack so dev and prod differ **only**
by `BRIDGE_ID` (which resolves accounts/tokens, DB, worker count, routing role). Concretely:

### A. Generate the nginx routes too (closes the drift gap) — MUST
Extract **all** `location` blocks into a shared `docker/routes.conf` (env-agnostic; upstream
targets are variables). Both modes `include` it. Only `upstream {}` blocks + `${BRIDGE_*}`
env-substitution differ (already handled by `generate-bridge-compose.sh`). Then a route
**cannot** exist in one mode and not the other.

### B. One nginx base — MUST
OpenResty+Lua for **both** modes (production = customers → needs the intelligent pool-router
that reads account-pool-state to pick the freshest account). Drop the `NGINX_IMAGE_BUILD`
per-mode split.

### C. Delete the hand-parallel files — MUST
Deprecate/remove `docker/nginx-prod.conf` and the parallel compose variants as independent
SSoTs. Everything is emitted by `generate-bridge-compose.sh` + shared templates. Inputs
reduce to: `secrets/workers/*.txt`, `BRIDGE_ID`, shared templates.

### D. Deploy only from repo, never live-edit — MUST
Prod deploys go exclusively through `scripts/bridge-deploy.sh` from a committed ref. Kill the
"edit on Hetzner → capture drift back" loop. Prod lagging dev is then the **intended, known**
state (not yet deployed), never a silent hand-drift.

### E. One path = one schema — SHOULD
The per-worker pool-state endpoint (`src/main.py:5786`, docstring "for the nginx pool-router")
is internal. Either move it to an internal-only path (e.g. `/internal/worker-limiter-state`),
or make it return the same `{"accounts": {self: {...}}}` envelope (a summary of one). Then the
public `/v1/metrics/account-pool-state` has exactly one producer and one schema regardless of
routing — a routing slip can never again change the response shape.

### F. Deploy-gate: currency + parity — SHOULD (this is the "check before deploy")
Pre-deploy check fails the deploy on drift:
  - **Currency:** deployed commit on the target host == repo ref being deployed.
  - **Parity:** the target Bridge's live OpenAPI/response schemas == expected contract.
Prerequisite from ADR-0005: give the Bridge metrics endpoints real `response_model` (Pydantic)
so OpenAPI becomes a reliable source of truth (today 93% of endpoints are `schema: {}`).

### G. Prod-DB protection via `BRIDGE_ID` — SHOULD
Prod DB credentials are injected only when `BRIDGE_ID=production`; a dev deploy cannot reach
the prod DB.

## Verified state of the generator (2026-07-01, this session)

`scripts/generate-bridge-compose.sh` already models the **clean** intent — for BOTH
`BRIDGE_ID=primary` and `=production` it emits a compose that uses OpenResty+Lua
(`Dockerfile.nginx-lb`) + the SHARED `nginx.conf` + generated upstreams, and a LOCAL
`privacy-service`. **But production is NOT deployed from it.** Gaps that keep prod on the
hand-maintained files:

- **Deploy uses hand-compose for prod:** `scripts/bridge-deploy.sh` →
  `SERVER2_COMPOSE="-f docker/docker-compose-prod.yml -f docker/docker-compose-prod-platform.yml"`.
  The generator is (at most) used for primary.
- **Remote privacy (hardware):** the 7 GB prod host cannot run the ~13 GB Presidio/Docling/Flair
  model, so prod workers use `PRIVACY_SERVICE_URL=http://100.112.98.39:8100` (the DEV bridge's
  privacy-service over Tailscale) and run **no** local privacy container. The generator instead
  emits a LOCAL `privacy-service` (5 GB) for production — which would not fit / is not what runs.
- **platform-api / DB overlay:** prod has `docker-compose-prod-platform.yml` + 12 platform-api
  routes in `nginx-prod.conf` (usage/timeseries/developer-tokens → platform-api, "DB-Auslagerung
  Phase 2"). The generator does not model this.
- **No-Lua reality:** the running prod nginx is plain `nginx:alpine` (no Lua pool-router), despite
  the generator intending OpenResty — another sign prod diverged by hand.

**Conclusion:** "clean" is not "switch prod to the generator as-is" — the generator must first be
extended to model production's *legitimate* differences as **`BRIDGE_ID`-driven parameters**:
`privacy: local|remote(url)`, the platform-api/DB overlay, worker set, host sizing. Only the
route set + base image must be forced identical. This is a **tested migration on the live bridges**
(secrets live only on the hosts; the bridge cannot be generated or run offline), not an
autonomously-committable rewrite.

### Safe migration path (must be run where the secrets + hosts are)
1. On the **primary** host: `BRIDGE_ID=production scripts/generate-bridge-compose.sh` → **diff**
   the generated compose/nginx against the current `docker-compose-prod.yml`/`nginx-prod.conf`.
   The diff IS the gap list.
2. Extend the generator to close each gap as a `BRIDGE_ID` parameter (privacy local/remote,
   platform-api overlay, routes from the shared source) until the diff is only intended env values.
3. Deploy the generated stack to a **canary / the primary bridge first**, run `bridge-deploy.sh`'s
   built-in smoke + dist tests, verify `/v1/metrics/account-pool-state` == aggregate on both.
4. Cut prod over to the generated compose; delete `nginx-prod.conf` + the parallel compose files.
5. Add the currency+parity deploy-gate (F) so a future hand-edit / missing route fails the deploy.

## Migration order

1. **Unblock now (separate, not this ADR):** either deploy the app-side tolerance
   (`werking-energy` commit `cc1f4d8e2`, preflight accepts both schemas) **or** the one-line
   bridge fix (add `account-pool-state|sandbox-observed-rate-limit` to the `nginx-prod.conf`
   metrics_reader regex + redeploy the prod bridge). Either makes customer reports run today.
2. **A + B + C** (shared routes, one base, drop parallel files) — the structural fix. Test on
   primary first, then production. Verify both hosts return identical `/v1/metrics/*` shapes.
3. **D** (deploy-only-from-repo discipline + remove the back-port commits from the workflow).
4. **E** (endpoint schema separation).
5. **F + G** (deploy-gate + typed OpenAPI from ADR-0005, DB gating).

## Acceptance criteria

- `diff` of the generated nginx config between `BRIDGE_ID=primary` and `=production` shows
  **only** upstream targets / env values — **no** route-set differences.
- `GET /v1/metrics/account-pool-state` returns the **same schema** on both hosts.
- A deliberately-dropped route in the shared source fails a pre-deploy parity check (CI), not prod.
- No `chore: capture live prod config` commits are ever needed again.

## Progress log

**2026-07-01 — Item A applied to the metrics-reader route set (commit `2d4c0f5`), the exact
incident class. LIVE + verified on primary; gated on prod.**

- New shared `docker/routes-metrics-reader.conf` holds the canonical metrics-reader endpoint
  allowlist as ONE regex. `docker/nginx.conf` (10 prefix locations → `include`) and
  `docker/nginx-prod.conf` (regex block → `include`) both include it; each keeps only its own
  `upstream metrics_reader` target (primary→`metrics-reader:8000`, prod→`metrics-reader-prod:8000`).
  A metrics endpoint can no longer exist in one config and not the other.
- Both compose files mount the shared file at `/etc/nginx/routes-metrics-reader.conf`;
  `scripts/bridge-deploy.sh` mounts it into the `nginx -t` validation container too.
- Validated locally with `openresty -t` (primary base) AND `nginx -t` (prod nginx:alpine base) —
  the include works on BOTH bases, so eventual prod cutover is mechanism-proven.
- **Deployed to primary** (`bridge-deploy.sh hetzner nginx`, SHA `2d4c0f5`): nginx healthy,
  smoke green. Verified live: `account-pool-state` = aggregate `{accounts:{…}}` (4 accounts);
  reader GET endpoints 200; `usage`/`timeseries` correctly fall through to platform-api (401 auth);
  `sandbox-observed-rate-limit` is a **POST-only** reader endpoint (`metrics_reader/main.py:963`,
  returns `400 account_id required` — reader reached) → routing it to the reader is CORRECT
  (a GET returns the reader's generic "unsupported" 404; not a routing bug).

Still open (unchanged from Decision above):
- **Prod NOT yet deployed** — host `178.104.178.79` is at `1f48358`, so it still lacks BOTH the
  route fix `42748c8` AND this `2d4c0f5`. Prod's `account-pool-state` therefore still returns the
  FLAT single-worker schema (the incident). Deploying prod (`bridge-deploy.sh server2 nginx`)
  fixes the drift AND adopts the shared source in one step — **gated on Rafael** (live customer bridge).
- Item A for the OTHER routes (platform-api/auth/sandbox/health/worker-direct) not yet shared:
  the auth/sandbox regex *paths* are identical in both, but primary uses `upstream platform_api`
  while prod uses variable `proxy_pass` + `resolver` (DNS re-resolution) — sharing them needs the
  proxy mechanism unified first (behavior change, own verification). The LLM routes (chat/research)
  legitimately differ (Lua per-worker vs fallback pool) until item B.
- Items B–G unchanged. The generator (`generate-bridge-compose.sh`) is still aspirational: its
  input `secrets/workers/*.txt` does not exist on either host (real tokens live in `secrets/*`
  flat, referenced by the hand compose files); wiring it to reality is the large, non-autonomous
  migration this ADR describes.

**2026-07-01 (cont.) — Item A extended to the platform-api route set (commit `c775655`) +
deploy-gate F added (commit `b6eba0c`). LIVE + verified on primary; prod still gated.**

- New shared `docker/routes-platform-api.conf` holds ALL platform-api-destined routes (metrics
  `usage`/`timeseries`, the `auth|users|…|admin` regex, `db/health`, `sandbox`), included by BOTH
  configs AFTER the metrics-reader include. Unified onto variable `proxy_pass $platform_api` +
  `resolver`: primary previously used a static `upstream platform_api` (resolve-once → stale-IP
  502 after a platform-api restart); that block is **removed**. `$platform_api` stays a per-env
  `set` (the only legit difference). Fail-loud posture standardised: `proxy_next_upstream off` +
  `proxy_connect_timeout 5s` on all platform routes.
- Validated both engines (openresty -t primary + nginx -t prod alpine); deployed primary
  (`bridge-deploy.sh hetzner nginx`, SHA `c775655`, healthy, smoke+dist+state PASS). Live-checked:
  `db/health` 200 (PostgreSQL — proves variable proxy_pass reaches platform-api), `metrics/usage`
  + `users` 401 platform-auth (not 502, not a worker), `sandbox/*` → platform-api FastAPI 404
  (`{"detail":"Not Found"}`, NOT the `[Bridge workerX]` wrapper), `account-pool-state` still
  aggregate. **Primary is now fully single-sourced for BOTH route sets, no regression.**
- **Deploy-gate F** (`scripts/bridge-parity-check.sh <hetzner|server2>`): fail-loud checks —
  (1) currency host HEAD == origin/develop, (2) no modified TRACKED files under `docker/`
  (untracked `*.bak` only WARNs), (3) the shared includes loaded INSIDE the running nginx
  container are sha256-identical to the host repo. Verified: PASS on primary, FAIL on prod
  (deliberately behind, pre-cutover) — it catches the exact drift class from the incident.

**Verified host ground truth (2026-07-01, read-only SSH) — the cutover parameter spec:**

| aspect | primary (`hetzner`, 49.12.72.66) | production (`server2`, 178.104.178.79) |
|---|---|---|
| repo SHA | `c775655` (current) | `1f48358` (behind — pre-cutover) |
| worker set | `worker1..4` | `worker-sahori`, `worker-kurt` |
| token files | `secrets/claude_token_account1..4.txt` (+ name symlinks) | `secrets/claude_token_worker-{sahori,kurt}.txt` |
| worker→token map | worker1→account1 … worker4→account4 | worker-sahori→…-sahori, worker-kurt→…-kurt |
| privacy | LOCAL container, `PRIVACY_SERVICE_URL=http://privacy-service:8100` | REMOTE `http://100.112.98.39:8100` (dev over Tailscale); NO local privacy container |
| platform-api/DB | platform-api + postgres overlay | same, running (`wt-prod-platform-api`, `bridge-postgres-prod`) |
| nginx base | OpenResty + Lua | plain `nginx:alpine` (no Lua) — **Item B behaviour change** |

  → generator BRIDGE_ID params to implement at cutover: `WORKER_TOKEN_MAP` (per-host, flat
  `secrets/claude_token_*.txt`, NOT `secrets/workers/`), `PRIVACY_MODE=local|remote(url)`,
  `PLATFORM_OVERLAY` (layered `-f *-platform.yml`, as bridge-deploy already does), and the
  worker-naming reconciliation (generator `worker-<name>` + `upstreams.generated.conf` vs the
  deployed inline `worker1..4` in `nginx.conf`). `generate-bridge-compose.sh` header now documents
  each gap and fails loud (it will NOT run blindly / no `mkdir secrets/workers` trap).

Still open / gated (unchanged intent):
- **Prod NOT deployed** — still `1f48358`; lacks `42748c8`, `2d4c0f5`, `c775655`, `b6eba0c`. Its
  `account-pool-state` still returns the FLAT schema. One gated cutover (`bridge-deploy.sh server2 …`)
  after Rafael's OK adopts all four; run `bridge-parity-check.sh server2` first — it must go green.
- **Item B (OpenResty+Lua on prod)** — real behaviour change to the customer bridge (prod is
  nginx:alpine today). Prepared/documented only; needs its own test + approval at cutover, NOT live.
- **Generator cutover (B/C/full)** — the worker-naming + overlay reconciliation is the tested,
  non-autonomous migration; parameter spec above is turnkey but must be diff-verified on the hosts.

**2026-07-01 (cont.) — Items A+B+C SHIPPED: ONE shared OpenResty+Lua nginx.conf for
both bridges. LIVE + verified on primary (SHA `65c9650`); prod cutover startklar, gated.**

The nginx layer is now single-source. The unification did NOT go through the
whole-compose `generate-bridge-compose.sh` (that stays aspirational); it shipped a
narrower, verifiable mechanism — one shared `nginx.conf` + a focused per-topology
upstreams generator.

- **`docker/lua/pool_router.lua` topology-agnostic** (commit `14392ff`): removed the
  hardcoded `ACCOUNT_WORKER` map + fixed `WORKER_NAMES={worker1..4}`. `WORKER_NAMES`
  now parsed from the `BRIDGE_WORKERS` env (same SSoT the metrics-reader uses;
  `env BRIDGE_WORKERS;` in nginx main context; empty → loud ERR + safe worker1..4
  default). Account→worker is read live from each account's own `.worker` field in
  account-pool-state (verified: `engelmann→worker1` primary, `worker-sahori→worker-sahori`
  prod), so there is no map to keep in sync. luajit byte-compile verified.
- **`docker/nginx.conf` = the single shared source** (commit `876cb00`): no worker
  names anywhere in it. The one legit per-bridge difference (worker set + backup
  host) is `include /tmp/upstreams.conf`, emitted by the new
  `scripts/generate-bridge-upstreams.sh` into `docker/upstreams-{primary,prod}.conf`
  (compose envsubst's `${BRIDGE_BACKUP_HOST}` in). primary: worker1..4, default pool
  NO backup (dev/prod isolation preserved); prod: worker-sahori/worker-kurt, default
  pool backs up to the dev bridge (Model-B). `claude_production` backs up to
  `${BRIDGE_BACKUP_HOST}` on both. `metrics_reader` + `claude_unavail` + the
  `$backend_pool` map stay shared (`metrics-reader` resolves on both via prod's
  compose alias). The 4 hardcoded `/workerN/` debug locations collapse to ONE
  topology-agnostic regex. `${BRIDGE_PROD_HOST}` → `${BRIDGE_BACKUP_HOST}`.
- **Compose + deploy + parity wired** (commit `65c9650`): both nginx services mount
  the shared `nginx.conf` + their `upstreams-*.conf`, set `BRIDGE_WORKERS` +
  `BRIDGE_BACKUP_HOST`; **prod nginx switches nginx:alpine → build Dockerfile.nginx-lb
  (OpenResty+Lua) = Item B**. `bridge-deploy.sh` validates the shared nginx.conf for
  BOTH bridges (renders+mounts the upstreams include, `--add-host metrics-reader`) and
  builds prod nginx; `bridge-parity-check.sh` gains check 4 (in-container
  `upstreams.conf` == per-bridge repo file).
- **`docker/nginx-prod.conf` DELETED** (Item C) — no compose/script references it; a
  failed prod cutover's auto-`git reset` restores it + the nginx:alpine compose.

**Verified on primary (`bridge-deploy.sh hetzner nginx`, canary):** compose config +
`openresty -t` (BOTH param sets) PASS · nginx built OpenResty · healthy · smoke
(research 200, 14 URLs) PASS · **`bridge-parity-check.sh hetzner` 4/4 PASS** · live:
`/health` 200, `/lb-status` 200, `account-pool-state` **aggregate (4 accounts)**,
`/v1/db/health` 200, `/v1/users` 401, `/worker3/health` 200 (new regex) · pool-router
`last_refresh_status=ok`. The deploy's distribution test read "1/4 workers", which is a
**false alarm from live account state, not a regression**: at that moment only `gmail`
(worker3) was eligible (`available=True`, weekly 29%) — the other 3 were in cooldown /
near weekly cap (`available=False`), so the router correctly sent all traffic to the one
account with headroom. The test's "≥2 workers" assumption is invalid when <2 accounts are
eligible; it is optimization-signal-only and does not roll back.

**PROD CUTOVER — startklar (gated on parent; do NOT run while an energy pipeline is on prod):**
1. `scripts/bridge-parity-check.sh server2` — will FAIL currency until step 2 pulls (expected).
2. `scripts/bridge-deploy.sh server2 nginx` — pulls develop, builds the OpenResty nginx,
   recreates ONLY `wt-prod-lb`. Validation + smoke (X-Priority: production) gate it; auto-
   rollback on failure. Workers/metrics/platform-api are unchanged by this work (prod
   metrics-reader-prod already returns the aggregate with `BRIDGE_WORKERS=worker-sahori,worker-kurt`).
3. Post-cutover verify: `account-pool-state` = aggregate on prod, `/internal/pool-router/state`
   `last_refresh_status=ok`, `bridge-parity-check.sh server2` 4/4 green.

Still open (unchanged intent): Items D (deploy-only-from-repo discipline), E (endpoint
schema separation), F/G residue (typed OpenAPI + DB gating from ADR-0005). The
whole-compose `generate-bridge-compose.sh` stays aspirational (header now points to the
shipped narrower mechanism).

**2026-07-02 — PROD CUTOVER DONE + verified (both bridges single-source).**

The "startklar/gated" state above is superseded: the prod cutover was executed and both
bridges now run the shared source. Live-verified read-only this date:

- **Both hosts at SHA `984abe7`** (repo HEAD) — prod (`178.104.178.79`) no longer behind.
- **`bridge-parity-check.sh` = 4/4 PASS on both** `hetzner` and `server2` (currency + no
  modified tracked `docker/` files + in-container includes sha256-identical to repo +
  correct per-host upstreams include). Only WARN: 5 untracked `*.bak` cruft files per host.
- Shared route includes are **byte-identical across hosts** (`routes-metrics-reader.conf`
  `69f486c0`, `routes-platform-api.conf` `e4c402d7`); the sole diff is the upstreams include
  (`upstreams-primary.conf` worker1..4 vs `upstreams-prod.conf` worker-sahori/worker-kurt).
- **Acceptance criterion met:** `GET /v1/metrics/account-pool-state` returns the aggregate
  `{ts, accounts}` on BOTH (dev 4 accounts, prod 2) — the flat-schema incident is gone.
- Prod nginx-LB (`wt-prod-lb`) runs the OpenResty+Lua image (Item B); no local privacy
  container on prod (remote dev privacy over Tailscale, as designed).
- No commits and no hand-edits on either host since the 07-01 cutover → **no drift**.

Item D (deploy-only-from-repo discipline) is now documented in `CLAUDE.md` as the standing
rule; E/F/G residue unchanged.

## Links
- ADR-0005 (Bridge schema drift pre-build validator) — the parity/typing prerequisite for F.
- Incident + app-side fix: werking-energy `cc1f4d8e2` (preflight tolerates flat schema).
