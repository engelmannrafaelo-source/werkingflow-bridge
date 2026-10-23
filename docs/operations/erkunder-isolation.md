# Erkunder: Schrittgrenzen, Ergebnisintegrität und Wiederanlauf

Fehlgeschlagene Schrittversuche werden vor dem Aufräumen zusätzlich unter
`/arbeit/.diagnosen/<bericht_id>.<lauf_start>.json` gesichert (Verzeichnis 0700, Datei 0600).
Das Archiv liegt außerhalb des zu löschenden Berichts, wird atomar geschrieben,
mit fsync gesichert und zurückgelesen. Nur eine fehlgeschlagene Archivierung
verweigert das Aufräumen mit 503. Fehlende Platzquittungen (auch 404 nach
Neustart), laufende Plätze und Abbruchfehler werden als unbestätigte Versuche
archiviert; anschließend werden Bericht und Eingang gelöscht. Abbruchfehler
stehen weiter in `platz_abbruch_fehler`. Ein Ordner ohne Laufdiagnose erhält
einen Minimalbeleg `zustand=ohne_laufdiagnose`. Neue Starts verlangen weiterhin
den bestätigten `stop_places()`-Durchlauf. Laufende Aufgaben werden dabei
als abgebrochen markiert, damit ein Neustart sie nicht wieder aufnimmt.

`GET /diagnose/<id>` liefert das Archiv mit derselben internen Authentisierung
wie die anderen Leitstand-Wege; es bleibt nach erfolgreichem Aufräumen erhalten.
Jeder neue Lauf erhält eine eigene persistierte Nanosekunden-Startkennung.
Der Abruf liefert den neuesten Lauf und unter `aeltere_laeufe` die älteren;
ein Neulauf derselben bericht_id überschreibt keinen früheren Lauf. Auch
vorhandene Archive im alten Dateinamen bleiben lesbar.
Status und Logs enthalten ebenfalls Schrittname, Versuch, Abbruchkategorie und
soweit verfügbar Fehlerklasse, Exitcode und eine klassifizierte technische
stderr-Zeile. Freie SDK-Texte, Prompts, Kundendaten und Tokens werden nicht
übernommen. Es gibt keine automatische Löschung dieses Diagnosearchivs.
Die Worker-Erweiterung überträgt diese Angaben in den Fortschritt. StepFailed,
Leitstand-Fehler und `erkunder abbruch: <schritt>: <grund>` bleiben kurze
klassifizierte Gründe ohne angehängte Zusammenfassung; damit bleibt der alte
Worker beim Erkunder-only-Rollout kompatibel. Einzelgründe stehen ausschließlich
in `schritt_diagnosen`, Archiv und Logs. Ein reiner Erkunder-Rollout aktualisiert
keinen Worker.

Der Proxy hält sein begrenztes Log in `/tmp/erkunder-proxy/tinyproxy.log`.
Das Verzeichnis gehört root und ist nicht sticky; die vorhandene Logdatei
gehört nobody. So kann tinyproxy schreiben und der root-Trimmer kürzen, ohne
am `fs.protected_regular`-Schutz des übergeordneten `/tmp` zu scheitern.

Jeder Platz verwendet einen privaten Docker-IPC- und PID-Namespace sowie eine
feste unprivilegierte UID. `ipc: private` trennt Plätze voneinander; es allein
trennt **keine aufeinanderfolgenden Schritte**. Nach jedem Schritt beendet und
erntet `Platz.run()` zunächst alle Schrittprozesse. Sein `finally` entfernt danach
UID-eigene Dateien in `/tmp`, `/dev/shm` und `/dev/mqueue` sowie SysV-Segmente,
Nachrichtenwarteschlangen und Semaphore der UID (Eigentümer oder Ersteller).
Jeder Bereinigungsfehler sperrt weitere Schritte und `/abbrechen` mit 503.
Die Inventarfunktionen dürfen nur im dedizierten Platzcontainer laufen.

