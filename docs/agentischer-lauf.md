# Allgemeiner agentischer Lauf

Der versionierte Auftrag `agentischer-lauf/1` ergänzt `POST /v1/research` und
`POST /v1/jobs` mit `kind: research`. Die App liefert den unveränderten Auftrag,
Systemtext, Material, technische Ausgabeform und gegebenenfalls einen unabhängigen
Prüfauftrag. Die Bridge wertet kein Fachurteil aus. Legacy-Aufrufe verwenden den
bestehenden Handler; die Erkunder-Fachschicht bleibt bis zum Umstieg erhalten.

## Ausführungsgrenze

`src/agentischer_lauf/adapter.py` beschreibt den Providervertrag: eine garantierte
Obergrenze für Eingabe-, Cache-Read- und Cache-Creation-Tokens vor jedem Aufruf,
ein tatsächlich durchgesetztes Ausgabelimit sowie Werkzeuge ausschließlich als
Anfragen an die Bridge. Eine nachträgliche Tokenmessung erfüllt diesen Vertrag
nicht. Die bestehende CLI wird deshalb nicht automatisch registriert.

Die Serverkomposition bindet `http.configure(runtime, admit)`. `admit` muss den
verifizierten Tenantumfang liefern und sämtliche bestehenden Modell-, Provider-,
Konten-, Datenschutz-, Kapazitäts-, Budget- und Abrechnungsregeln für beide
Teilaufträge durchsetzen. Abrufe übergeben `body=None`; dabei ist weiterhin die
Identität zu prüfen, ohne Modellaufruf oder neue Abrechnung. Eine App kann weder
Adapter noch Zulassungsfunktion auswählen. Ohne beide Bindungen wird der Modus
mit HTTP 422 und `faehigkeit_nicht_verfuegbar` abgelehnt.

**Offene Implementierungsgrenze:** Dieser Zweig liefert keinen produktiven
Provideradapter und keine produktive `admit`-Bindung. Er ist ein offline geprüfter
Laufkern samt Transport, kein nutzbarer Ersatz für den DS3-Erkunder. Die aktuellen
CLI-/Research-Adapter dürfen nicht durch ein bloßes Fähigkeitsflag angemeldet
werden. Ihr gesamtes Tokenbudget ist bisher nicht vorab hart durchsetzbar.

## Sandbox und Material

Die vermittelte Sandbox hält unveränderliches Eingangsmaterial getrennt von
beschreibbaren, ausdrücklich deklarierten Ausgaben. `Read`, `Write`, `List` und
`Bash` akzeptieren jeweils ein leeres Konfigurationsobjekt (`parameter: {}`).
Unbekannte Werkzeuge oder Werkzeugkonfigurationen werden vor dem Modellaufruf
mit HTTP 422 abgewiesen. Jeder Teilauftrag erhält genau seine eigene Werkzeugliste;
die Prüfung erbt keine Freigaben vom Hauptlauf.

Ausgabeziele stehen in `ausgabe.artefakte` und optional in
`ausgabe.artefakte_optional` (Vorgabe: leer). Beide sind beschreibbar und kommen
mit Herkunft (`sha256`, `bytes`, `ref`) zurück. Fehlt unter einem Ziel aus
`artefakte` jede Datei, endet der Lauf mit `antwort_fehlt`; fehlt sie unter
einem optionalen Ziel, wird das Ziel ausgelassen. Ein optionales Ziel darf ein
Ziel aus `artefakte` oder `datei` weder gleichen noch enthalten noch in ihm
liegen. Dasselbe gilt für `pruefung.ausgabe`.

`Bash` hat zwei Ausführungsorte, beide serverseitig gewählt:

- **CLI-Platz** (ausgeliefert): Läuft die Phase über einen CLI-Adapter, führt
  die CLI Bash selbst im Platz aus. Der Platz hat eine eigene UID, ein
  schreibgeschütztes Image und Netzausgang nur über seinen Proxy, wie der
  Erkunder heute. Zugelassen wird das nur auf einem geprüften Platz (siehe
  [agentischer-lauf-cli.md](agentischer-lauf-cli.md), Zulassung).
- **Rechenplatz** `Runtime(..., bash=BashPlace(...))`: ein frischer Container je
  Aufruf. Er ist **nicht gebunden**. Er braucht einen Docker-Daemon, den der
  Aufrufer erreicht, und das ist ohne rootless Daemon Root auf dem Host
  (ABASHR-Auflage 1). Ohne diese Bindung endet ein Bash-Auftrag an einen
  Nicht-CLI-Adapter mit HTTP 422 `faehigkeit_nicht_verfuegbar`.

