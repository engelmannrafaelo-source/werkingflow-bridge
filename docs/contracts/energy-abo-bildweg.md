# Energy: Abo-Bildweg und Abo-Modus (am Pool vorbei)

Entscheid KD vom 03.11.2026; der Abo-Modus kam am selben Tag auf Wunsch des
Energy-Owners dazu. Er erweitert `energy-staging-abo-rueckfall.md` und
deutet nichts davon um. Code: `src/providers/subscription_vision.py`,
`src/providers/funding_policy.py`, `src/vision_provider.py`,
`src/docconvert_worker_vision.py`, `src/main.py` (Chat, Recherche, Dokument)
und `scripts/generate-bridge-upstreams.sh` (LB).

## 1. Abo-Bildweg

- Bilder von werking-energy laufen über das Claude-Abo, und zwar über die CLI
  der Bridge (`ClaudeCodeCLI.run_completion`). Das gilt nur dann, wenn die
  Anfrage im Staging-Rückfall läuft. Dafür müssen Kennung, Schalter und
  eigene Herkunft wie im Rückfallvertrag stimmen. Außerdem fehlt entweder der
  Pool oder er kann nicht zahlen (`PoolUnavailable`), oder der Abo-Modus gilt
  (Abschnitt 2). Betroffen sind Chat mit Bildern (gestreamt und nicht
  gestreamt) und `/v1/document/convert` mit Triage und Beschreibung.
- Ohne Abo-Modus bleibt der Pool erste Wahl. Bei Dokumenten gilt: Kann der
  Pool schon das erste Bild nicht zahlen, beginnt der ganze Bildlauf einmal
  neu auf dem Abo. Ist dagegen schon ein Bild über den Pool gelaufen, bleibt
  es beim lauten Fehler wie bisher, damit ein Dokument nicht gemischt
  finanziert wird.
- Technik:
  - Jedes Bild wird geprüft. Das Format kommt aus der Dateisignatur (PNG,
    JPEG, GIF oder WebP), nie aus dem media type des Aufrufers.
  - Grenzen: höchstens 5 MB Base64 je Bild, 20 MB je Aufruf und 20 Bilder je
    Aufruf (die Triage-Gruppe). Das Bild muss Base64 sein, eine URL wird
    abgewiesen.
  - Die Bilder liegen in einem frischen Verzeichnis (`mkdtemp`, 0700). Jede
    Datei ist 0600 und wird mit `O_EXCL|O_NOFOLLOW` geschrieben. Der Name ist
    fest (`bild-<n>.<ext>`), kein Teil des Pfads stammt vom Aufrufer.
  - Nach dem Lauf wird jede Datei überschrieben und gelöscht, auch wenn der
    Lauf scheitert. Scheitert das Löschen, wird das laut geloggt, ohne Inhalt.
  - Bildinhalte und Base64 stehen nie im Log.
- CLI-Lauf:
  - Ohne Sitzung und ohne Resume, `max_turns` = Bilder + 2.
  - Erlaubt ist nur `Read`; Bash, Write, Edit, Web, Task, Glob, Grep, LS und
    `mcp__*` sind verboten. Weil der Worker mit `bypassPermissions` läuft,
    trägt die Verbotsliste die Grenze.
  - Prompt-Injektion über den Bildinhalt: Die Antwort wird nur ausgegeben,
    wenn die CLI ausschließlich die eigenen Bilddateien gelesen hat und jede
    davon. Jeder andere Werkzeugaufruf oder Pfad, auch `..`, verwirft die
    Antwort mit 502, nicht wiederholbar.
- Zulassung ohne Umgehung:
  - `require_account_reserve()` mit der 90-%-Reserve, die Ratelimit-Sperre
    des Workers und `acquire_with_wait` des adaptiven Limiters.
  - Im Chat läuft zusätzlich `enforce_pool_admission`, und die
    CLI-Authentifizierung wird verlangt.
  - Je Worker laufen höchstens `ABO_VISION_MAX_PARALLEL = 2` Abo-Bildaufrufe
    gleichzeitig. Begründung: 4 Dev-Worker ergeben höchstens 8 gleichzeitige
    Läufe auf den Konten der Entwickler-Sitzungen. Die Probe brauchte 11 s je
    Bild. Ein N1F-Dokument (Dokument-Vision mit 4 parallel) wird damit halbiert
    statt die Konten zu fluten, und die Fristschätzung der Dokument-Vision
    rechnet mit 2 je Runde.
  - Wer länger als 120 s auf einen Platz wartet, bekommt 429 `abo_vision_busy`
    (wiederholbar).
