# ADR-0012: Der Job trägt seine Heimat-Bridge; die Abfrage folgt ihm

**Status:** ACCEPTED — Rafael 03.09.2026 ~17:00Z, wörtlich: *„Die vier
Prod-Worker — er hätte ja alle acht nutzen sollen. Wenn die vier Prod-Worker
nicht mehr verfügbar sind, dann auf Staging umlaufen"* und *„Mache die
architektonisch sauberste Lösung. Defensive coding, fail fast."*
Damit ist der Zwei-Bridge-Überlauf (ADR-0010) BEHALTEN und der Fehler im
Abfrageweg behoben — nicht der Überlauf abgeschaltet.
**Date:** 2026-09-03 · **Author:** Bridge-Arbeits-Session (8c8bb383)
**Betrifft:** `src/jobs/` · `docker/nginx.conf` ·
`scripts/generate-bridge-upstreams.sh` · beide Bridges.

---

## Problem

Die beiden Bridges halten **getrennte** Job-Stores (ADR-0009, bewusst: keine
geteilte DB). Unter dem Zwei-Stufen-Pool (ADR-0010) kann ein `POST /v1/jobs`
von der **Peer**-Bridge bedient werden: `proxy_next_upstream http_429` läuft
die lokalen Worker ab und landet auf dem Backup-Upstream — und der IST die
andere Bridge. Die Zeile entsteht dort. Das `GET /v1/jobs/{id}` wurde
unabhängig davon geroutet (`$llm_backend_pool`, ohne Retry also immer der
ERSTE Server des Pools) und traf die andere Bridge. Antwort: `404 Async job
not found (unknown id, or expired)` — für einen Job, der lebt und
fertigrechnet. Deterministisch gemessen am 03.09.2026: 20/20 Polls 404.

## Entscheidung

**Die `job_id` nennt die Bridge, deren Store den Job hält
(`job_<home>_<32 hex>`), und der Lastverteiler routet die Abfrage nach dieser
Kennung — zustandslos und in beide Richtungen.**

`<home>` ist `BRIDGE_ORIGIN_ID` (ADR-0011, derselbe Wert, den nginx als
`X-Bridge-Origin` stempelt) — bewusst KEINE zweite Env-Variable: eine Bridge,
die „wer bin ich" für Abrechnung und Job-Ablage verschieden beantwortet, hat
zwei Identitäten, und deren Drift fällt erst als verlorener Poll auf. Nicht zu
verwechseln mit `attribution.bridge_origin`: das ist die **Budget**-Heimat des
Aufrufers, die getrennt mitreist. Ablage- und Abrechnungsheimat sind zwei
Fragen; sie stehen in zwei Feldern.

Verworfen: geteilter Store / Replikation / Doppelschreibung (hebt ADR-0009
auf), sowie das Abschalten des Überlaufs (Optionen A/D der DevOps-Vorlage) —
Rafaels Entscheid ist, alle acht Worker nutzbar zu halten.

## Mechanik

1. **Kennung** — `src/jobs/job_id.py` ist die EINE Quelle der Grammatik. Sie
   veröffentlicht auch die nginx-Regex; `tests/jobs/test_job_id_home.py` hält
   beide gegeneinander fest, damit Router und Erzeuger nicht auseinanderlaufen
   können, ohne dass ein Test rot wird.
2. **Routing** — `docker/nginx.conf` zieht die Kennung per `map` aus `$uri`
   (`$job_home_marker`); der Generator emittiert je Topologie
   `claude_jobs_home` (nur lokale Worker, KEIN Cross-Bridge-Backup — ein
   Failover zum Peer fragte garantiert den falschen Store) und
   `claude_jobs_peer`, sowie das `map` `$job_poll_pool`.
   `"1:<peer>"` → **home**: eine Abfrage, die schon einmal gesprungen ist,
   wird nie ein zweites Mal weitergereicht (Ein-Hop-Schleifenschutz, ADR-0010).