Für den Rechenplatz gilt: Der Werkzeugaufruf lautet
`{"command": "python scripts/calculate.py"}`; Ergebnis sind `exit_code`, `stdout`
und `stderr`. Eine normale Rückgabe mit Exitcode ungleich null ist ein sichtbares
Befehlsergebnis für das Modell. Zeit-, Isolations- und Artefaktfehler brechen den
Lauf mit Fehlercode ab. `stdout` und `stderr` sind begrenzter UTF-8-Text;
binäre Rechenergebnisse gehören in deklarierte Artefaktdateien.

### Rechenplatz

`BashPlace` verwendet das vorhandene Rechenimage aus `Dockerfile.erkunder`,
serverseitig auf eine geprüfte lokale Image-ID festgelegt. Der Betreiber übergibt
außerdem einen privaten Ablagepfad und den lokalen Docker-Zugang. Code und
Ablagepfad müssen unter denselben Pfaden auf dem Docker-Host sichtbar sein.
Das ist eine Voraussetzung der Serverkomposition, keine vom Auftrag wählbare
Option; dieser Anschluss erteilt keinem Worker automatisch Docker-Rechte.
Bevor er gebunden wird, muss der Daemon rootless laufen oder hinter einem eigenen
Dienst liegen, der allein den Socket hält. Nie den Host-Socket in einen Worker.
Gleichzeitig laufen je Prozess höchstens `max_parallel` Rechencontainer
(Vorgabe 3). Darüber antwortet der Platz mit 429 `lauf_belegt` (wiederholbar).
Container werden mit `--rm` angelegt, damit der Daemon sie auch nach einem harten
Absturz des Aufrufers entfernt. PID 1 im Container ignoriert SIGINT. Die
CPU-Grenze je Prozess liegt eine Sekunde über der Zeitgrenze, damit immer die
Zeitgrenze greift (`grenze_zeit`).

Jeder Aufruf bekommt einen eigenen Platz mit PID-, IPC- und Netz-Namensraum,
schreibgeschütztem Root-Dateisystem, begrenzten tmpfs-Verzeichnissen, 2 GiB RAM,
128 Prozessen und einer CPU. Der Rechenplatz hat überhaupt keinen Netzausgang
(auch keinen Modell-Proxy). Das kurze Bootstrap bereitet Verzeichnisrechte vor;
vor jedem Befehl werden UID/GID und die dafür benötigten Fähigkeiten abgelegt.
Das Kind läuft unprivilegiert mit `no-new-privileges`, dem bestehenden
`protect_process`-Schutz und einer expliziten Umgebung ohne Zugangsdaten.
Die vorhandenen Prozess-Aufräumfunktionen beenden und ernten die Nachkommen;
abschließend wird der gesamte Container auch bei Abbruch entfernt.

Material wird einzeln schreibgeschützt eingebunden und nach dem Befehl auf seine
Identität geprüft. Die bisherigen Ausgaben werden für den nächsten Aufruf
übernommen. Nur reguläre Dateien unter deklarierten Ausgabezielen dürfen zurück;
Links, Spezialdateien, zu viele Dateien, übergroße Ausgaben und veränderte
Materialpfade führen zu einem Fehler. Der Zustand umfasst auch Löschungen.
Bash erlaubt das Lesen und Schreiben innerhalb dieses Rechenplatzes; eine
zusätzliche `Write`-Freigabe ist für Rechenartefakte nicht erforderlich.
Prozessumgebung, laufende Prozesse und temporäre Dateien werden nicht fortgesetzt.
Es gibt keine fachliche Festlegung auf bestimmte Skript- oder Ergebnisnamen.

Die Servergrenze pro Befehl beträgt standardmäßig 30 Sekunden, konfigurierbar bis
480 Sekunden; zusätzlich gilt die Gesamtfrist des Auftrags. Das zwingende
Aufräumen wird bei einer Fristüberschreitung noch abgewartet. Erreicht ein Befehl
die Dateigrößengrenze, kann eine begrenzte Teildatei zurückbleiben; der nicht
erfolgreiche Exitcode bleibt sichtbar.

Inline-Material wird nach SHA-256 und Bytezahl geprüft. HTTPS-Verweise benötigen
einen serverseitig gebundenen Resolver, der Ziel und jeden Redirect autorisiert,
Netzwerkregeln durchsetzt und den Download während des Lesens begrenzt. Ohne
Resolver sind Verweise nicht verfügbar. URLs fließen nicht in den Auftragsdigest
oder die Modellnachrichten ein. Es gibt keinen allgemeinen freien URL-Fetch.

