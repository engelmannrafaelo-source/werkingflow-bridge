# Wartung der Bridge

Diese Seite ist der Einstieg fuer einen konkreten Auftrag zur Implementierung,
Diagnose oder zum Betrieb der Bridge. Fuer API-Nutzung und Recherche genuegt
die [Nutzungsanleitung](../README.md).

Vor Arbeiten am Dienst die aufgabenbezogenen Unterlagen gezielt lesen:

- [Betriebsregeln und Architektur](operations/session-context.md)
- [Setup-Referenz](operations/setup-reference.md)
- [Ergaenzende Betriebsreferenzen](operations/additional-references.md)
- [Architektur-Entscheidungen](adr/)

Die Setup-Referenz enthaelt auch historische Anleitungen. Massgeblich sind die
aktuellen Betriebsregeln und Architektur-Entscheidungen; Live-Zustand erneut pruefen.
Bestehende Freigaben bleiben verbindlich. Keine eigenmaechtigen Eingriffe oder
manuellen Konfigurationsaenderungen auf laufenden Hosts. Deploys nur ueber den
vorgesehenen Repository-Prozess aus einem freigegebenen, committeten Stand.

## Git-Identitaet in parallelen Worktrees

`user.name` und `user.email` werden pro Sitzung im eigenen Worktree gesetzt,
mit `git config --worktree user.name ...` und `git config --worktree user.email ...`.
Dafuer aktiviert das Repository `extensions.worktreeConfig=true`.
Die gemeinsame Repo-Konfiguration enthaelt keine Sitzungsidentitaet;
`user.useConfigOnly=true` verhindert eine automatisch erratene Identitaet.
Vor dem Commit `git var GIT_AUTHOR_IDENT` und
`git config --show-origin --get-regexp '^user\.'` pruefen.
`git config --local user.name/email` gilt dagegen fuer alle Worktrees.
Bereits veroeffentlichte Commits werden wegen einer falschen Identitaet nicht
umgeschrieben; die Herkunftsberichtigung gehoert in den zugehoerigen Befund.

## platform-api im Deploy

Seit ADR-0011 haengen an einer platform-api auch Jobs, die auf den Workern
der ANDEREN Bridge laufen: Ein Job mit Herkunft dev auf einem Prod-Worker
fragt Pin, Identitaet und Budget bei der Dev-platform-api ab. Das Neuanlegen
der platform-api laesst rund 3 s ohne Antwort (gemessen 10.10.2026, BR7).
Zwei Stellen sorgen dafuer, dass daraus kein endgueltig gescheiterter Job wird.

- **Im Code.** Die Pin-Abfrage und die Identitaetsaufloesung davor
  wiederholen sich nach `RESTART_BRIDGING_BACKOFFS_S`
  (`src/platform_client.py`), insgesamt etwa 7,5 s, mit fester Obergrenze.
  Antwortet die platform-api auch dann nicht, bleibt die Abfrage fail-closed:
  Es wird nichts geraten. Die Ablehnung ist dann aber wiederholbar
  (`ProviderConfigTemporarilyUnavailable`). Der Endpunkt setzt dazu
  `X-Bridge-Dependency-Unavailable: provider-config`, der Job-Executor macht
  daraus 424, und der Runner stellt den Job zurueck. Die Frist steht in
  `registry.DEPENDENCY_PATIENCE`: etwa 15 min, danach scheitert der Job laut
  mit `UPSTREAM_HTTP_424`. Echte Antworten bleiben sofort endgueltig, also
  401/403/404 der Heimat-Bridge, ein unbekannter Anbieter oder ein fehlender
  Peer.
