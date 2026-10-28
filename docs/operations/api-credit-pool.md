# Gemeinsames Anthropic-API-Guthaben

Rafaels Auftrag vom 28.10.2026: die vier verknüpften Max-API-Guthaben auf beiden
Bridges nutzen, insbesondere für Vision. Die vorhandenen OAuth-Konten bleiben
unabhängig davon. Der aktivierte Pool setzt den normalen Vision-Standard auf
Anthropic; ein ausdrücklich angefragter Gemini- oder Bedrock-Weg bleibt erhalten.

## Aufbau

Eine einzige platform-api auf der Primary-Bridge verwaltet die Reservierungen
in `anthropic_credit_reservations` (Migration 073). Alle Dev- und Prod-Worker
verwenden dieselbe `ANTHROPIC_CREDIT_POOL_URL` und einen eigenen
`ANTHROPIC_CREDIT_POOL_TOKEN`. Die internen Routen sind nicht über nginx
veröffentlicht. `/accounts` gibt Schlüssel nur nach Prüfung dieses Tokens aus;
`/status` enthält ausschließlich Budget- und Herkunftsdaten, keine Schlüssel.
Der gemeinsame GPU-Privacy-Dienst kann denselben Client benutzen, sobald
der Koordinator aus seinem Netz erreichbar ist. Auf dem Primary-Host
verwenden die Container die Docker-Adresse http://platform-api:8000; entfernte
Clients verwenden die Tailnet-Adresse. Die eigene Tailnet-Publikation ist aus
den Primary-Containern nicht per Hairpin erreichbar.

Echte Bildinhalte auf /v1/chat/completions werden im nginx-Router anhand des
Bodies erkannt und brauchen keine freie Abo-Kapazität. Im Worker überspringt
ein aktiver Guthaben-Pool für Bildanfragen Abo-Zulassung, Abo-Login und
Abo-Kontosperren. API-Authentifizierung, Nutzer-Pins und Budgetregeln bleiben
wirksam. Reine Textanfragen erhalten diese Ausnahme nicht.

Vor jeder Text-/Bildanfrage zählt Anthropic die Eingabetokens. Die platform-api
reserviert unter einer PostgreSQL-Transaktionssperre pro Gutschrift die obere
Preisgrenze für Eingabe, Cache-Schreiben und maximale Ausgabe, plus 10 Prozent.
Antworten buchen die gemeldete Usage konservativ ab. Bestätigte 4xx-Absagen
geben die Reservierung frei; eine verlorene Antwort, 5xx oder unvollständige
Usage lassen den vollen Betrag stehen. Unklare Reservierungen werden niemals
nach einer Frist automatisch freigegeben. Wiederholte Settlement-Aufrufe sind
idempotent. Kosten und Eingabetokenzahlen, aber keine Prompts oder Bilder,
werden zentral gespeichert.

Konten werden nach Ablaufzeit der tatsächlichen Promotional-Credit-Tranche
sortiert. Pro Tranche bleiben 2 USD Reserve. Zusätzlich begrenzt der zuletzt
aus der Console gemessene Restbetrag die Vergabe; Kosten seit fünf Minuten vor
dieser Messung und ungeklärte Reservierungen werden nochmals abgezogen.
Snapshot älter als eine Stunde, unbekannter Modellpreis oder Ablauf in weniger
als einer Stunde: keine Freigabe. Ein aktiver Pool fällt nie auf den bisherigen
bezahlten Vision-Schlüssel zurück. Die bestehende tägliche Vision-Kappe bleibt
zusätzlich bestehen. Die Schlüssel sind keine Admin-Schlüssel.

## Aktualisierung und Betrieb

`scripts/install-api-credit-pool.py` läuft auf Dev mit einer privaten
`--source`-Datei. Es installiert die Client-Secrets, das Refresh-Werkzeug in
`/root/.local/share/bridge-api-credit-pool`, dessen isolierte Python-Umgebung und
`/etc/cron.d/bridge-api-credit-pool`. Es startet keine Container neu.
Aktivierung ausschließlich per `scripts/bridge-deploy.sh` aus dem Commit.

