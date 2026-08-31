# ADR-0009: Bridge Worker/Database Host Separation

**Status:** PARTIAL — mechanism built and validated (nginx routing, upstream
generation, metrics-reader polling, a third deploy topology), new host
prepared, **Schritte 1, 2, 2b, 2c und 2d des Umsetzungsplans auf `develop` und
auf der DEV-Bridge (`hetzner`) im Echtbetrieb; auf Prod (`server2`) ist davon
nichts deployt** — siehe Umsetzungsplan unten. Mit 2d ist Weg (b) vollstaendig:
der Worker-Pfad braucht keine eigene DB-Verbindung mehr, der alte
Postgres-Reachability-Blocker (Item 4) ist aufgeloest; zum Cutover fehlen noch
die drei Punkte im Header von `docker/docker-compose-worker-host.yml`
(PLATFORM_API_URL via Tailscale-Publish von platform-api — Rafael-gated). The cutover itself (moving real traffic to the new host) is NOT
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

**ENTSCHIEDEN am 2026-08-20 durch Rafael: Weg (b).** Ein Eingang zur Datenbank
statt fuenf. Begruendung in seinen Worten: die Prod-Bridge traegt eine zu kritische
Aufgabe (Kundendaten, Abos, Berechtigungen), sie darf nicht durch Worker-Last
gefaehrdet werden — die Entkopplung ist der Zweck der Uebung, und sie soll
langfristig sauber sein, nicht schnell. Weg (a) ist damit VERWORFEN: er haette die
Angriffsflaeche genau des Containers vergroessert, den die Trennung schuetzen soll.

**Nachgemessener Umfang (2026-08-20) — groesser als dieser ADR-Abschnitt annahm.**
Der Text nennt `/v1/jobs` und das Budget-Gate. Tatsaechlich greifen im Worker-Pfad
sechs Module direkt auf die DB zu (~10 Aufrufstellen):

| Modul | Stellen | Zweck |
|---|---|---|
| `src/jobs/routes.py` | 2 | dauerhafter Job-Speicher |
| `src/routing/prepaid_cap.py` | 1 | Prepaid-/Budget-Deckel |
| `src/api_auth/tenant_resolver.py` | 2 | **Tenant-Aufloesung — im Auth-Pfad JEDER Anfrage** |
| `src/principals.py` | 2 | Principal-Aufloesung, ebenfalls Anfragepfad |
| `src/audit/recorder.py` | 1 | Audit-Log (Schreibpfad) |
| `src/platform_config.py` | 2 | Plattform-Konfiguration |

Zwei davon (`tenant_resolver`, `principals`) liegen im **heissen Pfad**. Ein naiv
gebauter Innen-API-Aufruf pro Anfrage wuerde die Kopplung nicht aufloesen, sondern
nur verschieben: statt DB-Last auf der Bridge haette jede Anfrage einen zusaetzlichen
Netz-Hop dorthin. Der Entwurf muss diese Lesepfade deshalb mit kurzlebigem Cache und
ausdruecklichem Fehlerverhalten behandeln (kein stiller Fallback), waehrend Audit und
Jobs als Schreibpfade anders zu loesen sind. Das ist der eigentliche Entwurfskern,
nicht das Umbiegen der Verbindung.

### Umsetzungsplan (entschieden 2026-08-20 mit Rafael)

Kernidee: **die beiden schwierigen Pakete werden auf der BESTEHENDEN Bridge gebaut
und bewiesen — ohne dass irgendetwas umzieht.** Danach ist der Umzug keine Wette
mehr, sondern eine Adressaenderung.

**Schritt 1 — Abrechnungszeile unverlierbar machen (auf der bestehenden Bridge).**
Der Nachher-Pfad ist der einzige mit Geld-Semantik. Heute schreibt derselbe Prozess,
der den LLM-Aufruf gemacht hat, direkt in die DB; ueber Netz waere ein verlorener
Schreibvorgang nicht abgerechnete Nutzung. Loesung: dauerhafter lokaler Ausgangspuffer
im Worker + idempotentes Eintragen. Der Code hat die Nahtstelle schon — genau eine
Funktion (`persist_ai_call_activity`) und eine bereits vorhandene Trennung zwischen
*authoritative* Abrechnungszeile und *best-effort* Budget-Abzug (`_deduct_call_cost`,
"never raises"). Nur die Zeile muss garantiert ankommen; der Abzug ist nachrechenbar.
**Dieser Schritt lohnt sich eigenstaendig**: heute geht die Zeile bei einem
DB-Aussetzer verloren, danach nicht mehr — auch ohne jeden Umzug.