- **Im Deploy.** Enthaelt ein Deploy `platform-api` (auf server2 auch
  `postgres-prod`, die Datenbank der platform-api), laeuft
  `scripts/platform-api-job-gate.sh` einmal vor Phase 4, also vor dem ersten
  Neuanlegen ueberhaupt. Laege der Gate direkt vor `platform-api`, waere auf
  server2 die Datenbank schon neu angelegt; eine Ablehnung haette eine
  DB-Luecke hinterlassen und der Rueckbau eine zweite (BR8R M1). Der Preis:
  Zwischen Gate und Neuanlegen liegt der Bau des Images. Ein Job, der in
  diesem Fenster startet, faellt notfalls in die Ueberbrueckung im Code. Die Probe
  (`scripts/platform_api_job_gate.py`, nur Standardbibliothek) wird per stdin
  in einen laufenden Worker der betroffenen Bridge gereicht. Sie fragt
  lesend `GET /v1/internal/jobs-maintenance/active`, und zwar beim lokalen
  Store nach allen aktiven Jobs und bei jedem Peer aus `FEDERATION_PEERS`
  nach den Jobs mit dieser Bridge als Herkunft. Solange Jobs aktiv sind,
  wartet der Deploy, hoechstens `PLATFORM_API_JOBGATE_WAIT_S` Sekunden
  (Default 900, 0 = nur pruefen). Sind danach noch Jobs aktiv oder ist der
  Zustand nicht pruefbar, lehnt er ab. Dann ist kein Container neu angelegt,
  nichts wird zurueckgerollt, nur der Checkout wird zurueckgesetzt. Auch das
  Image ist dann noch nicht neu gebaut.
- **Einfuehrung.** Hat ein Ziel den Endpunkt noch nicht (HTTP 404, Image vor
  BR8), lehnt der Deploy ab und nennt `PLATFORM_API_JOBGATE_EINFUEHRUNG=1`.
  Der Schalter gilt nur fuer den einen Aufruf und nur fuer 404, nie fuer
  Timeouts oder andere Fehler. Er gehoert in ein ruhiges Zeitfenster.
  Solange eine Bridge die alte Fassung faehrt, sieht der Gate deren Jobs
  nicht.
- **Kein rollender Ersatz.** Die platform-api hat einen festen
  Containernamen und einen festen Tailnet-Port (8300). Ohne vorgeschalteten
  Proxy koennen zwei Instanzen nicht nebeneinander laufen. Die Luecke bleibt
  also, der Gate bestimmt nur, wann sie entsteht.

## Fehlervertrag: retryable und retry_after_s

Jeder Fehler, den die Bridge nach aussen gibt, traegt zwei Felder:
`retryable` (dieselbe Anfrage kann spaeter unveraendert gelingen) und
`retry_after_s` (Wartezeit, wenn die Bridge sie kennt, sonst `null`).
Die Regeln stehen an einer Stelle, in `src/error_contract.py`:

1. Wer es weiss, setzt das Urteil ausdruecklich, zum Beispiel die
   Pin-Abfrage, die research-cloud-Kappe oder der Job-Runner. Es wird
   danach nicht neu abgeleitet, auch nicht aus dem Text.
2. Ohne ausdrueckliches Urteil entscheidet der HTTP-Status
   (408, 425, 429, 5xx = wiederholbar), wie schon immer in `bridge_error()`.
3. Ein Job-Fehler, den niemand eingeordnet hat, gilt nicht als
   wiederholbar. Eine Wiederholung kostet einen ganzen Lauf.

Wo es landet:

- **Direkter Aufruf** (Chat sync, Fehler-Events im Stream): im
  Fehler-Envelope. Ein `HTTPException` mit eigenem `retryable` behaelt es,
  statt den Statuswert zu erben.
- **Research und Doc-Agent** antworten mit HTTP 200 und `status="error"`:
  Die Felder stehen im Antwortkoerper. Ein Fehler ohne Urteil ist
  `retryable: false`. Der Textmarker von `_mark_retryable` bleibt fuer
  aeltere Textleser stehen und wird von derselben Stelle gesetzt.
