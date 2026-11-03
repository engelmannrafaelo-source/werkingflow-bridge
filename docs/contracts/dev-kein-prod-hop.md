# Dev-Bridge: Schalter `PRIMARY_PEER_HOP` (Default 1 = ADR-0010 Level-1-first)

> **STAND 03.11.2026 nachmittags: KD hat den Entscheid unten zurueckgenommen.** ADR-0010 ist
> Rafaels ausdrueckliche Entscheidung (31.08.2026). Default des Generators ist wieder `1`
> (Peer-first, `docker/upstreams-primary.conf` byte-gleich zum Stand vor dem Schalter).
> `0` (kein Sprung zu Prod) bleibt als bewusst gesetzte Variante. Der Text unten beschreibt
> die Variante `0`.


Grundlage: KD-Entscheid vom 03.11.2026. Der Peer-first-Weg der Dev-Bridge
(ADR-0010, Topologie `primary`) war nicht dafür gedacht, Dev-/Staging-Last auf
die Prod-Worker zu legen. Gemessen im Prod-LB-Log: am 02.11. 14182 + 580
werking-energy-Anfragen, am 01.11. 2450 + 78, dazu Verkehr von
partner-platform, Orchestrator und CUI, jeweils mit `origin=dev` und
`hopped_trusted=1`. Dev-Staging läuft auf dem Dev-Kontingent und dem Dev-Pool.

Code: `scripts/generate-bridge-upstreams.sh` (Generator-Parameter
`PRIMARY_PEER_HOP`), Ergebnis `docker/upstreams-primary.conf`. Die gemeinsame
`docker/nginx.conf`, `docker/nginx-start.sh` und die Compose-Dateien bleiben
unverändert. `docker/upstreams-prod.conf` ist bei jedem Wert des Schalters
byteweise gleich.

## Vertrag

- `PRIMARY_PEER_HOP=0` ist die optionale Variante ohne Sprung (Standard ist seit
  der Rücknahme 1). Mit 0 laufen nicht gehoppte Anfragen ohne `X-Priority: production` laufen auf der
  Dev-Bridge lokal über `claude_workers`, mit dem Lua-Kontengate:
  `/v1/chat/completions`, `/v1/research`, `POST /v1/jobs` mit und ohne
  `Idempotency-Key`, `/v1/jobs/lookup`, die Jobliste `GET /v1/jobs` und
  Abfragen von Jobs mit eigener (`dev`) oder ohne Heimatkennung.
- Absichtlich weiter zur Prod-Bridge (`${BRIDGE_BACKUP_HOST}`):
  - **`X-Priority: production`**: `claude_production` bleibt remote-first.
    Der Header bezeichnet echten Prod-Prioritätsverkehr. Das ist dieselbe
    Aufteilung wie auf der Prod-Bridge und war schon vor ADR-0010 so
    (Rafael 31.08.2026). Ein Job mit Schlüssel und X-Priority behält seine
    feste Bridge beim Peer (`$jobs_peer_first`), ebenso wie derselbe Job ohne
    Schlüssel. Energy-Staging sendet kein X-Priority
    (`energy-staging-abo-rueckfall.md`). `$priority_pool_affinity` bleibt
    deshalb `peer`.
  - **Abfragen und Abbrüche von Jobs mit Heimat `prod`**
    (`job_prod_<hex>` → `claude_jobs_peer`, ADR-0012): Jobs, die beim
    Umstellen schon laufen, sollen nicht sterben. Eine gehoppte Abfrage
    bleibt wie bisher lokal (Ein-Hop-Schutz).
  - **`/health/production`** (gemeinsame `nginx.conf`): nur eine Probe, die
    kein Konto verbraucht.
  - Eingehend unverändert: Die Absendervertrauen-Einträge `geo $sender_is_peer`
    und `$origin_sender_trusted` behalten `${BRIDGE_BACKUP_HOST}`. Die
    Prod-Bridge darf bei ihrem eigenen Überlauf weiter auf Dev landen; das
    ist Prod-Verhalten und nicht Teil dieser Entscheidung.
- Der Gemini-Bildweg (`X-Vision-Provider: gemini`) bleibt auch mit
  X-Priority lokal, weil es ihn auf der Prod-Bridge nicht gibt.
- `PRIMARY_PEER_HOP=1` ist der Standard (ADR-0010). Die erzeugte Datei ist
  byteweise das Include vor dem 03.11.2026.
- Andere Werte führen zum Abbruch des Generators, bevor er eine Datei
  anfasst.
- Auf der Topologie `production` hat der Schalter keine Wirkung.

## Stellen, die auf `primary` zum Peer führen (vollständig, geprüft)

Die Liste stammt aus einem grep nach `BRIDGE_BACKUP_HOST`, `claude_production`,
`claude_jobs_peer` und `peer` in `docker/` und im Generator.
`tests/nginx/test_dev_no_prod_hop.py` hält sie als geschlossene Liste fest.