**Schritt 1 ist GEBAUT (2026-08-20) und laeuft auf der Dev-Bridge.** Commits
`cf583e1`, `53f1dd2`, `14f410f`, `2158d7d`, `0a1bc45` auf `develop`, alle fuenf
Vorfahren des Dev-Stands (geprueft mit `git merge-base --is-ancestor`). Auf Prod
(`server2`) nicht deployt. Der Satz "nicht deployt" stand hier bis 2026-08-21
und war da schon ueberholt — Deploy-Zustand ist Zustand, nicht Architektur, und
veraltet in einem ADR still. Was dabei
herauskam, in der Reihenfolge, in der es zaehlt:

*Die Annahme dieses Abschnitts traegt nicht so, wie sie oben formuliert ist.*
Der Text sagt, der Code habe "eine bereits vorhandene Trennung zwischen
authoritative Abrechnungszeile und best-effort Budget-Abzug". Nachgeprueft:
getrennt waren die **Kommentare**, nicht der Kontrollfluss.

- `_deduct_call_cost` stand HINTER dem grossen `try` von
  `persist_ai_call_activity` und lief auch dann, wenn dieses in seinen
  `except`-Zweig gefallen war. Bei einem TEILausfall — abgelehnter INSERT auf
  einem sonst gesunden Pool, real passiert 2026-08-01 — war das Ergebnis:
  **keine Geldzeile, aber ein Budget-Abzug.** Die Umkehrung der behaupteten
  Ordnung.
- `apply_budget_deduction` ist ein read-modify-write auf `user_budgets` plus
  FIFO-Zug durch die TopUp-Lots, **ohne jeden Dedup-Schluessel**
  (`project_budgets_service.deduct` genauso). Das ist die harte Randbedingung
  des ganzen Entwurfs: ein Nachlauf darf den Abzug nie blind wiederholen.
- `"never raises"` gilt nur fuer `Exception`. `asyncio.CancelledError` ist eine
  `BaseException` — ein Client-Abbruch mitten im Nachher-Pfad nahm die Zeile
  ohne jedes Log mit.
- `usage_events.idempotency_key TEXT UNIQUE` existiert seit Migration 016 und
  wurde auf diesem Pfad nie gesetzt; `src/sandbox/lease_service.py` benutzt ihn
  bereits genau richtig. **Kein Schema-Change, keine Migration noetig.**

Gebaut (`src/activity/ledger_spool.py` + Verdrahtung in `ai_call_writer.py`):
write-ahead auf lokale Platte mit `fsync` VOR dem ersten DB-`await`;
idempotenter INSERT (`ON CONFLICT (idempotency_key) DO NOTHING RETURNING id`);
`recorded_at` aus der Ursprungszeit des Calls statt `NOW()`; Nachlaeufer, der
Unquittiertes ueber denselben Writer einspielt und Waisen toter Prozesse
uebernimmt (Besitz ueber gehaltene `flock`, nicht ueber die pid — pids werden
im Container-Namensraum wiederverwendet); Audit-Zeile jetzt NACH der Geldzeile
und nur bei tatsaechlich erzeugter Zeile (`activities` hat keinen
Unique-Schluessel); Budget-Abzug an "dieser Versuch hat die Zeile erzeugt"
gebunden — damit verzoegert statt verloren, und nie doppelt.

Fail-loud statt stiller Selbstabschaltung: Boot-Gate `assert_spool_ready()` in
`lifespan`, in der Form der bestehenden Worker-Invarianten. Ist der Puffer an,
aber nicht arbeitsfaehig, bootet der Worker nicht. `BRIDGE_LEDGER_SPOOL_ENABLED`
ist die Reissleine (Default an), kein Normalzustand.

Benannte Ungenauigkeit, nicht versteckt: ein spaet nachgeholter Abzug zieht aus
dem Topf, der DANN aktuell ist, waehrend die Zeile im Zeitraum des Calls
verbucht ist. Begrenzt durch `MAX_AGE`, und mit einem ERROR sichtbar gemacht,
das beide Monate nennt — still waere es fuer eine spaetere Rechnungsdiskussion
unaufloesbar.

Rueckstand steht informativ auf `/health` (`ledger_spool`), macht den Worker
aber **nie** ungesund: `/health` steuert nginx-Routing und Container-Health, ein
Durchfallen dort wuerde aus "die Ledger-Zeile wird wiederholt" einen echten
Ausfall machen.