- **Jobs**: im Fehlerobjekt von `GET /v1/jobs/{id}`. Der Worker bestimmt das
  Urteil, die platform-api der Job-Heimat speichert es. Zeilen ohne
  gespeichertes Urteil, also aeltere oder von einer platform-api vor BR9
  geschriebene, bekommen es beim Abruf aus dem `code`.
  `UPSTREAM_HTTP_424` und `_429` sind endgueltig, weil die Bridge vorher
  schon selbst gewartet hat.
- **Geparkte Jobs** bleiben `pending`. `deferred_until` in der Zukunft heisst:
  Die Bridge wartet absichtlich und startet den Job dann selbst neu.
  Dazu kommen `defer_count` und `defer_reason`.

## Erkunder im Deploy

Auf dem Dev-Ziel gehoeren Ausgang, drei Plaetze und Leitstand zu einer
Release-Gruppe. Auch eine explizite Auswahl eines einzelnen Erkunder-Dienstes
rollt diese Gruppe aus. Das gemeinsame Image wird einmal mit `GIT_COMMIT`
gebaut; derselbe SHA wird bei `compose up` fuer den Image-Tag verwendet.
Vor jedem Container-Eingriff wartet der Deploy am Leitstand auf Leerlauf:
`ERKUNDER_DEPLOY_WAIT_S` setzt die Frist in Sekunden (Default 900, 0 prueft
sofort). Laufende Bericht-IDs erscheinen im Log. Bei Fristablauf, nicht
pruefbarem Zustand oder unerreichbarem Leitstand scheitert der Deploy laut;
kein Container wird gestoppt, nur der Checkout wird zurueckgesetzt.

`POST /deploy/pruefen` prueft laufende Berichte unter derselben Sperre wie
`/start` und schliesst die Annahme neuer Berichte erst bei Leerlauf. Dadurch
kann zwischen Pruefung und Stop kein neuer Bericht hineinrutschen. Die
Erkunder-Gruppe kommt danach zuerst, der Leitstand startet nach den Plaetzen.
Auch Rollback prueft einen noch laufenden Leitstand vor dem Stop. Fehler beim
Freigeben der Annahme (`DELETE /deploy/pruefen`) bleiben laut; ein abgerissener
Deploy nach erfolgreicher Sperre verlangt eine bewusste Freigabe oder Neustart.
Alle Deploy-Routen verlangen den internen Schluessel.

### Einmalige Einfuehrung auf einem Altimage

Fehlt das Protokoll nachweislich (`POST /deploy/pruefen` antwortet authentifiziert
mit HTTP 404), verweigert der normale Deploy den Stop und nennt
`ERKUNDER_DEPLOY_EINFUEHRUNG=1`. Die Probe wird samt Port aus der lokalen
Wartungsfassung uebertragen und braucht kein neues Python-Modul im Altimage.
Timeout, Transportfehler, andere HTTP-Status und ungueltige Antworten erlauben
auch mit dem Schalter keine Einfuehrung.

Die freigegebene Erstumstellung erfolgt in einem ruhigen Zeitfenster bei
pausierten Auftraggebern und vollstaendig leerem `/arbeit`:

```bash
ERKUNDER_DEPLOY_EINFUEHRUNG=1 scripts/bridge-deploy.sh hetzner erkunder --dry-run
# Erst nach Freigabe des echten Deploys, aus dem committeten Stand:
ERKUNDER_DEPLOY_EINFUEHRUNG=1 scripts/bridge-deploy.sh hetzner erkunder
```

Der Schalter gilt nur fuer diesen Aufruf. Er funktioniert auch beim Gesamtdeploy
und wird bei vorhandenem neuen Protokoll ignoriert: Dann gilt weiterhin die
normale atomare Annahmesperre. Eine Dienstliste ohne Erkunder bleibt unberuehrt.