Diese Kombination benötigt keine zusätzlichen Capabilities. Ein neuer IPC- und
Mount-Namespace pro Schritt würde hier einen privilegierten Launcher oder neue
User-Namespace-Berechtigungen voraussetzen. `ipc: none` mit fehlendem Shared
Memory würde Rechenbibliotheken unnötig einschränken und ersetzt die Bereinigung
von persistenten IPC-Objekten nicht. Private Namespaces plus zwingende Ernte und
Bereinigung erlauben Shared Memory innerhalb eines Schritts. Containerproben
prüfen zwei echte Schritte verschiedener Berichte derselben UID; Node-Threads und
`claude --version` prüfen den installierten Runtime-/CLI-Start ohne Modellaufruf.

Der Leitstand hält nach jedem erfolgreichen Schritt den SHA-256 des Ergebnistexts
in seiner nur für root lesbaren `.lauf.json`. Jeder spätere Zugriff prüft denselben
Hash: Übergabe an Harmonisierung/Prüfung, Abschluss nach dem Prüfschritt und Abruf.
Der Abruf liefert genau den bereits geprüften gelesenen String, ohne zweite
Dateilesung. Abweichungen oder fehlende Hashes bei alten erfolgreichen Schritten
führen zum sichtbaren Integritätsfehler; `/ergebnis/<id>` liefert dann 409.
Alte Berichte ohne Hash müssen neu gerechnet werden. Das Metadaten-API-Schema
bleibt unverändert. Skripte behalten ihre bisherige separate Abrufbehandlung;
die Hashbindung betrifft die fachlichen Ergebnis- und Prüfungstexte.

Beim Start sperrt der Leitstand zuerst alle Altberichte. Er wartet dann insgesamt
höchstens 120 Sekunden auf bestätigte Bereinigung aller Plätze. Die Frist umfasst
auch HTTP-Wartezeit; sie erlaubt verzögertes Compose-Starten, ohne unbegrenzt zu
hängen. Danach bleibt der Prozess erreichbar, aber Vergaben und die authentisierte
`GET /bereitschaft` liefern 503. Während dieser Sperre antworten auch alle
anderen authentisierten Wege außer Aufräumen mit 503; eine Probe über einen
unbekannten Pfad kann damit nicht fälschlich Bereitschaft melden.
Er stürzt dafür nicht ab und erzeugt keine
Neustartschleife. Eine erfolgreiche Anfrage an `POST /aufraeumen/<id>` entfernt
den angegebenen Altbericht und versucht die Initialisierung erneut, falls der
Leitstand noch gesperrt ist. HTTP-Authentisierung: `X-Erkunder-Intern` aus der
internen Dienstkonfiguration. Ein Berichtpfad-/Rechtefehler bleibt ein harter
Startfehler; er wird nicht als verzögerter Platzstart behandelt.

Laufende Altberichte warten nach erfolgreichem Start höchstens 300 Sekunden auf
erneutes Anhängen mit frischem Token. Fünf Minuten lassen dem Worker Zeit für
Reconnect/Retry, ohne die Plätze für die gesamte sechs Stunden lange
Ergebnisaufbewahrung zu blockieren. Nach Fristablauf wird der Bericht mit
`Wiederanhaengen: Zeitgrenze erreicht` abgebrochen und bleibt root-only erhalten;
ein neuer Bericht kann starten. Früher freigeben/löschen:
`POST /aufraeumen/<id>`. Die Frist betrifft nur Wiederaufnahme, nicht aktive
Modellschritte und nicht die Ergebnisaufbewahrung.

Reproduzierbare Proben mit einem aus dem zu prüfenden Commit gebauten Image:

- `scripts/test-erkunder-ipc.sh <image>`
- `scripts/test-erkunder-integrity.sh <image>`
- `scripts/test-erkunder-start.sh <image>`