- Ausgabe:
  - Kein stilles Kürzen. Am Ausgabelimit der CLI endet die Antwort mit
    `stop_reason=max_tokens`; im Chat ist das `finish_reason=length` (Energy:
    `vision_output_truncated`), in Dokumenten erscheint ein Vermerk.
  - Ist die Antwort länger als 200 000 Zeichen, ist das ein Fehler (502); sie
    wird nicht gekürzt.
  - Ein unfertiger Lauf (`error_max_turns`, kein Ergebnis) ergibt 502, ein
    Strom ohne Abschluss 503. Eine Ablehnung durch das Modell wird als
    `content_filter` gemeldet.
- Nie ein bezahlter Weg: kein `ANTHROPIC_VISION_API_KEY`, kein Pool-Nachversuch,
  kein Gemini (auch nicht als Dev-Standard, ein Gemini-Ziel bekommt 409), kein
  Bedrock und keine Prepaid-Tageskappe. Die 409-Riegel für Werkzeuge,
  Sitzungen, Fremd-Tiers und Pins bleiben.

## 2. Abo-Modus: am Pool vorbei

Im Abo-Modus berührt die Bridge den Credit-Pool **nie**, auch wenn er
konfiguriert ist: kein `/accounts`, kein reserve, kein Messages-Aufruf. Das
gilt für Chat (Text und Bild), `/v1/document/convert` und `/v1/research`.

| Angabe | Wirkung |
|---|---|
| Header `X-Funding-Source: subscription` (Alias `abo`) | Abo-Modus, Ursache `subscription_requested` |
| Header `X-Funding-Source: pool` | heutiges Verhalten (Pool zuerst); der Header schlägt den Schalter |
| Header mit anderem Wert | 400 `funding_source_invalid` |
| kein Header, Worker-Schalter `ENERGY_STAGING_SUBSCRIPTION_ONLY` wahr **und** `X-User-ID` in `ENERGY_STAGING_SUBSCRIPTION_ONLY_USERS` | Abo-Modus, Ursache `subscription_mode` |
| kein Header, Schalter aus oder Liste leer bzw. nur aus Leerzeichen und Kommas | heutiges Verhalten (fail-closed) |

- Der Abo-Modus wirkt nur, wenn alle Bedingungen gelten: `X-App-ID:
  werking-energy`, `ENERGY_STAGING_SUBSCRIPTION_FALLBACK` wahr,
  `BRIDGE_ORIGIN_ID=dev` und die Herkunft ist die eigene Bridge. Eine
  Prod-Anfrage über das Dev-Backup läuft nie auf dem Dev-Abo.
- Verlangt ein Header das Abo, wo es nicht gilt (Prod-Bridge, fremde App,
  fremde Herkunft, Schalter aus), antwortet die Bridge mit 409
  `subscription_funding_not_available`, nie still über den Pool.
- Die Allowlist enthält Bridge-UUIDs oder E-Mail-Adressen, kommagetrennt,
  Groß- und Kleinschreibung egal. Sie wird **nur als Worker-Env im Deploy**
  gesetzt und nie im Code. Heute ist sie **leer**: Welche ID die N1F-Läufe
  von Energy-Staging (Hetzner) tatsächlich senden, ist noch nicht belegt; die
  Bestätigung durch den Energy-Owner steht aus. Railway-Staging, lokale Läufe
  und die Demo-Kostenmessung sind nur dann nicht betroffen, wenn sie eine
  andere X-User-ID senden.
- **Grenze: nicht fälschungssicher, nur organisatorisch.** Unter dem geteilten
  API-Key behauptet der Aufrufer die `X-User-ID` selbst. Die Quell-IP ist im
  Worker unbrauchbar: `X-Real-IP` zeigt bei öffentlichem Verkehr auf
  Caddy/Docker, und `X-Forwarded-For` lässt sich bei Direktaufrufen fälschen.
  Fälschungssicher wird es erst mit einem eigenen Service-Principal für
  energy-staging (Stufe 2, `BRIDGE_PRINCIPALS_ENABLED`); das ist nicht gebaut.