| Stelle | bei 0 |
|---|---|
| `$llm_backend_pool` (Chat, Recherche, `POST /v1/jobs`, Jobliste, Default für unmarkierte Abfragen) | lokal, außer bei X-Priority |
| `$jobs_peer_first` / `$jobs_keyed_route` → `claude_jobs_keyed_peer` | lokal, außer bei X-Priority. Der Block bleibt bestehen, weil `nginx.conf` ihn statisch nennt |
| `$job_poll_pool` `0:prod` → `claude_jobs_peer` | bleibt, wegen Jobs im Flug |
| `claude_production` (remote-first), `$backend_pool` in `location /` | bleibt, nur für X-Priority |
| `$priority_pool_affinity` `peer` | bleibt (folgt `claude_production`) |
| `claude_workers`, `claude_version`, `claude_workers_affin_*`, `claude_jobs_home`, `claude_jobs_keyed_local` | waren schon lokal |
| `nginx.conf`: `/health/production`, `geo`-Vertrauen | bleibt (Probe, eingehend) |

Die Kommentare in der gemeinsamen `nginx.conf` beschreiben weiter den
ADR-0010-Fall („on primary EVERY non-hopped request …“). Sie wurden
absichtlich nicht geändert, weil die Datei sonst auch für Prod andere Bytes
hätte. Maßgeblich ist der Generator.

## Schalter setzen und zurücknehmen

Variante ohne Sprung setzen: Generator mit `PRIMARY_PEER_HOP=0` für
`primary` laufen lassen, `docker/upstreams-primary.conf` committen und den
Dev-LB ausrollen (`bridge-deploy.sh hetzner nginx`).

Zurück zum Standard (ADR-0010): `PRIMARY_PEER_HOP=1 scripts/generate-bridge-upstreams.sh primary`,
die Datei committen und den Dev-LB ausrollen. `bridge-parity-check.sh`
vergleicht den Container mit der Datei im Repo. Ein nicht committeter Wert
fällt dort deshalb als Drift auf.

## Pyramiden-Szenarien

Default ohne gesetzten Wert = aus. Chat, Recherche und Job ohne Schlüssel
bleiben auf Dev-Workern. Ein Job mit Schlüssel entsteht auf Dev; Wiederholung
und Lookup finden ihn dort. Jobliste und Abfrage eigener sowie unmarkierter
Kennungen bleiben lokal. Der Gemini-Bildweg bleibt lokal. In diesem Block
erreicht keine einzige Anfrage den Peer, und das Zugriffslog zeigt
`llm_pool=claude_workers`. Abfrage und Abbruch eines Jobs `job_prod_*` gehen
zum Peer, genau einmal. X-Priority-Chat und X-Priority-Job (mit und ohne
Schlüssel) gehen zum Peer. X-Priority mit Gemini bleibt lokal. Antwortet der
Peer bei X-Priority mit 429, übernimmt der Dev-Backup wie bisher. Bei der
Rücknahme mit 1 gehen Chat, Job mit Schlüssel und unmarkierte Abfrage wieder
zuerst zum Peer. Prod-Include und Worker-Maps sind für 0 und 1 byteweise
gleich. Bei einem falschen Wert bricht der Generator ab, ohne eine Datei
abzuschneiden. Die bestehenden Peer-Mechanik-Tests (feste Bridge beim Peer,
IDEMR) laufen gegen die Variante 1, die weiter unterstützt wird.

Tests ohne Netz: `tests/nginx/test_dev_no_prod_hop.py` (Generator) und
`tests/nginx/test_dev_no_prod_hop_e2e.sh` (echtes OpenResty mit
`nginx-start.sh`, Stub-Worker und Stub-Peer im Docker-Testnetz).

## Risiken

- Mehr Last auf den vier Dev-Workern und Dev-Konten. Bei Sättigung antwortet
  der lokale Pool mit 429 bzw. `pool_exhausted` statt auf Prod
  auszuweichen. Das ist beabsichtigt.
- Jobs mit Schlüssel, die zum Umstellzeitpunkt beim Peer liegen: Eine
  Wiederholung mit demselben Schlüssel geht jetzt an Dev und legt dort einen
  **zweiten** Job an. Der Lookup findet den Prod-Job nicht mehr. Deshalb
  außerhalb laufender Energy-Läufe mit Schlüssel umstellen.
- Die Jobliste zeigt keine Jobs mehr, die vorher auf Prod angelegt wurden.
  Abfragen per Kennung funktionieren weiter.

## Ergänzung 03.11.2026: `ENERGY_SUBSCRIPTION_NO_HOP`

Ein eigener Generator-Schalter (Default 0, Ausgabe byte-identisch) hält nur
`X-App-ID: werking-energy` auf den lokalen Dev-Workern, damit der Energy-Abo-Modus
nicht per Hop auf der Prod-Bridge über den Pool läuft. Vertrag:
`energy-abo-bildweg.md` Abschnitt 3, Tests:
`tests/nginx/test_energy_subscription_no_hop.py`.
