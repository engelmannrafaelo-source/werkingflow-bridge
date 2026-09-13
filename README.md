# AI-Bridge

Zentraler KI-Dienst fuer OpenAI-kompatiblen API-Zugriff und Recherche.
Anwendungen nutzen die Bridge ueber ihren API-Vertrag.

## Verbindung

URL, API-Key und Nutzeridentitaet kommen aus der Konfiguration der jeweiligen
Anwendung und Zielumgebung. Dev/Test/Staging und Produktion getrennt behandeln.
Keine Zugangsdaten in Dokumentation oder Quellcode eintragen.

## Schnittstellen

| Methode | Endpoint | Zweck |
|---------|----------|-------|
| POST | `/v1/chat/completions` | OpenAI-kompatible KI-Anfragen |
| POST | `/v1/research` | Recherche |
| GET | `/v1/research/{session_id}/content` | Recherche-Ergebnis abrufen |
| GET | `/v1/models` | Verfuegbare Modelle |
| GET | `/health` | Erreichbarkeit pruefen |

Authentifizierung mit `Authorization: Bearer <API-Key>`. Die aufloesbare
Nutzeridentitaet wird als `X-User-ID` uebermittelt; ein freier Anzeigename genuegt
nicht. Modelle aus der Modellliste der Zielumgebung beziehen.

## Recherche

`POST /v1/research` erwartet eine `query`; `depth` unterstuetzt
`quick`, `standard`, `deep` und `exhaustive`. Die Antwort kann den Inhalt direkt
enthalten und eine `session_id` fuer den spaeteren Download liefern.
Laufzeit und Ausgabeumfang haengen von Anfrage, Modell und Auslastung ab.

Fuer App-Integration den bestehenden Bridge-Client der Anwendung verwenden.
Auf Dev und Partner stehen zusaetzlich vorkonfigurierte Recherche-Kommandos
bereit; deren lokale Nutzungshinweise gelten fuer die jeweilige Umgebung.

## Fehler einordnen

Zuerst Zielumgebung, Berechtigung, Request-Schema und HTTP-Antwort pruefen.
Ein erfolgreicher Health-Check garantiert keine Kapazitaet fuer einen langen
Auftrag. Anhaltende Fehler mit Zeitstempel und reproduzierbarer Anfrage melden,
ohne Zugangsdaten zu protokollieren. Keine interne Ursache aus dem Statuscode
oder historischen Notizen ableiten.

## Wartung

Nur bei einem konkreten Auftrag zur Implementierung oder zum Betrieb des
Dienstes den [Wartungseinstieg](docs/maintenance.md) lesen.
Die dortige Dokumentation ist fuer normale API-Nutzung und Recherche nicht erforderlich.