- Sichtbarkeit:
  - `x_funding` bzw. das Ledger-Feld `provider_metadata` tragen
    `funding_policy=subscription_staging_fallback`, `funding_fallback_cause`,
    `funding_source=subscription`, `pool_umgangen` (true im Abo-Modus, false
    beim Rückfall) und `automatic_paid_fallback=false`.
  - Bildzeilen tragen zusätzlich `api_key_lane=vision_subscription`,
    `usage_source=subscription_cli` und `subscription_vision=true`.
  - Es gibt keine Pool-Reservierung. `real_cost_eur` ist 0 über die
    bestehende Abo-Regel (Provider `anthropic`); für `pay_per_token`-Mandanten
    gilt unverändert real = hypothetical.
- Prüfausdruck, je Worker (Schalter und Liste in dessen Umgebung):

  ```
  for w in 1 2 3 4; do docker exec wt-wrapper-worker$w python -c \
    "import json; from src.providers.funding_policy import pool_umgehung_status as s; print('worker$w', json.dumps(s()))"; done
  ```

  Die Ausgabe enthält `pool_umgangen_mit_header`, `pool_umgangen_ohne_header`,
  `wege_mit_header` und `wege_ohne_header` (chat, bild, document_convert,
  research; eine einzige Entscheidung für alle Wege), `allowlist_eintraege`,
  `bridge_origin_id` und die `gruende`. `pool_umgehung_status("<X-User-ID>")`
  prüft eine bestimmte ID. Je Anfrage beantwortet
  `funding_policy.pool_umgangen(request)` die Frage.

## 3. LB: Energy ohne Hop zur Prod-Bridge

Mit `PRIMARY_PEER_HOP=1` (ADR-0010) hoppt der Dev-LB Level-1-Aufrufe zuerst zur
Prod-Bridge. Dort würde Energy über den Pool finanziert und liefe am
Worker-Schalter vorbei. Der Generator-Schalter `ENERGY_SUBSCRIPTION_NO_HOP`
(0|1, Default 0, ein Tippfehler bricht ab, bevor eine Datei geschrieben wird)
regelt das:

- Bei `0` ist die committete `upstreams-primary.conf` byte-identisch;
  ADR-0010 ändert sich für niemanden.
- Bei `1` und nur auf primary nimmt `X-App-ID: werking-energy` (Groß- und
  Kleinschreibung egal) nie `claude_production`. Chat und Recherche sowie
  `POST /v1/jobs` mit und ohne Schlüssel laufen auf den lokalen Workern, auch
  mit `X-Priority: production`. Alle anderen Aufrufer laufen genau wie ohne
  den Schalter. Polls und Abbrüche von Jobs mit Prod-Heimatmarker erreichen
  weiter den Peer (ADR-0012).
- `upstreams-prod.conf` bleibt immer byte-identisch.
- Einschalten: `ENERGY_SUBSCRIPTION_NO_HOP=1 scripts/generate-bridge-upstreams.sh
  primary`, `docker/upstreams-primary.conf` committen und den Dev-LB ausrollen.
  Das ist ein Betriebsschritt mit Freigabe.
- Tragende Kapazität ohne Hop sind nur die Dev-Konten, gedeckelt durch die
  90-%-Reserve. Sperrt die Reserve ein Konto, antwortet dessen Worker mit 503;
  es gibt keinen Pool und keinen bezahlten Weg.

## Pyramiden-Szenarien

- **Bildweg:** Ein synthetisches PNG (oder JPEG/GIF/WebP) im Chat bei
  fehlendem Pool läuft über das Abo durch Zulassung und Limiter. Im Ledger
  steht die Fahrspur `vision_subscription` (nicht `vision_prepaid`), es gibt
  keinen Kappen-Lesezugriff und keinen bezahlten Schlüssel. Ist der Pool
  beim Bild erschöpft, wird der Aufruf neu auf dem Abo gestartet. Auf der
  Prod-Bridge bleibt das Bild strikt (503). Eine andere App bekommt keinen
  Abo-Bildweg.
