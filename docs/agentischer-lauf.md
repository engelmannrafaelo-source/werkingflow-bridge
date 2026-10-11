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

`Bash` wird ausdrücklich serverseitig über `Runtime(..., bash=BashPlace(...))`
gebunden. Ohne diese Bindung bleibt ein Bash-Auftrag bei HTTP 422
`faehigkeit_nicht_verfuegbar`. Der Werkzeugaufruf lautet
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
Gleiche Lauf-ID mit geändertem Inhaltsdigest ergibt 409. Ein gespeicherter Fehler
bleibt Fehler. Wiederverwendeter Erfolg verursacht keine erneuten Nutzungskosten;
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
