# ADR-0011: Identitäts-/Budget-Föderation (Heimat-Bridge rechnet ab)

**Status:** ACCEPTED — Rafael 31.08.2026 (~07:5x, Session 44f02655, Audit-Log:
Option A der Entscheidungsvorlage; sinngemäß „wenn der Türsteher die Werkstatt
anruft, dann passt das").
**Date:** 2026-08-31 · **Author:** prod-ops-Session (7f122be0)
**Kontext:** ADR-0010 „Voraussetzung: EINE Budget-Domäne". Dies ist die dort
geforderte Vorstufe (a); nach ihrem Beweis ist die Reaktivierung der
Default-Stufe der dokumentierte Ein-Zeilen-Revert im Generator.

---

## Problem

Ein Request wird heute von der Bridge budget-geprüft und abgerechnet, deren
Worker ihn AUSFÜHRT — gegen deren lokale users/budgets-Tabellen. Führt Level 1
(Prod) einen Dev-originierten Request aus, existiert die Identität dort nicht:
gesunde Dev-Nutzer werden nichtdeterministisch 402t und Dev-UUIDs
provisionieren still JIT-Schattennutzer in der Kundendatenbank (gemessen
31.08., DevOps a0d8b084; ältester Schatten 27.07.).

## Entscheidung

**Der Request trägt seinen Ursprung; Identität, Budget-Gate und Abrechnung
laufen IMMER gegen die platform-api seiner Heimat-Bridge.** Die ausführende
Bridge stellt nur Rechenkapazität. Keine gemeinsame DB (Option b, verworfen:
UUID-Kollisions-Migration, Single Point of Failure, macht die Dev-Bridge
prod-kritisch — genau die Kopplung, die ADR-0009 abbaut).

## Mechanik

Kein neues Endpoint-Paar. Seit ADR-0009 läuft JEDER User-/Budget-/Ledger-
Zugriff der Worker als HTTP-Call durch `src/platform_client.call_platform` —
die Heimat-Bridge hat alle nötigen internen Endpoints bereits. Föderation =
origin-bewusste ZIELWAHL an genau dieser einen Stelle:

1. **Origin-Stempel (nginx, geteilte `docker/nginx.conf`).** Der LB stempelt
   auf jedem Egress `X-Bridge-Origin` (Wert = envsubst `${BRIDGE_ID}`:
   `dev`/`prod`). Ein bereits gesetzter Header wird NUR übernommen, wenn der
   Absender vertrauenswürdig ist (`geo $remote_addr`): die Peer-LB-Adresse
   (`${BRIDGE_BACKUP_HOST}`) oder das Docker-interne Netz (Worker-Self-Calls,
   z. B. Research-Job → `/v1/research`). Jeder andere Client wird auf den
   eigenen Origin ZURÜCKGESETZT — anders als beim harmlosen `X-Bridge-Hop`
   wäre ein gefälschter Origin Kosten-Umleitung, kein Selbstschaden.

2. **Request-Kontext (Worker).** Eine Middleware legt den Origin in eine
   ContextVar (`src/federation.py`). `BRIDGE_ORIGIN_ID` benennt den eigenen;
   Origin fehlt/eigen ⇒ Verhalten exakt wie heute (lokal).

3. **Zielwahl.** `call_platform(..., domain="user")` löst bei fremdem Origin
   auf den Peer auf: `FEDERATION_PEERS` (JSON-Env: origin →
   `{platformUrl, tokenEnv}`; die Service-Tokens der Bridges sind
   VERSCHIEDEN, gemessen 31.08.). `domain="local"` (Default) bleibt immer
   lokal. Peer-platform-apis sind ausschließlich über das Tailnet erreichbar
   (Binding auf die Tailscale-IP des Bridge-Hosts, Port 8300 → 8000;
   gemessene RTT dev↔prod 1–4 ms) — NICHT über den öffentlichen LB, aus dem
   ADR-0009-Grund (interne Endpoints gehören nicht an die Edge).

4. **Domänen-Zuordnung der Call-Sites.**
   - `domain="user"` (Heimat): `identity/user_resolver`, `budget/routes`
     (evaluate/deduct/Trial-Selbstprovisionierung), `budget/plan_resolution`,
     `billing/project_budgets_service`, `activity/ledger_client`,
     `api_auth/tenant_resolver`, `routing/user_provider_override`.
   - lokal (ausführende Bridge): `jobs/store_client` (der Job gehört der
     ausführenden Bridge, 500e761), `routing/prepaid_cap`,
     `research_cloud/cap` (Konten-/Key-Tageskappen sind Bridge-Ressourcen),
     `routing/app_tier_policy`, `activity/app_registry` (identische
     Migrationskataloge; global gecacht, ein per-Origin-Split würde den
     Cache vergiften), `audit/recorder` (Verarbeitungsnachweis entsteht, wo
     verarbeitet wird), `principals` (siehe offener Punkt 2).

5. **Fail-Policy: fremder Origin ohne Peer-Konfiguration = FAIL-CLOSED
   (503 `federation_unconfigured`).** Die etablierte Fail-open-Politik des
   Gates gilt für TRANSIENTE Infra-Fehler; ein unkonfigurierter Peer ist ein
   Deploy-Fehler, und Fail-open hieße hier unbudgetierte Calls plus exakt die
   Schattennutzer, die diese ADR abschafft. Transiente Peer-Fehler
   (Timeout/5xx) behalten die per-Call-Site-Politik von heute.

6. **JIT-Sperre für Fremde (Gürtel + Hosenträger).** Primär verhindert die
   Zielwahl jede lokale Schattenanlage (User-Domain-Calls laufen daheim, wo
   die Identität existiert). Der Hosenträger sind die **Fallback-Sperren**:
   jeder Direkt-DB-Fallback der geflippten Call-Sites (inkl. der
   Trial-Selbstprovisionierung — der wörtliche Schattennutzer-Pfad) wirft
   bei fremdem Origin statt lokal zu lesen/schreiben, loud mit Origin im
   Fehlertext. `sandbox/lease_service` (der zweite JIT-Pfad) braucht keinen
   eigenen Guard: `/v1/sandbox/*` gehört nicht zu den cross-bridge
   geforwardeten Pfaden (nginx forwarded nur chat/research/jobs) — eine App,
   die direkt die falsche Bridge anspricht, ist App-Fehlkonfiguration und
   war es auch vor dieser ADR.

7. **Async Jobs.** Der Origin wird bei `POST /v1/jobs` in die persistierte
   `attribution` geschrieben (`bridge_origin`) und vom Executor beim Lauf in
   den Kontext gesetzt bzw. bei Self-POSTs als Header weitergereicht — sonst
   verlöre ein claim nach Neustart oder ein Research-Self-POST den Ursprung.

## Benannte Konsequenzen / offene Punkte

1. **Prepaid-/Vision-Kappen sehen förderierten Spend nicht** (Ledger-Zeile
   liegt daheim, die Kappe liest lokal). Diese Lanes sind per-Bridge-Keys und
   nicht Teil des Level-1/2-Poolings; dokumentierte Einschränkung, kein
   Blocker. Nicht doppelt schreiben — ein Ledger, der Heimat-Ledger.
2. **Principals bleiben lokal aufgelöst — GEKLÄRT (gemessen 31.08., ~09:1xZ):**
   der Shared-API-Key ist auf beiden Bridges identisch (sha256-Vergleich der
   Worker-Env), Principals-Enforcement ist beidseitig AN, und alle 17 aktiven
   Principals sind byte-gleich (md5 der `token_hash`-Spalte beider DBs; einzige
   Abweichung ein INAKTIVER Dev-Test-Eintrag `report-vercel-test`).
   Cross-Bridge-Auth funktioniert damit heute für beide Credential-Arten.
   **Betriebsbedingung statt Code-Änderung:** der Principal-Sync muss gehalten
   werden — bei Rotation auf nur einer Seite entstehen Cross-Bridge-401s
   (forensisches Log in `verify_api_key` weist darauf hin;
   `scripts/check-principal-drift.sh` prüft es, gehört in den Stufe-B-Pre-Check).
3. **Nutzungsauswertung pro Nutzer wird EINFACHER:** die Ledger-Zeilen landen
   wieder vollständig daheim; die ADR-0010-Konsequenz „beide Ledger lesen"
   entfällt für neuen Verkehr ab Föderations-Go-Live.
4. Die 6 Bestands-Schattennutzer auf Prod bleiben bis zur Merge-Entscheidung
   unangetastet (Rafael-gated, Kundendatenbank).

## Stufe A: Messprotokoll (2026-08-31, Dev-Bridge, Session 7f122be0)

Deploy `bd07585c` um 08:40:18Z (Anlaeufe 1–3 scheiterten an
raw.githubusercontent.com-Flakiness im --no-cache-Build → lua-resty-http
seitdem gevendort). Beweise, jeweils mit Erzeuger-Kommando:

1. **Origin-Stempel + Spoof-Reset:** externer Request mit
   `X-Bridge-Origin: prod` an `49.12.72.66:8000/health` → access.jsonl
   `"origin":"dev"` (Reset, untrusted Sender). ✓
2. **Fail-closed:** Chat-Call mit Origin `prod` (kein Peer konfiguriert),
   User e127c1bd, app werking-report → HTTP 503, Gate-Log „federation
   misconfigured — failing closed", KEIN LLM-Call, keine Zeile in der
   lokalen DB. (nginx verpackt den 503-Body als generisches
   `capacity_busy` — Status stimmt, Detail nur im Worker-Log; bekannter
   Kosmetik-Punkt. `proxy_next_upstream http_503` probiert zudem alle
   4 Worker durch — harmlos, da vor dem LLM abgewiesen.) ✓
3. **Voller foederierter Roundtrip** (synthetischer Peer `test` →
   platform-api via Docker-DNS, eigener Token-Env): Chat-Call Origin `test`
   → HTTP 200; worker1-Log zeigt die KOMPLETTE Kette foederiert
   (provider-config, user-budget-state, billing-context, usage/ai-call,
   budget/deduct — je „federated to home bridge 'test'"); usage_events-Zeile
   08:46:02Z (haiku, 0.0145 EUR) + Deduct auf report-standard gelandet. ✓
4. **Netz-Beine fuer Stufe B einzeln gemessen:**
   Worker-Container → REMOTE-Tailnet-IP (prod-LB): 200/15ms ✓;
   remoter Tailnet-Node → Tailscale-gebundenes `:8300`: 200/14ms ✓.
   **Hairpin Container → EIGENE Host-Tailscale-IP geht NICHT** (ts-input
   akzeptiert Docker-Quellen nicht) — betrifft nur den synthetischen
   Selbst-Peer, nicht die echte Cross-Host-Topologie; deshalb zeigt der
   Test-Peer auf die Docker-DNS-URL.
5. **Transiente Peer-Fehler live beobachtet** (waehrend der Peer noch auf
   der unerreichbaren Hairpin-URL stand): Gate fail-open mit lautem Log,
   Provider-Pin-Check fail-closed nach seinem eigenen Vertrag, keine
   lokalen DB-Schreiber. Error-Zeilen (cost 0) eines dauerhaft
   fehlkonfigurierten Peers koennen nach MAX_ATTEMPTS im Spool beerdigt
   werden — laut, im Log sichtbar, kein Geldverlust.

Host-Konfig Dev (persistiert): `docker/.env` `PLATFORM_FEDERATION_BIND`
(Achtung: compose liest `.env` aus dem Compose-Datei-Verzeichnis, NICHT dem
Repo-Root), `secrets/platform.env` `BRIDGE_ORIGIN_ID` + `FEDERATION_PEERS`
+ `FEDERATION_TOKEN_TEST`. Der `test`-Peer bleibt als dauerhafte
Regressions-Sonde bestehen (dev-only, zeigt auf die eigene platform-api).

## Rollout

- **Stufe A (dev, frei):** Code + Unit-Tests; Dev-Deploy; Beweis der
  kompletten Mechanik über einen synthetischen Peer-Eintrag, der auf die
  EIGENE platform-api via Tailscale-Binding zeigt (fremder Origin →
  Peer-URL → Token → Gate-Antwort), ohne Prod anzufassen.
- **Stufe B (prod, NUR mit Rafaels Direktfreigabe in der ausführenden
  Session):** geteilte nginx.conf (Origin-Stempel) auf Prod, Tailscale-
  Binding beider platform-apis, wechselseitige Token-Übergabe
  (`FEDERATION_TOKEN_*`), Prod-Worker-Env.
- **Danach:** Reaktivierung der ADR-0010-Default-Stufe (Ein-Zeilen-Revert im
  Generator) — erst nach je einem gemessenen förderierten Lauf in BEIDE
  Richtungen (dev-User auf Level 1 → Dev-Budget belastet; Prod-Overflow auf
  Level 2 → Prod-Budget belastet).