Der Einfuehrungsweg prueft `/arbeit` vor dem Stop im alten Leitstand. Jeder
Eintrag, auch ein abgeschlossener Bericht oder eine unbekannte/versteckte Datei,
verhindert den Stop. Es wird nichts geloescht; vorhandene Berichte sind zuvor
ueber den bestehenden Aufraeumweg zu behandeln. Nach erfolgreicher Vorpruefung
stoppt das Werkzeug nur den Leitstand und prueft dasselbe Volume nochmals in
einem netzlosen Hilfscontainer mit dem exakten alten Image und nur lesbaren
Mounts. Erst bei erneut bestaetigtem Leerlauf folgt der normale Gruppen-Deploy.

Scheitert der Stop oder die Nachpruefung (etwa durch einen inzwischen gestarteten
Bericht), wird der alte Leitstand wieder gestartet und der Deploy laut
abgebrochen. Auch ein fehlgeschlagener Wiederanlauf bleibt CRITICAL; es gibt
keinen Gruppen-Rollout. Der Pfad protokolliert `ERKUNDER-EINFUEHRUNG`, den
404-Grund und beide Leerlaufpruefungen. Er ersetzt keine atomare Annahmesperre:
Auftraggeber waehrend der Umstellung pausiert halten. Bei einem Verbindungsabbruch
nach dem Stop muss der Betriebszustand vor einem weiteren Versuch geprueft werden.
Nach einem Rollback auf ein Altimage ist fuer einen erneuten Wechsel wieder
dieser ausdrueckliche Einfuehrungsschritt erforderlich.

Der Job-Executor parkt Transportfehler und HTTP 502/503/504 ueber den bestehenden
Registry-Vertrag `DEPENDENCY_UNAVAILABLE_STATUS` (424). Der Watchdog uebernimmt
den gleichen Job nach 60 Sekunden erneut, hoechstens 240-mal. `/start` mit
derselben Bericht-ID uebergibt einen frischen Token an B1m: Beim Neustart werden
alte Platzprozesse beendet, abgeschlossene Schritte bleiben erhalten und nur
unterbrochene Schritte laufen erneut. Auch zwischen normalen Statusabfragen
haengt sich der Executor idempotent an, damit ein kurzer, unbemerkter Neustart
nicht auf den Token warten bleibt. Fachliche Abbrueche, ungueltige Antworten
und die Gesamtfrist bleiben endgueltige Fehler.

Ein normaler Gesamtdeploy ueberspringt die Gruppe, wenn alle fuenf Container
gesund sind und ihre tatsaechlichen Image-Commits fuer die Erkunder-Eingaben
mit dem Zielstand uebereinstimmen. Ein SSH-Aufruf prueft diese Voraussetzung;
es gibt dann keinen Imagebau, Neustart oder Health-Wartezyklus. Der Vergleich
umfasst Code, SDK-Parser, Abhaengigkeiten, Docker-/Proxy-Konfiguration und
Compose. Compose-Aenderungen loesen konservativ einen Deploy aus, auch wenn
nur ein anderer Dienst betroffen ist. Unbekannte Labels, fehlende Git-Objekte
oder Container und ungesunde Dienste erlauben kein Ueberspringen. Neue externe
Importe der Erkunder-Daemons muessen im Dockerfile und im Vergleich enthalten
sein; das Image kopiert bewusst nur diese Laufzeitquellen.

Die Gesundheitsprobe prueft nach abgeschlossenem Lifespan eine authentifizierte
HTTP-Anfrage auf eine nicht vorhandene Route (erwartet 404, keine Mutation).
Beim Proxy prueft sie einen lokal verweigerten CONNECT (403, ohne externen
Netzverkehr). Das ist Dienstbereitschaft, kein Modell-/Bericht-End-to-End-Test.
Rollback prueft aeltere Images ohne Docker-Healthcheck mit derselben Probe aus
dem Deploy-Werkzeug. Ein fehlgeschlagener oder nur teilweise gestarteter
Erkunder-Release wird als ganze Gruppe zurueckgerollt; Fehler bleiben laut.