**Was Schritt 1 NICHT abdeckt — ein verbleibender Verlustkanal, gemessen nicht
vermutet (2026-08-20).** Der Puffer beginnt in `persist_ai_call_activity`. Er
kann nur retten, was ihm uebergeben wird. Bei den **Streaming-Aufrufstellen**
steht der Persist-Aufruf HINTER der `async for`-Schleife eines Async-Generators
(z.B. `_tracked_bedrock_stream` in `src/main.py`). Bricht der Client mitten im
Stream ab, schliesst Starlette den Generator, und die Zeile hinter der Schleife
wird nie erreicht — die Funktion wird also gar nicht erst aufgerufen.

Nachgemessen mit einem Minimalbeispiel (Generator abbrechen + `aclose()`):
Code nach der Schleife laeuft nicht; bei vollstaendig gelesenem Stream laeuft
er. Der Verlust ist damit heute schon da und von Schritt 1 unberuehrt.

Bewusst NICHT hier mitgefixt: die saubere Loesung (Persist ueber `BackgroundTask`
der `StreamingResponse` oder als abgeschirmte Aufgabe) ist eine Aenderung am
Antwortpfad selbst, nicht an der Nahtstelle — anderes Risiko, andere
Testflaeche, und der Geldpfad ist der schlechteste Kandidat fuer eine
Nebenbei-Aenderung. Gehoert als eigenes Stueck geplant.

Nicht getan, ausdruecklich: kein Deploy, kein Container-Neustart, keine
Aenderung an production-barrier/prod-bridge, kein Postgres nach aussen, kein
nginx, kein Umzug. Scharf wird der Puffer erst durch einen separaten,
Rafael-freigegebenen Deploy.

**Schritt 2 — Lesepfade ueber einen Eingang (ebenfalls noch lokal).**
Auth/Tenant/Principal/Budget-Tor ueber die Innen-API statt fuenf DB-Verbindungen,
mit kurzlebigem Cache und ausdruecklichem Fehlerverhalten. Alter Weg bleibt als
sofortiger Rueckfall, Validierung im Echtbetrieb ohne Ortswechsel.

**Schritt 2c — der Schreibpfad des Geldes (gebaut 2026-08-21, auf der
Dev-Bridge deployt, Prod unberuehrt).** Branch `feat/worker-dbfree-moneypath`,
gemerged nach `develop` (`0e4e00b`) zusammen mit dem Lesepfad-Rest
(`feat/worker-dbfree-reads`, `5e64734`); `bridge-deploy.sh hetzner` hat beides
am 2026-08-21 auf die Dev-Bridge gebracht (`55d98c3` -> `0e4e00b`, Smoke 11/11).
`server2` (Prod) steht unveraendert auf dem Stand davor — der Dev-First-Gate
verlangt genau diese Reihenfolge. Schritt 2
hat die LESEpfade verlegt; die Abrechnungszeile selbst schrieb der Worker
weiterhin direkt in Postgres. Damit war das Ziel "ein Worker ohne
`BRIDGE_DB_URL`" fuer den einzigen Pfad mit Geld-Semantik nicht erreicht.

`persist_ai_call_activity` haelt jetzt keine Verbindung mehr. Es entscheidet
weiterhin alles — Preis, Provider-Vokabular, `resolve_ledger_cost`, jeden
Skip-Zweig — und stellt das Ergebnis per HTTP fest. Dieselbe Regel wie in 2b:
**Daten, keine Urteile**; die reinen, unit-getesteten Funktionen bleiben im
Worker, nur die Statements mit Verbindungsbedarf ziehen um
(`src/activity/ledger_db.py`, Gegenstueck `src/activity/ledger_client.py`).

Die harte Randbedingung ist unveraendert erhalten: der Puffer schreibt
fsync-fest auf Platte VOR dem ersten Netzaufruf, und der Abzug haengt weiter an
"dieser Versuch hat die Zeile erzeugt". Ueber HTTP ist das dieselbe Aussage wie
vorher ueber die DB — die Antwort `written` gibt es pro `idempotency_key`
hoechstens einmal, ein Nachlauf hoert `duplicate` und zieht nichts ab.

Bestehende Endpunkte geprueft statt hineingezwaengt:
`POST /v1/budget/deduct` passt exakt (gleiche Argumente, gleiche Rueckgabe,
402/400 bilden die zwei Ausnahmen ab) und wird wiederverwendet.
`POST /v1/activity/log` passt **nicht**: es schreibt nur `activities`, kennt
keine `usage_events`-Spalten, stempelt `NOW()` statt der Ursprungszeit des Calls
und lehnt eine app_id ausserhalb seiner Allowlist mit 400 ab, wo dieser Pfad
bewusst NULL + `app_id_raw` bucht. Es ist das Gegenstueck der Audit-Haelfte,
nicht der Geldzeile — daher `POST /v1/internal/usage/ai-call`, das beide Zeilen
in der bestehenden Reihenfolge schreibt (Geld zuerst, Audit nur bei tatsaechlich
erzeugter Zeile, weil `activities` keinen Unique-Schluessel hat).