## Dauerhaftigkeit und Abruf

Die vorhandene Jobverwaltung bleibt zuständig für Zustellung, Status und Deferrals.
Ein privates, gemeinsam eingebundenes Laufvolume hält nur Ausführungscheckpoints,
Diagnose und Artefakte. Sein Pfad wird bei Serverkomposition übergeben, seine
Zugriffsrechte sind 0700. Alle beteiligten Worker müssen dasselbe Volume sehen.
Ein flock pro Tenant/Lauf verhindert parallele Ausführung. Eine noch gehaltene
Sperre liefert `lauf_belegt`, HTTP 429 mit Retry. Ein freigegebener, aber nicht
abgeschlossener Checkpoint liefert `lauf_unvollstaendig`; möglicherweise bereits
berechnete Modellantworten werden nicht automatisch erneut erzeugt.

Ein bereits angenommener Auftrag behält seine Frist über Job-Deferrals hinweg.
Gleiche Lauf-ID mit geändertem Wire-Digest ergibt 409. Ein gespeicherter Fehler
mit `retryable: false` bleibt endgültig. Ein vollständig gespeicherter Fehler mit
`retryable: true` ist dagegen kein endgültiger Laufabschluss: ein erneuter POST
mit demselben Auftrag und derselben `lauf_id` beginnt einen neuen Versuch unter
derselben Tenant/Lauf-Sperre. GET (Job, Diagnose oder Artefakt) führt nichts aus;
ein terminaler Job wechselt dadurch nicht nachträglich auf Erfolg.

Jeder neue Versuch bekommt ein frisches Zeit- und Tokenbudget aus demselben
Auftrag. Wiederholte Reservierung eines bereits angenommenen Versuchs verlängert
seine Frist nicht. Ein unvollständiger Checkpoint wird weiterhin niemals neu
ausgeführt, auch wenn ein darin vermerkter Teilfehler retrybar war. Die gesamte
Ausführung einschließlich einer optionalen Prüfung startet frisch; weder
Teilantworten noch Teilsandboxes werden als erfolgreich übernommen. Der Aufrufer
beachtet `retry_after_s` und begrenzt Zahl und Gesamtkosten seiner Wiederholungen.
Die Bridge startet keinen Hintergrund-Retry und verspricht für retrybare Fehler
keine Exactly-once-Ausführung.

Vor Beginn archiviert die Bridge den alten Checkpoint samt Usage, Diagnose und
Artefaktbytes unter `attempts/<interne Nummer>/state.json` im geschützten Laufvolume.
Diese Nummer ist kein Wire-Feld und verändert den Digest nicht. Ein Archivfehler
verhindert den neuen Versuch. Öffentliche Laufreferenzen zeigen auf den aktuellen
Versuch; frühere Daten bleiben intern für Diagnose und Abrechnung erhalten.

Diese Variante erhält den bisherigen Wire-Vertrag und die deterministische
Lauf-ID. Ein zusätzliches verpflichtendes Versuchsfeld würde dagegen jeden
Aufrufer und dessen Digest-Bildung ändern. Energy benötigt nur eine additive
Transportänderung: nach einem terminalen Jobfehler mit `retryable: true` (und der
Wartefrist) einen neuen `POST /v1/jobs` mit identischer Payload senden, die neue
`job_id` speichern und diese abfragen. Der alte Job bleibt Fehler; bloßes GET auf
dessen ID löst keinen Versuch aus. Energy behält Bahn, Fachablauf und Digest.
Für einen abgeschlossenen, nicht retrybaren Lauf ist eine neue Lauf-ID nötig.
 Wiederverwendeter Erfolg verursacht keine erneuten Nutzungskosten;
die ursprünglichen Nutzungsdaten bleiben in der geschützten Diagnose.

Artefakte werden vollständig unter der bestehenden authentifizierten
`/v1/research/{lauf_id}/content?lauf_artifact=...`-Route abgerufen. Referenzen tragen
keine Hostpfade oder Material-URLs. Die Diagnose enthält den vollständigen
Werkzeugverlauf; allgemeine Jobfehler enthalten nur Code, Ende, Retry, Usage und
Diagnosereferenz. Nach Beginn erstellte Artefakte bleiben auch bei einem späteren
technischen Fehler abrufbar. Speicheraufbewahrung/Cleanup muss vor Aktivierung
noch an die Betriebsregeln des allgemeinen Laufvolumes angeschlossen werden.

## Prüfung und Integration