Alle Proben verwenden Wegwerfcontainer und synthetische Daten. Die Startprobe
verwendet ein eigenes internes Docker-Netz ohne veröffentlichte Ports.


## SDK-Parser im schlanken Erkunder-Image

Der gemeinsame SDK-Parser darf im Erkunder keine Worker-/Jobstore-Module
voraussetzen. Das Kind installiert ihn ausdrücklich mit
`track_job_usage=False`; der Worker behält standardmäßig seine bestehende
Teilverbrauchsbuchung. Das Image enthält absichtlich kein `src.jobs`.

Diagnosen lösen SDK-ExceptionGroups bis zur klassifizierbaren Blatt-Ausnahme
auf. Fehlende Python-Abhängigkeiten werden als `ModuleNotFoundError` bzw.
`ImportError` mit `fehlercode=dependency_missing` sichtbar. SDK-Importe liegen
innerhalb der geschützten Kind-Ausführung. Auth-, Modell-, Proxy-/Netz-,
Rechte- und CLI-Startsignaturen werden nur als feste Codes übernommen;
HTTP404 allein beweist kein nicht verfügbares Modell. Fehlt die Diagnosehülle
bei einem Prozessfehler ganz, lautet der Code `child_diagnostic_missing` mit
Exitcode; ein unbekannter Fehler wird nicht als Auth-/Proxyfehler ausgegeben.
Es werden weder rohe stderr-Zeilen noch Modulnamen, Settings, HOME-Inhalte
oder Umgebungswerte archiviert. Die vorhandene Informationsgrenze bleibt bestehen.

Nach dem gemeinsamen Erkunder-Imagebau führt der reguläre Deployweg
`src.erkunder.imagecheck` im gebauten Image aus. Das Werkzeug löst den neuen
Commit-Tag zur unveränderlichen Image-ID auf. Ein Hilfscontainer ohne Netz,
ohne Kundenvolume, mit read-only-Dateisystem und UID/GID 1101:1100 importiert
SDK-Parser, Kind, Platz und Leitstand und verarbeitet synthetische System-,
Assistant-, Result- und unbekannte SDK-Nachrichten über den installierten
Kind-Parser. Es startet weder CLI noch Modell. Diese Architekturprobe prüft
auch lazy Imports; ein bloßer Modulimport reicht dafür nicht.

Jeder Fehler beendet `prepare_erkunder_deploy` mit Exitcode ungleich null,
bevor Annahmesperre oder Recreate beginnen. Der Aufrufer in
`scripts/bridge-deploy.sh` bricht den Deploy ab. Der Check ist ein Laufzeit-
Image-Gate, kein neuer Unit-Test und kein fachlicher Gutachten-Smoke.


Der Platz leert stderr parallel zu stdin/stdout über eine eigene Pipe. Nur die
letzten maximal 8192 Bytes bleiben flüchtig zur Klassifizierung erhalten;
zusätzlich wird jeweils ein 4096-Byte-Leseblock verarbeitet. Dauerhaft bleibt
nur die Whitelist-Fehlerklasse/der feste Fehlercode. Es gibt keine Rohtextdatei,
kein stderr im Rückgabewert und keine Ausgabe von Credential-/Umgebungswerten.
Die bereinigte JSON-Diagnose des Kindes hat bei gleichen Feldern Vorrang.

Auch bei Zeit-, Speicher- oder angefordertem Abbruch übernimmt der Platz nach
dem Beenden des Kindes die abschließende, bereits klassifizierte stderr-Diagnose.
Der Abbruchgrund des Supervisors bleibt dabei erhalten. Die Diagnose enthält
weiterhin nur feste Klassen; ein fehlender Eintrag beweist keinen fehlerfreien
Modellfortschritt. Die bisherigen Archive enthalten keine SDK-Zwischenereignisse
und erlauben deshalb keine nachträgliche Unterscheidung von Thinking, Wartezeit
und Werkzeugausführung.