Kein Retry auf den nicht idempotenten Abzuegen und **kein Direct-DB-Fallback**
auf dem Schreibpfad — anders als bei den Lesepfaden aus 2a/2b. Ein Fallback nach
verlorener ANTWORT waere genau der zweite Versuch, den ein read-modify-write
ohne Dedup-Schluessel nicht vertraegt; und der Puffer ist als Wiederholmechanismus
ohnehin der bessere (asynchron, begrenzt, auf `/health` sichtbar).

Zwei Dinge lagen auf demselben Pfad und waeren nach dem Umzug still falsch
geworden — beide mitgezogen, weil sie nicht laut, sondern leise gescheitert
waeren:
- `app_tier_policy` las `app_tier_policies` direkt. Ohne DB lieferte das
  lautlos "keine Policy" — was hier nicht "keine Policy" heisst, sondern "die
  Kosten gehen an den Kunden statt an das interne `billing_account`". Fail-open
  bleibt (eine Kostenoptimierung darf keinen Call 503en), aber die Nicht-Antwort
  ist jetzt WARNING statt DEBUG.
- `app_registry.load_known_app_ids` begruendete sein Verhalten mit "keine DB ⇒
  kein INSERT, also nichts zu validieren". Genau diese Praemisse hebt 2c auf.
  Alt belassen haette jeden DB-freien Worker in die Luecke laufen lassen, gegen
  die das Modul gebaut wurde (2026-08-01, `bridge-jobs` gegen das ENUM).

Abnahme ist als Test formuliert, nicht als Behauptung:
`tests/billing/test_worker_needs_no_database.py` laesst den echten Writer mit
verbogenem `get_pool` laufen — jeder Griff zum Pool sprengt den Test.

Im Echtbetrieb nachgemessen statt behauptet (Dev-Bridge, 2026-08-21, ~25 min
nach dem Deploy): 33 Abrechnungszeilen ueber `/v1/internal/usage/ai-call`, 12
Abzuege ueber `/v1/budget/deduct`, kein Fehler auf dem Pfad. Die Zeilen sind
strukturgleich zu denen des alten DB-Pfads derselben Sitzung — gleicher
`billing_mode`, `idempotency_key` auf 100 %, gleiche Verteilung der
Kostenfelder. Der Vergleich ist der Punkt: eine blosse 200-Antwort haette auch
eine halb befuellte Zeile gedeckt. Nachpruefbar mit

```sql
select case when recorded_at > <deploy-ts> then 'nach' else 'vor' end, billing_mode,
       count(*), count(*) filter (where hypothetical_cost_eur > 0),
       count(*) filter (where idempotency_key is not null)
from usage_events where recorded_at > now() - interval '6 hours' group by 1,2;
```

**Offen, bewusst nicht hier mitgefixt:** die Direct-DB-Rueckfaelle aus 2a/2b
(E-Mail-Identitaet, `find_allocated_plan_id`) bestehen weiter und greifen, wenn
platform-api nicht antwortet. Auf einem Worker OHNE `BRIDGE_DB_URL` wirft
`get_pool()` dort — der Ausgang ist korrekt (Zeile bleibt geschuldet bzw. Abzug
unterbleibt mit Log), der Weg dorthin ist eine RuntimeError-Kaskade statt eines
benannten Fehlers. Aufraeumen gehoert zu Schritt 3, wo die Rueckfaelle laut
dieser ADR ohnehin entfallen.

**Schritt 2d — der Rest des Worker-Pfads (gebaut 2026-08-31, Commit `1061b11`).**
Die Tabelle oben (Messung 2026-08-20) war nach 2a-2c ueberholt; neu gemessen ueber
die transitive Import-Huelle von `src.main` blieben vier Stuecke wirklich Direct-DB:

- **Der Job-Store komplett** (`src/jobs/store.py`, 14 Funktionen — /v1/jobs-Routes,
  Runner, Watchdog, TTL-Cleanup). Das SQL bleibt in store.py, das damit
  platform-api-Modul ist (`/v1/internal/jobs*`); Worker gehen ueber
  `src/jobs/store_client.py` — gleiche Signaturen, Drei-Stufen-Muster der
  Lesepfade. `claim_stale_job` MUSS auf der Plattform-Seite bleiben: seine
  `FOR UPDATE SKIP LOCKED`-Atomizitaet existiert nur innerhalb eines einzelnen
  Statements — ueber HTTP ist sie exakt so atomar wie vorher, das Statement
  laeuft dort in einem Stueck. Die nicht idempotenten Zaehler-Operationen
  (mark-running/defer/claim: attempts/defer_count) sind im Client-Docstring als
  begrenzt-doppelbar benannt statt mit einer Dedup-Mechanik ueberbaut, die die
  Tabelle bewusst nicht hat — Folgen sind geldfrei und selbstheilend (Watchdog).