Alle zehn Minuten prüft `refresh-api-credit-pool.py` Console-Identität und
Promotional-Credit-Tranchen. Es übernimmt nur USD-Gutschriften des Programms
`subscription_api_credit`, die für API-Aufrufe gelten. Neue monatliche
Tranche-IDs werden automatisch aufgenommen; verbrauchte alte Budgets werden
nicht zurückgesetzt. Die Veröffentlichung des Manifests unter
`/root/werkingflow-bridge/secrets/api-credit-pool.json` erfolgt atomar per SSH.
Console-Sitzungen werden nur im privaten Quellfile auf Dev gehalten. Wenn sie
ablaufen, muss der Betreiber sie aus einer neu angemeldeten Console erneuern.
Ein fehlgeschlagenes Refresh erneuert niemals den alten Messzeitpunkt.

Die Console bleibt für den tatsächlichen Geldbetrag maßgeblich. Die Bridge
rechnet bewusst höher als eine mögliche Cache-/Haiku-Ermäßigung. Nutzung durch
fremde API-Schlüssel derselben Organisation kann zwischen Console-Ablesungen
auftreten: die Bridge kann solche fremden Aufrufe nicht reservieren. Daher
keine automatischen Nachkäufe aktivieren und Guthaben möglichst exklusiv für
diesen Pool nutzen. Bestehende Zahlungs-/Nachladeeinstellungen werden vom
Installer nicht geändert.

## Prüfung und Rückweg

`CREDIT_POOL_TEST_DSN` muss auf eine Wegwerf-PostgreSQL-Instanz zeigen.
`pytest tests/test_api_credit_pool.py` prüft echte parallele Transaktionen,
Idempotenz, Ablauf, externe Reststände, Tokenzählung, Unsicherheit und fehlenden
bezahlten Fallback. Die Vision-Regressionssuite zusätzlich ausführen.
Live: ein kleiner Text-/Bildaufruf je Schlüssel, Vision-Aufrufe durch beide
Bridges und ein Statusabruf beim zentralen Koordinator. Der Ledger muss dabei
settled-Zeilen und die richtigen Organisationen zeigen.

Rückweg: alten geprüften Commit über das Deploy-Werkzeug ausrollen. Die alten
Schlüssel wurden nicht ersetzt; alter Code ignoriert die Pool-Variablen. Für
eine bewusste Rückkehr zum alten Pfad auf neuem Code Pool-URL entfernen und
regulär deployen. Reservierungstabelle und alte Guthabenbelege behalten.

## Verifizierter Rollout am 28.10.2026

Dev läuft auf `3806dc5` (Branch `codex/api-credit-pool-runtime-20261028`): vier
Worker und der Koordinator aktiv, 15/15 Smoke-Probes bestanden. Echte Bildaufrufe
mit Office und Werking sind im gemeinsamen Ledger abgerechnet. Der öffentliche
Dev-Test mit der bestehenden Hop-Markierung liefert `api_vision_bypass` und
HTTP 200. Ohne diese gezielte Dev-Routenwahl geht Standardverkehr gemäß der
bestehenden Topologie zuerst zur Produktions-Bridge; deren Aktivierung fehlt
noch. Die internen Schlüsselrouten antworten öffentlich mit 404.

Produktion: Secrets vorbereitet, Koordinator-Netz erreichbar, Laufzeit noch
`dcba6ba`. Der reguläre Dev-first-Rollout umfasst ungefähr 50 weitere Commits
(Budget, Auth, Jobs); Rafaels Entscheidung dazu ist ausstehend. Keine aktive
Guthaben-Nutzung durch die alten Prod-Worker behaupten.

GPU: Client-Code `5a2bd5d` vorhanden, Pool wieder deaktiviert, weil der GPU-Host
den Primary-Koordinator nicht erreichen kann. Installation mit
`--disable-gpu-pool` erhält den bisherigen Provider. Erst Netzfreigabe prüfen,
dann diesen Schalter weglassen und regulär deployen. Das verbesserte
GPU-Deploy-Werkzeug prüft die Verbindung vor Aktivierung und erneut im Container.

Chrome/Engelmann und Safari/Gmail brauchen noch einen Console-Login und eigene
Bridge-Schlüssel. Office/Opera und Werking/Arc sind bestätigt; je 200 USD
ursprüngliches Guthaben, tatsächliche aktuelle Beträge im Manifest/Console.
API-Guthaben und Partner-Browser-Logins sind unabhängige Arbeitsstränge.
