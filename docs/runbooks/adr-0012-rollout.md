# Runbook: ADR-0012 ausrollen (Job-Heimat in der `job_id`, Poll folgt ihr)

**Nichts hiervon ist gefahren.** Der Deploy liegt bei der DevOps-Session und
braucht Rafaels Direktwort in genau dieser Session (globale CLAUDE.md,
Freigabe-Kanal). Dieses Dokument ist die Kette, die Proben und der Rückweg.

Code: Branch `feat/job-home-bridge-routing` → `develop`. Entscheid + Begründung:
`docs/adr/0012-job-home-bridge-routing.md`.

---

## Die Falle, die diese Kette vermeidet

`POST /v1/jobs` wird von den **Worker**-Containern bedient — dort läuft
`src/jobs/routes.py`, also die Zeile, die die `job_id` erzeugt.
`bridge-deploy.sh both` liefert die **Prod-Worker NICHT** mit aus
(`prod-workers` ist bewusst kein Teil von `both`). Ohne diesen Schritt stempeln
die Prod-Worker keine Kennung.

Das ist hier **nicht katastrophal**, weil der `default`-Zweig des nginx-`map`
für markerlose IDs exakt das heutige Routing behält — aber es wäre ein Deploy,
der nichts bewirkt und so aussieht, als hätte er es. `prod-workers` gehört
ausdrücklich in die Kette.

## Vorbedingung, hart: `BRIDGE_ORIGIN_ID` auf JEDEM Worker

Ein Worker ohne diese Variable **verweigert die Job-Annahme** (503
`job_home_unconfigured`, fail-closed — eine unroutbare ID wäre der stille
Rückfall in genau den Fehler, den die ADR beseitigt). Sie liegt host-lokal in
`secrets/platform.env`; der Worker-Host `prod-workers-1` hat eine **eigene
Kopie**. Vor Schritt 1 lesen, nicht annehmen:

```bash
for h in 49.12.72.66 178.104.178.79 100.93.143.105; do
  echo "== $h"; ssh root@$h "grep -c '^BRIDGE_ORIGIN_ID=' /root/werkingflow-bridge/secrets/platform.env"
done
```

Fehlt sie irgendwo: **stopp**, das ist eine Host-Änderung (Rafael-gated), nicht
Teil dieses Deploys.

**Gemessen 2026-09-03 ~17:5xZ (Momentaufnahme, vor dem Deploy neu lesen):** auf
allen drei Hosts gesetzt und richtig gepaart — `49.12.72.66` → `dev`,
`178.104.178.79` → `prod`, `100.93.143.105` (prod-workers-1) → `prod`. Auch zur
LAUFZEIT bestätigt, nicht nur in der Datei: `docker exec wt-wrapper-worker1
printenv BRIDGE_ORIGIN_ID` → `dev`, `docker exec wt-worker-host-erk printenv
BRIDGE_ORIGIN_ID` → `prod`. Damit ist der einzige Kandidat für eine
Rafael-gated Host-Änderung in dieser Kette zum Messzeitpunkt AUSGERÄUMT — die
Datei zu lesen genügt aber nicht auf Dauer, ein Container, der vor einer
Änderung startete, trägt den alten Wert (Code-Präsenz ≠ Laufzeit).

## Reihenfolge: App zuerst, LB danach

Beide Richtungen sind gebaut-sicher (der `default`-Zweig macht die LB-Hälfte
rückwärtskompatibel), aber App-zuerst ist die Reihenfolge mit der kleinsten
Angriffsfläche: neue IDs tragen die Kennung, die LB ignoriert sie noch.

`bridge-deploy.sh <server> [service...]` erlaubt Teil-Deploys, also lassen sich
App-Hälfte und LB-Hälfte je Bridge wirklich trennen (Service-Namen: Kopf von
`bridge-deploy.sh`, `HETZNER_ALL` / `SERVER2_ALL`):

| # | Schritt | Befehl |
|---|---------|--------|
| 1 | App dev-Bridge (Worker + platform-api, **ohne** nginx) | `scripts/bridge-deploy.sh hetzner platform-api worker1 worker2 worker3 worker4` |
| 2 | **App Prod-Worker** — nicht vergessen, `both` enthält sie NICHT | `scripts/bridge-deploy.sh prod-workers` |
| 3 | App Prod-Bridge (platform-api, **ohne** nginx) | `scripts/bridge-deploy.sh server2 platform-api` |
| 4 | LB dev scharf | `scripts/bridge-deploy.sh hetzner nginx` |
| 5 | LB prod scharf | `scripts/bridge-deploy.sh server2 nginx` |