- **`research_cloud/cap.py`** — bekam dasselbe C6-Muster wie prepaid_cap
  (`/v1/internal/research-cloud/spent-24h`). Vorher waere der Tages-Deckel auf
  einem DB-freien Worker bei JEDER Pruefung still fail-open ins Leere gelaufen.
- **`GET /v1/metrics/anonymization`** (Worker-Endpoint, liest `audit_log`) —
  Drei-Stufen via `/v1/internal/audit/anonymization-metrics`; ohne beide Stufen
  jetzt 503 statt der alten `"db": false`-Null-Zeilen. pseudonym-monitor
  behandelt "unerreichbar" als Alarm — erfundene Nullen haetten den
  Anonymisierungs-Fehleralarm nach dem Umzug bei jeder Abfrage still geblendet.
- **Die taeglichen Sweeps** (Trial-Warnung, Budget-Vorwarnung,
  Bedrock-Reconciliation) starten jetzt in `platform_main` statt in JEDEM
  Worker: der Pool-Halter faehrt die Sweeps. Vorher vierfach redundant (nur
  durch DB-Stempel idempotent); auf einem DB-freien Worker waeren sie ein
  taeglicher Fehlerlog geworden. RESEND_API_KEY ist in `wt-platform-api` aus
  derselben `platform.env` vorhanden (live geprueft 2026-08-31).

Bewusst NICHT angefasst: `platform_config.py` (kein Worker-Aufrufer, laeuft nur
in platform-api — dort IST der Pool richtig); die Direct-DB-Rueckfaelle aus
2a/2b (sie sind der sofortige Rueckweg, solange Worker BRIDGE_DB_URL behalten,
und entfallen physisch erst mit dem Umzug); der Streaming-Verlustkanal aus
Schritt 1 (eigenes Stueck); Item 5 (metrics-reader Log-Volume) — unveraendert
offen. Abnahme als Test: `tests/jobs/test_store_client_needs_no_database.py`
(gleiche Bauart wie 2c — jeder Pool-Griff sprengt den Test).

Im Echtbetrieb nachgemessen (Dev-Bridge, 2026-08-31, Deploy `932f090`,
Smoke 11/11): ein voller Job-Lebenszyklus (create → mark-running → progress →
done) als `POST /v1/internal/jobs*` in den platform-api-Logs, dazu ein
organischer Fremd-Job mit Heartbeats ueber denselben Pfad; alle vier Worker
fahren claim-stale/abandoned/cleanup alle 30s gegen platform-api; 0 Treffer
fuer "falling back to direct DB" in allen Worker-Logs; Sweeps starten in
platform-api (Worker-Logs sweep-frei); `/v1/internal/audit/
anonymization-metrics` beantwortet die pseudonym-monitor-Abfragen mit 200.
Bedrock-Reconciliation auf dev "AWS credentials not configured — disabled":
kein Regress, die Worker hatten den Key auf dev auch nie; auf prod liegt er in
der von platform-api MITgenutzten `platform.env` (live geprueft, read-only).

**Schritt 3 — EIN Worker von vieren zieht um.** nginx behaelt drei lokal. Ein Fehler
zeigt sich an einem Viertel des Verkehrs. Rueckweg = nginx-Ziel zurueckstellen, gleiche
auto-rollback-gesicherte Route wie jeder nginx-Deploy.

**Schritt 4 — restliche drei**, danach die Log-Volume-Abhaengigkeit des
metrics-readers (`/v1/jobs` ist seit Schritt 2d erledigt).

**Dringlichkeit, ehrlich gemessen (2026-08-20):** die Prod-Bridge laeuft bei 4 Kernen
mit Last 0,14, 14 % RAM, 14 % Platte — von Ueberlastung keine Spur. Das Argument ist
NICHT Kapazitaet, sondern Schadensradius: Worker und Kundendatenbank teilen Maschine
und Schicksal. Praezedenzfall existiert (Dev-Bridge: Platte voll -> Postgres tot ->
alles 500). Deshalb bewusst OHNE Zeitdruck bauen — der Geldpfad ist der schlechteste
Kandidat fuer Hektik.

**Unveraendert gueltig:** `docker-compose-worker-host.yml`
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