`tests/agentischer_lauf/` nutzt synthetische SDK-Antworten und HTTP-/Store-Doubles.
Die Bash-Tests führen echte, kurzlebige Rechencontainer ohne Modellaufrufe aus;
`ABASH_TEST_IMAGE` muss eine lokale, geprüfte Image-ID benennen. Sie prüfen auch
Netz-/Pfadausbrüche, Prozesserschöpfung, Abbruch und binäre Artefakte. Smoke-Suiten
mit globalen SDK-Mocks laufen in einem eigenen pytest-Prozess.
Die Laufkerntests prüfen unveränderten App-Wortlaut, Ende-Signale,
Werkzeugfreigabe, Budgetreservierung, Ausgaben, isolierte Prüfrunden,
Materialintegrität, Idempotenz und den Fehlertransport durch die Speicherung.
Der SHA-256-Test des Legacy-Promptmoduls belegt dessen unveränderte Fassung.
Es werden keine Prompts nach Energy verschoben; das gehört zum getrennten App-Auftrag.

Die Integration in `main.py` beschränkt sich auf zwei Dispatch-Dekoratoren.
Research-Protokoll, Scholarly und der Legacy-Research-Funktionskörper bleiben
unverändert. Beim Merge mit parallelen Research-Arbeiten diese Dekoratoren und
die ResearchResponse-Erweiterungen erhalten. Die neue JSON-Schema-Abhängigkeit
wird ausdrücklich in `pyproject.toml` geführt.

## CXA-Wire-Digest (normativer Vertrag, A3)

`herkunft.auftrag_sha256` ist SHA-256 über das kanonische UTF-8-JSON der sechs
Felder `query`, `model`, `max_turns`, `max_tokens`, `antwort_im_text` und
`agentischer_lauf` des validierten ResearchRequest. Das vollständige Laufobjekt
enthält **lauf_id**, den Schema-Alias `schema` und sämtliche Modell-Defaults:
`systemtext: ""`, `pruefung: null` sowie nicht genutzte Ausgabefelder
`datei: null` und `json_schema: null`. Bei vorhandener Prüfung gilt dasselbe
für deren Systemtext und Ausgabe. Einzige Ausnahme von den Defaults:
`artefakte_optional` steht nur im Digest, wenn die Liste nicht leer ist; leer
entfällt der Schlüssel, in `ausgabe` wie in `pruefung.ausgabe`. So bleibt
jeder Digest ohne optionale Ziele unverändert. Entfernt wird außerdem nur `quelle` aus
jedem Materialeintrag, einschließlich Inline-Bytes oder URL; Identität, Ziel,
Bytezahl und Material-SHA bleiben enthalten. `async_mode` und alle übrigen
äußeren Research-Felder gehören nicht in den Digest. Die sechs äußeren Felder
sind für diesen Modus ausdrücklich anzugeben, es gibt keine Modellwahl per Default.

Objektschlüssel werden rekursiv sortiert, Listen behalten ihre Reihenfolge.
Separatoren sind Komma und Doppelpunkt ohne Leerzeichen; Unicode wird nicht
ASCII-escaped, NaN/Infinity sind unzulässig. Es gibt keine zusätzliche
Unicode-Normalisierung und keinen abschließenden Zeilenumbruch. SHA-256 wird
als 64-stelliger Hexwert in Kleinbuchstaben übertragen.

Dies präzisiert die CX0-Inhaltsdigest-Formulierung: der hier festgelegte
**Wire-Digest enthält lauf_id**. Ein von der App zur Erzeugung der Lauf-ID
verwendeter Inhaltsdigest ist davon getrennt und kein Herkunfts-Wire-Digest.
Der folgende normative Vektor wird direkt aus diesem Vertrag durch
`tests/agentischer_lauf/test_retry_contract.py` gelesen und mit dem Laufcode verglichen.

