# ADR-0009: Bridge Worker/Database Host Separation

**Status:** PARTIAL — mechanism built and validated (nginx routing, upstream
generation, metrics-reader polling, a third deploy topology), new host
prepared. The cutover itself (moving real traffic to the new host) is NOT
done and has an open blocker (see "Open blocker" below). Nothing in this ADR
has touched the live production-barrier bridge.
**Date:** 2026-08-18
**Author:** devops-workspace session (Rafael-initiated: "the prod-bridge
should be DB + routing only, workers belong on their own machine")
**Affects:** production-barrier (`178.104.178.79`), the new worker-host
(`production-barrier-neu` / `168.119.178.70`, Tailscale `100.93.143.105`,
tailnet hostname `prod-workers-1`).

---

## Context

Production-barrier is an 8GB Hetzner host running, side by side:
- `bridge-postgres-prod` — customer data, subscriptions, entitlements, the
  usage/billing ledger.
- `wt-prod-lb` — the OpenResty+Lua nginx load balancer.
- `wt-prod-platform-api` — the DB-backed API layer.
- Four worker containers (`wt-prod-worker-{sahori,kurt,coach,erk}`), each
  with a 4GB memory *limit*.

20GB of container memory *limits* are promised on a 7.6GB real host. Actual
usage sits at 3-9% of each worker's limit — nothing is on fire — but limits
exist for the case where it isn't quiet, and if two containers spike at once
the kernel OOM-killer picks a victim from the whole cgroup hierarchy, not
just from whichever container caused the spike. The database is one
container-crash away from being that victim, for a reason that has nothing
to do with the database.

Rafael's stated intent: production-barrier should be small — database and
routing only. Worker compute belongs on its own machine(s), reached over the
network like the existing GPU-privacy host or the dev-bridge's privacy
service already are.

## What already existed to build on

- `${BRIDGE_BACKUP_HOST}` (ADR-0006): nginx already fails over to a
  **different host** (the dev bridge) for the default pool. Cross-host
  proxying to a `host:port` upstream target is proven, live technology here
  — this ADR reuses the exact same mechanism for the *primary* worker set,
  not just the backup.
- Tailscale is the established substrate for cross-host Bridge traffic
  (`hetzner-bridge` 100.112.98.39, `production-bridge` 100.126.91.53,
  `gpu-privacy-1` 100.65.149.39 for smart-anonymize). The firewall
  (`fw-prod-bridge`) already allows all-TCP from the Tailscale CIDR
  (`100.64.0.0/10`) to every applied host, so a worker-host's ports need no
  new firewall rule — only Tailscale membership.
- `INSTANCE_NAME` (worker env var) already fully decouples a worker's
  **billing/account identity** from its network location. `account-pool-state`
  and the Lua pool-router read the account's own `.worker` field — not a
  Docker hostname — to know which account a worker represents. This is why
  problem 2 below has a clean answer: identity travels in an env var, not in
  a DNS name.

## The three problems (and one more the investigation surfaced)

### 1. Port collision — all four workers listen on :8000

On production-barrier, workers are Docker Compose services (`expose: "8000"`,
no host port), reachable by nginx via Docker's embedded DNS
(`worker-sahori:8000`, etc.) inside the shared `bridge-prod-net` network. A
remote host doesn't share that network, so the workers need **distinguishable
host addresses**.

**Resolution:** on the worker-host, each worker publishes its own port
(`docker-compose-worker-host.yml`: sahori→8001, kurt→8002, coach→8003,
erk→8004), bound to the host's Tailscale IP. nginx addresses each as
`100.93.143.105:800N` instead of `worker-name:8000`.

### 2. Worker names carry billing meaning

`worker-sahori` / `worker-kurt` / etc. are not arbitrary — the Lua
pool-router resolves an *account* to a *worker* via account-pool-state's live
`.worker` field, and that field is populated by each worker's own
`INSTANCE_NAME`. If the name→address resolution breaks in the move, requests
still carry the right `X-Target-Worker` header for observability, but nginx
could physically connect to the wrong container — or none — silently
misattributing usage.

**Resolution:** identity (`INSTANCE_NAME`, unchanged, moves with the
container into `docker-compose-worker-host.yml`) and network location
(nginx's `server` line) are now two independent inputs, generated from the
**same source table** so they can never drift apart:

- `scripts/generate-bridge-upstreams.sh`: `PROD_WORKER_TARGETS` (associative
  array, `name → host:port`, empty by default) overrides the upstream
  `server` line for a worker in `docker/upstreams-prod.conf`. Untouched
  workers still resolve as `name:8000` (today's exact behaviour — verified
  byte-identical output with the override table empty).
- Same generator also emits `docker/worker-map-{primary,prod}.conf`, an
  nginx `map $direct_worker $worker_target {...}` body used by the
  direct-worker debug route (`/workerNAME/...`) so that route resolves a
  moved worker's name too, not just the main traffic path.
- `src/metrics_reader/main.py`: `BRIDGE_WORKER_TARGETS` env (same
  `name=host:port` syntax, independent input — deliberately not shared
  in-process with the nginx-side table, see "why two tables" below) governs
  where the reader polls a worker's own `/health` and
  `/v1/metrics/account-pool-state` for the aggregate endpoint. Unset entries
  keep the exact `f"{worker}:8000"` behaviour that existed before this
  variable did.

Why two independent tables (nginx's `PROD_WORKER_TARGETS` and
metrics-reader's `BRIDGE_WORKER_TARGETS`) instead of one shared source: they
are consumed by two different runtimes that don't share config-loading
machinery (Lua/nginx vs. Python) and are populated at two different points
in the deploy pipeline (nginx's is baked into a generated file at commit
time; the reader's is a plain compose env var, settable at deploy time
without a code regen). Keeping them separate costs one line of duplication
per worker at cutover and avoids inventing a third config-distribution
mechanism to keep two already-different mechanisms in sync. Both are
documented as "keep in sync at cutover" at their definition site.

**Validated:** `openresty -t` passes for primary and production topologies
unmodified (byte-identical `upstreams-{primary,prod}.conf` output — diffed
against the committed files), and for a synthetic populated
`PROD_WORKER_TARGETS`/`worker-map-prod.conf` pair (two workers pointed at a
fake `100.93.143.105:8001/8002`) — proving the mechanism works, not just that
it parses.

### 3. The deploy machinery knows exactly two topologies

`scripts/bridge-deploy.sh` took `hetzner|server2|both`; every phase (compose
selection, nginx validation, smoke test, migration gate) assumed "this host
has an nginx-fronted, DB-backed bridge."

**Resolution:** a third `SERVER` value, `prod-workers`, added as a genuinely
additive branch (not a modification of the `hetzner`/`server2`/`both` paths,
which are byte-for-byte the same as before this ADR):

- New `docker/docker-compose-worker-host.yml` — the four worker services
  only (no nginx, no Postgres, no platform-api), YAML-anchor-deduplicated,
  each publishing its own port on the host's Tailscale IP.
- `WORKERHOST_HOST` (Tailscale IP), `WORKERHOST_COMPOSE`,
  `WORKERHOST_SVC_*`/`WORKERHOST_ALL`/`WORKERHOST_NEEDS_BUILD` — same shape
  as the existing `HETZNER_*`/`SERVER2_*` tables.
- `phase_validate`: skips the nginx-config validation block entirely for
  this compose (no nginx service exists on this host to validate).
- `phase_migration_gate`: a `db_container=""` (no local Postgres) short-
  circuits to a no-op — there is no schema to check on a host with no
  database, not "the check is skipped."
- Phase 5 (smoke): this topology has no public `/v1` endpoint yet (nginx
  still lives on production-barrier), so `bridge_smoke.py` — which exercises
  the FULL request path — does not apply until the (separately gated) nginx
  cutover. Per-container health, already proven in Phase 4's health wait, is
  the applicable bar today; this is stated explicitly in the deploy log
  rather than silently skipped.
- `prod-workers` is deliberately **not** part of `both` — a routine
  `bridge-deploy.sh both` never touches this host implicitly.
- `scripts/bridge-parity-check.sh` gained check 5/5: the worker-map include,
  same drift-detection shape as the existing upstreams-include check
  (4/5, renumbered from 4/4).

**Not done:** actually running `bridge-deploy.sh prod-workers` for real (it
would build workers that cannot reach the database — see the open blocker)
or wiring a "cutover" command that flips `PROD_WORKER_TARGETS` and redeploys
server2's nginx. Both are one `PROD_WORKER_TARGETS` edit +
`generate-bridge-upstreams.sh production` + `bridge-deploy.sh server2 nginx`
away once the blocker below is resolved — deliberately left as a manual,
reviewable step rather than automated, matching ADR-0006's own precedent
that this class of change is "a tested migration on the live bridges, not an
autonomously-committable rewrite."

### 4. Open blocker — workers connect to Postgres DIRECTLY (found during investigation, not in the original three)

`docker-compose-prod-platform.yml` gives every prod worker
`env_file: ../secrets/platform.env`, which carries `BRIDGE_DB_URL` pointing
at `postgres-prod:5432` — the durable `/v1/jobs` store and the budget/
activity gate depend on this. `postgres-prod` is a Docker Compose service
name, resolvable only inside `bridge-prod-net` on production-barrier itself.
Its own compose comment is explicit: `expose: "5432"` only, "Host-Port
absichtlich NICHT exposed... Reduziert Attack Surface."

A worker on the worker-host cannot resolve `postgres-prod` at all. Making it
reachable means either:

- **(a) Publish Postgres on the Tailscale interface** on production-barrier
  (`5432:5432` bound to its tailnet IP, not the public IP — the firewall's
  Tailscale-CIDR rule already covers this without a new firewall entry) and
  point worker-host's `platform.env` at that address. This is a direct
  change to how the **customer database** is exposed — small in code, but a
  real increase in attack surface for the single most sensitive container in
  the whole stack, and it is a change to the LIVE production-barrier, not
  the empty new host.
- **(b) Route worker DB access through platform-api instead of a direct
  connection** — arguably the more correct architecture (one DB ingress
  point instead of five), but a real code change to how `/v1/jobs` and the
  budget gate work, not a deploy-topology change.

**This ADR does not choose (a) or (b).** `docker-compose-worker-host.yml`
documents the blocker inline (see its header) and will start workers that
proxy LLM calls but fail durable-jobs/budget-gate calls until one of the two
is implemented — do not cut nginx traffic over before that path is verified
end-to-end. The decision is Rafael's: it changes what the customer database
is reachable from, which is exactly the class of live-Bridge change this
workspace's standing rule (`CLAUDE.md` "Bridge = HANDS OFF") requires his
explicit sign-off for, in the session actually making the change.

### 5. Also surfaced, also unresolved — metrics-reader's log-based endpoints assume a shared filesystem

`metrics-reader-prod` mounts the SAME `prod-logs` Docker volume every worker
writes to, and reads it directly for `limiter_events.*.jsonl` (throughput,
prompt-performance, request-log endpoints — `get_limiter_trajectory` et al.
in `src/metrics_reader/main.py`). This is a **different** mechanism from the
HTTP polling fixed in problem 2 (`account-pool-state`/`/health` — both now
target-aware). A worker-host worker's logs never reach production-barrier's
volume, so these specific dashboards go dark for a moved worker — degraded
observability, not a functional break (LLM proxying and the primary
`account-pool-state` aggregate both still work). Not fixed here: the
smallest honest fix is giving each worker an HTTP endpoint that serves its
own log fragment and having metrics-reader fetch+merge it — the same pattern
`_fetch_prod`/`_merge_*` in `main.py` already use for combining the two
*bridges'* dashboards, generalized from "the other bridge" to "any worker's
own host." Left as a follow-up, flagged rather than silently accepted.

## What is prepared and verified right now

- **New host `production-barrier-neu`** (`168.119.178.70`, Hetzner cx53,
  16 vCPU / 32GB / 320GB, Ubuntu 24.04): Docker CE + Compose plugin
  installed; joined Tailscale as `prod-workers-1` (`100.93.143.105`);
  `fw-prod-bridge` firewall already applied (was pre-attached); repo cloned
  at `/root/werkingflow-bridge`, `develop` branch, clean. SSH reachable from
  the dev-server both via its public IP and its Tailscale IP. No prod
  function — free to experiment on.
- **nginx mechanism validated locally** (not against any live host): both
  unmodified topologies (`openresty -t`, primary + production) pass with
  byte-identical generated `upstreams-*.conf` output vs. the pre-ADR
  committed files (regression-safe), AND a synthetic populated
  `PROD_WORKER_TARGETS`/`worker-map-prod.conf` pair passes too (the actual
  new mechanism, not just "it still parses").
- **metrics-reader change**: syntax-checked, unit-tested in isolation
  (`_worker_target()` resolves overridden names correctly, falls through to
  `name:8000` for everything else, tolerates malformed env entries without
  crashing).
- **`docker compose config` syntax-checked** for the new
  `docker-compose-worker-host.yml` (with dummy local secrets, never
  committed — `secrets/` is gitignored).

## What is explicitly NOT done

- No SSH command was run against production-barrier or the dev bridge that
  changes anything (only read-only host/inventory checks used earlier in
  this investigation — none touched their compose state, containers, or the
  running nginx config).
- `bridge-deploy.sh prod-workers` has not been run for real (would need
  Anthropic OAuth tokens for the four accounts provisioned on the new host —
  a credential-handling decision left to the cutover, not fetched or copied
  here).
- `PROD_WORKER_TARGETS` / `BRIDGE_WORKER_TARGETS` are empty in the committed
  state — this ADR ships the *mechanism*, dormant, not the cutover.
- The Postgres-reachability blocker (item 4) is undecided.
- The metrics-reader log-volume gap (item 5) is undocumented-but-flagged,
  not fixed.

## Rollback (the mechanism this ADR ships, not a hypothetical)

Because the cutover is "populate `PROD_WORKER_TARGETS` + regenerate +
redeploy `server2 nginx`," rolling back is the same operation in reverse:

1. Revert `PROD_WORKER_TARGETS` to empty (or comment out the moved worker's
   entry) in `scripts/generate-bridge-upstreams.sh`.
2. `bash scripts/generate-bridge-upstreams.sh production` — regenerates
   `docker/upstreams-prod.conf` and `docker/worker-map-prod.conf` back to
   local-worker addressing.
3. Commit + `bridge-deploy.sh server2 nginx` — `bridge-deploy.sh`'s own
   Phase 5 smoke test gates this exactly like any other nginx deploy; a
   failure auto-rolls back to the pre-deploy SHA (existing mechanism,
   unmodified).
4. The old local worker containers on production-barrier are the intended
   rollback target and should NOT be stopped/removed until the worker-host
   path has run live long enough to trust — i.e., the cutover plan keeps
   both running (extra Anthropic-account capacity briefly duplicated is
   cheap; losing the instant-rollback target is not).

No step in this rollback touches the database or platform-api — only the
nginx upstream target, which is exactly the class of change ADR-0006's
existing deploy machinery (validate → smoke → auto-rollback) already governs.

## Links

- ADR-0006 (Bridge Single-Source Centralization) — the shared-nginx.conf,
  generated-upstreams, deploy-only-from-repo discipline this ADR extends
  rather than replaces.
- `docker/docker-compose-worker-host.yml` header — the Postgres-reachability
  blocker, inline at the point someone would next touch it.
- `scripts/generate-bridge-upstreams.sh` header — the target-override
  mechanism and why it defaults to empty.
