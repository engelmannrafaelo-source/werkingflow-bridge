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

Die vermittelte Dateisandbox hält unveränderliches Eingangsmaterial getrennt von
beschreibbaren, ausdrücklich deklarierten Ausgaben. Sie bietet `Read`, `Write`
und `List` mit leerem Konfigurationsobjekt an. Kein Modell erhält Zugriff auf
Host-Dateien, Prozesse, Netzwerk oder Credentials. Weitere Werkzeuge, insbesondere
Bash/Python, benötigen einen gesonderten isolierten Adapter und werden hier
abgelehnt. Das ist eine bewusst begrenzte technische Fähigkeit, keine Fachregel.

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

`tests/agentischer_lauf/` nutzt ausschließlich synthetische SDK-Antworten und
HTTP-/Store-Doubles. Die Tests prüfen unveränderten App-Wortlaut, Ende-Signale,
Werkzeugfreigabe, Budgetreservierung, Ausgaben, isolierte Prüfrunden,
Materialintegrität, Idempotenz und den Fehlertransport durch die Speicherung.
Der SHA-256-Test des Legacy-Promptmoduls belegt dessen unveränderte Fassung.
Es werden keine Prompts nach Energy verschoben; das gehört zum getrennten App-Auftrag.

Die Integration in `main.py` beschränkt sich auf zwei Dispatch-Dekoratoren.
Research-Protokoll, Scholarly und der Legacy-Research-Funktionskörper bleiben
unverändert. Beim Merge mit parallelen Research-Arbeiten diese Dekoratoren und
die ResearchResponse-Erweiterungen erhalten. Die neue JSON-Schema-Abhängigkeit
wird ausdrücklich in `pyproject.toml` geführt.