3. **Fail fast** — eine Abfrage für einen Job, dessen Heimat diese Bridge
   nicht ist, antwortet **421** `job_home_bridge_mismatch` mit beiden
   Bridge-Namen. Unbekanntes ID-Format: **400** `job_id_malformed`. Bridge
   ohne eigene Kennung: **503** `job_home_unconfigured`, fail-closed
   (ADR-0011 Punkt 5). Der 404 bleibt allein für „diese Bridge ist zuständig
   und hat die Zeile nicht mehr".
4. **Übergangsregel, befristet und laut:** IDs von VOR dieser ADR tragen keine
   Kennung und sind darum nicht routbar. Sie behalten exakt das bisherige
   Verhalten (lokal beantwortet), mit WARNING je Abfrage. Grund: sonst stürbe
   beim Umstieg jeder laufende Job. Das Fenster schließt sich von selbst — die
   TTL-Bereinigung des Stores (dev 45 min, prod ~75 min) räumt jede
   Vor-Deploy-Zeile binnen ~1 h; danach ist eine markerlose ID ein Befund.
   Im nginx-`map` entspricht dem der `default`-Zweig (= heutiges Routing),
   weshalb **beide Deploy-Reihenfolgen ungefährlich** sind. Empfohlen bleibt
   App zuerst, LB danach.
5. **Fehlpaarung scheitert beim Laden, nicht im Betrieb:** die eigene
   Map-Seite nutzt `${BRIDGE_ID}` wörtlich. Läge der dev-Include je auf einem
   prod-Host, kollidierten die Schlüssel und nginx startet nicht (verifiziert:
   `conflicting parameter "0:prod"`).

## Zwei Nebenbefunde derselben Familie, mit erledigt

- **429 benannte die falsche Grenze.** „All worker accounts have reached their
  weekly Anthropic limit" stand fest verdrahtet im Text, während die Konten am
  03.09. bei ~30 % der WOCHE und 100 % des 5-Stunden-Fensters standen, neun
  Minuten vor Reset. `account_exhausted_error(limit_window=…)` nennt jetzt das
  Fenster aus dem Capacity-Lock; wo es niemand weiß, steht „unknown" — es gibt
  keinen Rückfall auf „weekly". `reason` bleibt als Panel-Kennung unverändert,
  die Wahrheit steht in `extra.limit_window`.
- **Ein 429 tötete Jobs.** Der Self-Call des Executors ist absichtlich auf den
  eigenen Worker gepinnt und hat darum KEIN nginx-Failover. Schloss sich das
  Fenster nach der Annahme, starb der Job terminal (`UPSTREAM_HTTP_429`),
  während sieben andere Konten frei waren (03.09., drei Video-Takes). Ein 429
  ist kein gescheiterter Job, sondern einer, der noch nicht dran ist: er wird
  jetzt wie ein Dependency-Wait geparkt und vom Watchdog neu beansprucht — und
  DAS ist das Failover, das der Self-Call nicht haben kann, weil jeder Worker
  der Bridge eine stale Zeile claimen darf.

## Betriebsbedingung

`BRIDGE_ORIGIN_ID` muss auf JEDEM Worker gesetzt sein, der Jobs annimmt — auch
auf dem ADR-0009-Worker-Host (`prod-workers`, eigene `secrets/platform.env`).
Ohne sie verweigert der Worker die Job-Annahme. `/health` meldet den Wert als
`job_home`, und `bridge_smoke.py` (`jobs_home_marker`) prüft ihn, damit ein
Deploy daran scheitert und zurückrollt statt es einem Kunden zu zeigen.

## Offen, bewusst nicht gelöst

Die Isolationsfrage bleibt offen: synchrone Chat-Aufrufe laufen im
Erschöpfungsfenster weiterhin auf der jeweils anderen Bridge (ADR-0011 regelt,
dass Identität und Budget daheim ausgewertet werden — nicht, wo verarbeitet
wird). Das ist die Folge von Rafaels Entscheid für den Überlauf, nicht ein
Rest dieser ADR.
