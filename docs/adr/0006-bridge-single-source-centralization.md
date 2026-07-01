# ADR-0006: Bridge Single-Source Centralization (kill dev/prod config drift)

**Status:** PROPOSED
**Date:** 2026-07-01
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

## Links
- ADR-0005 (Bridge schema drift pre-build validator) — the parity/typing prerequisite for F.
- Incident + app-side fix: werking-energy `cc1f4d8e2` (preflight tolerates flat schema).