Nach jedem Schritt die Probe unten, bevor der nächste kommt. Zwischen 3 und 4
liegt der einzige Zustand, in dem sich am Routing etwas ändert — davor ist
alles durch Nichtstun reversibel.

Wer die Trennung nicht will, kann auch `hetzner` bzw. `server2` als Ganzes
fahren (App **und** LB in einem Zug). Das ist deshalb vertretbar, weil der
`default`-Zweig des `map` markerlose IDs beim heutigen Routing lässt — aber
Schritt 2 bleibt in JEDEM Fall zwischen dev und prod Pflicht, sonst läuft die
Prod-LB marker-scharf, während die Prod-Worker noch keine Marker setzen
(folgenlos dank `default`, aber der Deploy hätte dann nichts bewirkt und sähe
aus, als hätte er).

`bridge-deploy.sh` validiert die geteilte `nginx.conf` gegen **beide**
Topologien und rollt bei Smoke-Fehler selbst zurück. Der neue Smoke-Posten
`jobs_home_marker` liest `job_home` aus `/health`: fehlt die Identität,
scheitert der Deploy **vor** dem Kunden.

## Zeitpunkt

**LB-Neustart nur, wenn nichts läuft.** Er bricht laufende synchrone Aufrufe
hart ab (`ai_bridge_client.py` prüft nur `status_code == 200`, keine
Wiederholung). Also nach Abnahme des laufenden Beweislaufs oder morgen früh vor
Partner-Arbeitsbeginn (~05:00Z), an einer Phasengrenze. Prod-Cutover-Regel gilt
zusätzlich: nicht während einer Energy-Pipeline auf Prod.

## Proben

```bash
# nach 1-3: trägt eine neue ID die Kennung?
curl -s $AI_BRIDGE_URL/health | jq .job_home            # erwartet: "dev" bzw. "prod"
# POST -> erwartet job_<dev|prod>_<32 hex> statt job_<32 hex>

# Poll folgt dem Job über die Bridge-Grenze:
# Job auf EINER URL abgeben, auf DERSELBEN URL pollen -> 200 statt 404.
# Gegenlesen, wo die Zeile liegt:
for h in 49.12.72.66 178.104.178.79; do echo "== $h"; ssh root@$h \
  "docker exec -i bridge-postgres-prod psql -U bridge -d bridge -c \
   \"select job_id,status from ai_jobs where job_id='<ID>'\""; done

# Der Poll-Weg ist am Antwort-Header ablesbar:
curl -sI $AI_BRIDGE_URL/v1/jobs/<ID> -H "Authorization: Bearer $AI_BRIDGE_API_KEY" \
  | grep -iE 'x-backend-pool|x-job-home'
# claude_jobs_home = lokal beantwortet · claude_jobs_peer = der anderen Bridge gefolgt

# fail-fast, muss LAUT scheitern statt still 404:
curl -s $AI_BRIDGE_URL/v1/jobs/job_xxxxx_deadbeef ...   # 400 job_id_malformed
# und eine wohlgeformte ID einer fremden Bridge -> 421 job_home_bridge_mismatch

# markerlose Alt-ID (Übergangsregel) muss weiter beantwortet werden,
# und im Worker-Log eine WARNING mit "LEGACY" hinterlassen.
```

## Rückweg

Je Schritt der vorherige committete Ref über denselben Weg; `bridge-deploy.sh`
rollt bei Fehlschlag selbst zurück (Exit 1 = zurückgerollt, Exit 2 = Rollback
selbst gescheitert → von Hand). **Rückwärts in umgekehrter Reihenfolge:** erst
Prod-Bridge, dann Prod-Worker, dann dev.

Eine zurückgerollte LB mit marker-tragenden IDs ist harmlos (sie routet dann
wieder nach Pool, also wie heute). Eine zurückgerollte **App** mit scharfer LB
ist ebenfalls harmlos, weil markerlose IDs auf `default` fallen — das ist der
Zweck dieses Zweigs. Beide Halbzustände sind ausdrücklich vorgesehen.

Vorher sichern, damit der Ist-Zustand belegt ist:
`docker exec wt-wrapper-lb cat /tmp/upstreams.conf` und dasselbe auf
`wt-prod-lb`.

## Nach dem Rollout, mit Datum

Nach ~2 h darf keine markerlose ID mehr auftauchen (die Store-TTL räumt
dev 45 min / prod ~75 min). Prüfen:

```bash
ssh root@49.12.72.66 "docker logs wt-wrapper-worker1 --since 2h 2>&1 | grep -c 'LEGACY (unmarked)'"
```

Bleibt der Zähler > 0, hat ein Client eine ID zwischengespeichert — das ist ein
Befund, kein Rauschen. Die Übergangsregel ist als befristet dokumentiert, nicht
als Dauerzustand.