- **Dokument:** Ohne Pool liefern 3 Bilder 200 mit
  `vision_funding=subscription_staging_fallback` und eine Ledger-Zeile ohne
  Reservierung. Mit Pool, der beim ersten Bild `PoolUnavailable` meldet, wird
  einmal auf dem Abo neu gestartet. Auf der Prod-Bridge gibt es weiter 409.
  Fehler auf dem Abo werden 1:1 und laut weitergegeben.
- **Sicherheit:**
  - Eingang: zu viele Bilder, zu groß, URL statt Base64, kaputtes Base64,
    falsche Signatur und ein gefälschter media type werden laut abgewiesen.
  - Injektion: Der Bildtext verlangt Bash, `~/.claude/.credentials.json` oder
    `../../etc/passwd`, oder ein Bild wird nicht gelesen: die Antwort wird
    verworfen.
  - Dateien: Der Aufrufertext erscheint nie im Pfad; Datei 0600 und
    Verzeichnis 0700 werden danach gelöscht, auch bei einem CLI-Fehler.
- **Grenzen:** Reserve oder Limiter oder Ratelimit sperren, bevor die CLI
  startet. 7 parallele Aufrufe laufen höchstens zu zweit gleichzeitig; ist
  die Wartefrist für einen Platz voll, kommt 429. Ausgabe am Limit wird
  `length`, eine Überlänge 502. Ein unfertiger Lauf ergibt 502, eine
  Ablehnung `content_filter`.
- **Abo-Modus:**
  - Mit vorhandenem Pool berühren Header oder Schalter (Allowlist-Treffer)
    den Pool bei Chat-Text, Chat-Bild, Dokument und Recherche nie.
  - Ohne Angabe bleibt alles wie heute (Pool zuerst). Ebenso, wenn die Liste
    leer ist oder nur aus Leerzeichen und Kommas besteht, die X-User-ID nicht
    passt oder fehlt.
  - Ein Header auf der Prod-Bridge, bei fremder App oder fremder Herkunft
    ergibt 409 ohne Pool-Zugriff; ein unbekannter Header-Wert 400; der Header
    `pool` schlägt den Schalter.
  - Der Prüfausdruck stimmt auf Dev und auf Prod.
- **LB:** Bei Default 0 ist die Datei byte-identisch und auf Prod immer.
  Mit 1 bleibt Energy lokal (mit und ohne `X-Priority`, beide
  `PRIMARY_PEER_HOP`-Werte). Alle anderen sowie `werking-energy-x` und
  `not-werking-energy` laufen unverändert, Polls zum Prod-Heimatmarker gehen
  weiter zum Peer. Ein Tippfehler schreibt keine Datei.

Unit-Tests ohne Netz: `tests/unit/test_energy_abo_bildweg.py` und
`tests/unit/test_energy_abo_modus.py` (Fake-CLI, Pool als Falle, nur
Platzhalter-IDs) sowie `tests/nginx/test_energy_subscription_no_hop.py`.
`tests/billing/test_ledger_real_cost.py` bleibt unverändert (Hausregel).

## Offene Punkte

- Die Allowlist-ID der N1F-Läufe ist nicht belegt. Bis dahin bleibt die Liste
  leer und der Schalter wirkungslos.
- Fälschungssichere Herkunft gibt es erst mit einem eigenen Principal.
- `thinking`, `output_config` und `max_tokens` gelten auf dem CLI-Weg nicht.
  Sie stehen im Ledger als `ignored_params` bzw. `requested_max_tokens`. Wie
  beim Text-Rückfall kann sich das Ergebnis deshalb fachlich vom Pool-Weg
  unterscheiden.
- Die CLI speichert ihren Sitzungsverlauf, darin das gelesene Bild, im
  CLI-Verlauf des Workers (`~/.claude/projects/...`). Bei Chat-Texten ist das
  heute schon so. Ein Aufräumen dieses Verlaufs ist nicht gebaut.
- Gestreamter Chat mit Bild: Kommt `PoolUnavailable` erst im Strom, gibt es
  keinen Neustart auf dem Abo. Der Abo-Modus und der fehlende Pool sind
  davon nicht betroffen.
- `ABO_VISION_MAX_PARALLEL` gilt je Worker. Die workerübergreifende Grenze
  bilden Limiter und Reserve.
- Live nicht nachgewiesen: Ob der Worker-CLI-Pfad Bilder per Read genauso
  annimmt wie die lokale Probe, zeigt erst die Live-Probe im Worker.
