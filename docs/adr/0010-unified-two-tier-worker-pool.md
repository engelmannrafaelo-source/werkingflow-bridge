# ADR-0010: Ein Worker-Pool, zwei Schichten (Level 1 = Prod-Konten zuerst, fuer ALLE)

**Status:** Entscheidung ACCEPTED; Umsetzung der Default-Pool-Stufe **PAUSIERT
auf X-Priority-only** (2026-08-31, ~1h nach Go-Live zurueckgenommen) wegen einer
beim Live-Betrieb gemessenen, im Entwurf unterschaetzten Voraussetzung — siehe
"Voraussetzung: EINE Budget-Domaene" unten. Der Rest (Hop-Guard auf beiden
Bridges, Reads folgen dem POST-Pool, hopped/llm_pool-Sichtbarkeit, X-Priority
Level-1-first) bleibt LIVE (Dev 06:09Z, Prod 06:34Z; Prod-Freigabe verifiziert
per Audit-Log `/api/audit/inputs`: 06:28:34Z "ja dnan mach da auch ueberall die
jas" + 06:31:04Z Delegation, Session 44f02655).

## Voraussetzung: EINE Budget-Domaene (gemessen 2026-08-31, DevOps a0d8b084)

"Ein Pool, zwei Schichten" setzt stillschweigend "EIN Identitaets-/Budget-Raum"
voraus — den gibt es (noch) nicht: jede Bridge fuehrt eigene users/budgets.
Ein Dev-Request, der auf Level 1 ausgefuehrt wird, laeuft durch das Budget-Gate
der PROD-DB. Gemessen: Dev-User `e127c1bd` (interactive@werkingflow.com, auf
Dev kerngesund: report-standard 22/100) bekam auf einem Level-1-Job
`UPSTREAM_HTTP_402 trial_expired` — weil auf Prod nur ein JIT-Schattennutzer
(`jit-e127c1bd-...@werking-report.local`, Trial 25.0 aufgebraucht) existiert.
Zwei Folgen, beide inakzeptabel: gesunde Dev-Nutzer werden nichtdeterministisch
402t (nur wessen Schatten-Trial leer ist), und Dev-UUIDs provisionieren STILL
Schattennutzer in der Kundendatenbank (aeltester 27.07. — der Mechanismus
bestand fuer X-Priority-Fehlkonfigurationen schon vorher). X-Priority selbst
ist davon NICHT betroffen: dessen Nutzer (Energy/Safety prod) SIND Prod-Nutzer.

Der Weg zum Zielbild braucht daher zuerst eines von beiden, als eigenes,
geplantes Stueck: (a) Budget-/Identitaets-Foederation (das Gate der
ausfuehrenden Bridge fragt fuer fremde Identitaeten die Heimat-Bridge) oder
(b) eine gemeinsame Identitaets-/Budget-DB fuer beide Bridges. Danach ist die
Reaktivierung der Default-Stufe ein Ein-Zeilen-Revert im Generator
(`$llm_backend_pool`-Map, primary).

Aufraeum-Punkt daneben: die bestehenden `jit-*@werking-report.local`-Schatten-
nutzer auf Prod (mit Usage-Historie, DELETE wird 409en) gehoeren gesichtet.
**Date:** 2026-08-31
**Decider:** Rafael (31.08.2026 mittags, via Koordinations-Session; sinngemaesser
Wortlaut unten). **Author:** prod-ops-Session.
**Supersedes:** das Isolations-Prinzip des Default-Pools aus
`generate-bridge-upstreams.sh` ("a dev request must fail-fast, never spill onto
the customer bridge") und den entsprechenden Teil von ADR-0006/ADR-0009.

---

## Die Entscheidung (Rafael, sinngemaess)

> Es soll keine getrennten Dev-/Prod-Worker-Pools mehr geben, sondern EINEN
> 8-Worker-Pool in zwei Schichten. Level 1 = die vier bisherigen Prod-Worker
> (Konten erk/coach/kurt/sahori) — werden IMMER zuerst verwendet, von beiden
> Bridges und von ALLEN Pools inklusive Default (auch mein eigener
> Adhoc-Verkehr). Level 2 = die vier Dev-Worker (Konten
> office/gmail/werking/engelmann) — reine Ueberlauf-Schicht, wenn die
> Level-1-Konten ausgelastet sind.

Das bis dahin dokumentierte Isolations-Prinzip (Dev-Default-Traffic scheitert
schnell lokal, statt auf die Kunden-Bridge ueberzulaufen) ist damit **bewusst
aufgehoben** — das ist Rafaels Architektur-Entscheidung, kein Drift.

**Benannte Konsequenz:** Dev-Last (inklusive Rafaels Adhoc-Verkehr und allem,
was Sessions auf dem Dev-Server erzeugen) konkurriert ab jetzt mit Kundenlast
um die Level-1-Konten. Der Schutz der Kunden liegt nicht mehr in der Trennung,
sondern darin, dass Level 2 als Ueberlauf existiert und die Level-1-Konten
zuerst durch das Konten-Headroom-Gating (pool_router/Capacity-Locks der
Ziel-Bridge) verwaltet werden.

**Zweite benannte Konsequenz (bestand fuer X-Priority-Traffic schon vorher,
gilt jetzt fuer viel mehr Verkehr):** ein Request wird von der Bridge
abgerechnet, deren Worker ihn ausfuehrt. Dev-originierter Verkehr, der auf
Level 1 laeuft, schreibt seine Ledger-/Budget-Zeilen in die PROD-Datenbank
(und umgekehrt fuer Ueberlauf). Nutzungsauswertung pro Nutzer muss dafuer
beide Ledger lesen — nicht neu, aber jetzt mengenrelevant.

## Umsetzung

Scope: die **Anthropic-Konten-konsumierenden** Pfade — `/v1/chat/completions`,
`/v1/research`, `POST /v1/jobs`. Privacy-/Dokument-Konvertierung, `/health`
und die Worker-Metrik-Endpunkte bleiben lokal: sie verbrauchen keine
Konto-Kapazitaet, haengen an je-Bridge-Ressourcen (Privacy-Service, eigenes
Log-Volume), und ein Monitoring-Endpunkt, der die Zahlen der ANDEREN Bridge
liefert, waere ein kaputtes Monitoring.

Mechanik (alles in `generate-bridge-upstreams.sh` + der geteilten
`docker/nginx.conf`):

1. **Pools bleiben, Routing aendert sich.** `claude_workers` bleibt auf beiden
   Topologien unveraendert (dev: nur lokale Worker; prod: lokale Worker +
   Dev-Bridge als `backup`). Auf der Dev-Bridge wird saemtlicher
   nicht-gehoppter LLM-Traffic (egal ob mit oder ohne `X-Priority`) in den
   seit `600c7fe` remote-first konfigurierten `claude_production`-Pool
   geroutet: Prod-Bridge primaer, lokale Dev-Worker als `backup`. Die
   Pool-Wahl steht in einer pro Topologie **generierten** Map
   (`$llm_backend_pool` im Upstreams-Include).

2. **`X-Bridge-Hop` macht Cross-Bridge-Failover einmalig.** Jede vom LB
   proxied Anfrage traegt `X-Bridge-Hop: 1`. Eine Bridge, die eine bereits
   gehoppte Anfrage empfaengt, bedient sie ausschliesslich aus ihrer lokalen
   Schicht (inkl. Lua-Konten-Gating) und leitet nie wieder cross-host weiter.
   Damit ist die Kette exakt `Level 1 -> Level 2 -> Fehler` und beweisbar
   loop-frei — wichtig, weil `proxy_next_upstream ... http_429` mit
   unbegrenzten Tries sonst bei beidseitiger Saettigung unbegrenzt
   ping-pongen wuerde (dieses Loop-Fenster bestand fuer den
   `claude_production`-Pool schon vor dieser ADR; der Guard schliesst es
   mit). Ein Client, der den Header selbst setzt, erzwingt lediglich
   lokale Bedienung — harmlos.

3. **Lua-Konten-Gating bleibt wo es Sinn hat.** Nicht-gehoppte Anfragen auf
   der Dev-Bridge gehen OHNE lokales pool_router-Veto zur Prod-Bridge (deren
   pool_router/Capacity-Locks gaten die Level-1-Konten). Gehoppte Anfragen
   (= Level-2-Ueberlauf) durchlaufen das volle lokale Konten-Gating der
   empfangenden Bridge.

## Rollout

- Dev-Bridge (`bridge-deploy.sh hetzner`): frei deploybar, dort wird die
  Mechanik im Echtbetrieb bewiesen.
- Prod-Bridge: fuer das Zielbild ist prod-seitig KEINE Pool-Aenderung noetig
  (beide Pools dort sind bereits Level1-first mit Dev-Backup). Der naechste
  Prod-Deploy bringt die geteilte nginx.conf (Hop-Guard + Map-Umbau) mit —
  wie jeder Prod-Bridge-Deploy nur mit Rafaels Direktfreigabe in der
  ausfuehrenden Session.
- Rueckweg: Generator-Commit revertieren + regenerieren + nginx-Deploy — die
  gleiche auto-rollback-gesicherte Route wie jeder nginx-Deploy (ADR-0006).

## Verhaeltnis zu ADR-0009

Unveraendert. Der Ein-Worker-Cutover (Schritt 3) verschiebt nur, WO die
Level-1-Worker laufen; sobald sie auf dem Worker-Host mit publizierten
Tailscale-Ports leben, koennen BEIDE Bridges sie direkt adressieren
(PROD_WORKER_TARGETS-Mechanik) statt ueber die Prod-LB-Indirektion — die
natuerliche Endform dieses Zielbilds.