```json wire-digest-contract
{
  "fields": [
    "query",
    "model",
    "max_turns",
    "max_tokens",
    "antwort_im_text",
    "agentischer_lauf"
  ],
  "encoding": "utf-8",
  "json_options": {
    "sort_keys": true,
    "separators": [
      ",",
      ":"
    ],
    "ensure_ascii": false,
    "allow_nan": false
  },
  "vector": {
    "request": {
      "query": "Sortiere Bücher: α",
      "model": "synthetic",
      "max_turns": 2,
      "max_tokens": 40,
      "antwort_im_text": true,
      "async_mode": false,
      "agentischer_lauf": {
        "schema": "agentischer-lauf/1",
        "lauf_id": "digest-example",
        "material": [
          {
            "id": "book",
            "ziel": "input.txt",
            "bytes": 1,
            "sha256": "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",
            "quelle": {
              "art": "inline",
              "content_base64": "eA=="
            }
          }
        ],
        "werkzeuge": [],
        "ausgabe": {
          "modus": "text",
          "artefakte": []
        },
        "grenzen": {
          "zeit_s": 20,
          "tokens_gesamt": 500
        }
      }
    },
    "normalized": {
      "query": "Sortiere Bücher: α",
      "model": "synthetic",
      "max_turns": 2,
      "max_tokens": 40,
      "antwort_im_text": true,
      "agentischer_lauf": {
        "schema": "agentischer-lauf/1",
        "lauf_id": "digest-example",
        "material": [
          {
            "id": "book",
            "ziel": "input.txt",
            "bytes": 1,
            "sha256": "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"
          }
        ],
        "werkzeuge": [],
        "ausgabe": {
          "modus": "text",
          "artefakte": [],
          "datei": null,
          "json_schema": null
        },
        "grenzen": {
          "zeit_s": 20,
          "tokens_gesamt": 500
        },
        "systemtext": "",
        "pruefung": null
      }
    },
    "sha256": "0b9bf21a181708a7d028a74f8644b3d03870d00c427b9ea5d82397896333784f"
  }
}
```

Zweiter normativer Vektor mit nicht-leerer `artefakte_optional` im Hauptlauf und
leerer (also entfallender) in der Prüfung; gelesen von demselben Test:

```json wire-digest-vector-optional
{
  "request": {
    "query": "Sortiere Bücher: α",
    "model": "synthetic",
    "max_turns": 2,
    "max_tokens": 40,
    "antwort_im_text": false,
    "async_mode": false,
    "agentischer_lauf": {
      "schema": "agentischer-lauf/1",
      "lauf_id": "digest-example-optional",
      "material": [
        {
          "id": "book",
          "ziel": "input.txt",
          "bytes": 1,
          "sha256": "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",
          "quelle": {
            "art": "inline",
            "content_base64": "eA=="
          }
        }
      ],
      "werkzeuge": [],
      "ausgabe": {
        "modus": "datei",
        "datei": "bericht.md",
        "artefakte": [
          "daten"
        ],
        "artefakte_optional": [
          "skripte",
          "antwort-pruefer.md"
        ]
      },
      "pruefung": {
        "auftrag": "Prüfe den Bericht",
        "model": "synthetic",
        "werkzeuge": [],
        "ausgabe": {
          "modus": "text",
          "artefakte": []
        },
        "max_turns": 1,
        "max_tokens": 20
      },
      "grenzen": {
        "zeit_s": 20,
        "tokens_gesamt": 500
      }
    }
  },
  "normalized": {
    "query": "Sortiere Bücher: α",
    "model": "synthetic",
    "max_turns": 2,
    "max_tokens": 40,
    "antwort_im_text": false,
    "agentischer_lauf": {
      "schema": "agentischer-lauf/1",
      "lauf_id": "digest-example-optional",
      "systemtext": "",
      "material": [
        {
          "id": "book",
          "ziel": "input.txt",
          "sha256": "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",
          "bytes": 1
        }
      ],
      "werkzeuge": [],
      "ausgabe": {
        "modus": "datei",
        "datei": "bericht.md",
        "json_schema": null,
        "artefakte": [
          "daten"
        ],
        "artefakte_optional": [
          "skripte",
          "antwort-pruefer.md"
        ]
      },
      "pruefung": {
        "auftrag": "Prüfe den Bericht",
        "systemtext": "",
        "model": "synthetic",
        "werkzeuge": [],
        "ausgabe": {
          "modus": "text",
          "datei": null,
          "json_schema": null,
          "artefakte": []
        },
        "max_turns": 1,
        "max_tokens": 20
      },
      "grenzen": {
        "zeit_s": 20,
        "tokens_gesamt": 500
      }
    }
  },
  "sha256": "87368e8c7ea0deeffb4c35221e83f0cc7510d171093fd7ae198b4cd20c138950"
}
```

## Zusammenspiel mit CX3

`ResearchRequest` führt `research_instructions`, `privacy_allowlist` und
`agentischer_lauf` hinter dem Klassen-Docstring. Die ersten beiden Felder sind
CX3-Felder und bei gesetztem Laufobjekt auch mit null/leeren Werten unzulässig.
`agentischer_lauf: null` gilt wie ein fehlendes Feld und benutzt den Legacy-Weg,
auch nach `model_dump → model_validate` und bei der Job-Weitergabe. Die Nutzung
der CX3-Instruktionen auf dem Rechercheweg setzt weiterhin den CX3-Handler voraus;
dieser Zweig übernimmt nur deren Schema zur konfliktarmen Zusammenführung.
