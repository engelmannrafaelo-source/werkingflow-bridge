# ADR-0010: Ein Worker-Pool, zwei Schichten (Level 1 = Prod-Konten zuerst, fuer ALLE)

**Status:** ACCEPTED (Entscheidung Rafael), Umsetzung Dev-Bridge-seitig gebaut;
Prod-Bridge-Deploy der geteilten nginx.conf-Aenderungen separat gated.
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
