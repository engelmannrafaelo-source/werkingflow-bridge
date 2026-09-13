# AI-Bridge — KI-Dienst fuer API und Recherche

Die Bridge stellt eine OpenAI-kompatible KI-API und Recherche bereit.
Normale App-, Dev- und Partner-Aufgaben nutzen diesen Dienst ueber seinen
API-Vertrag; die interne Bereitstellung gehoert nicht zum Standardkontext.

## Nutzung

- Chat: `POST /v1/chat/completions`; Recherche: `POST /v1/research`.
- Recherche-Ergebnis: `GET /v1/research/{session_id}/content`.
- Modelle: `GET /v1/models`; Erreichbarkeit: `GET /health`.
- URL, Zugang und Nutzeridentitaet aus der Konfiguration der Zielumgebung beziehen.
  Dev/Test/Staging und Produktion haben getrennte Zugaenge und Nutzerdaten.
- Fehler mit korrektem Request-Schema, Status und Zeitstempel reproduzieren;
  keine Ursache aus historischen Notizen ableiten.

## Arbeit an der Bridge selbst

Nur bei einem konkreten Bridge-Auftrag die [Wartungseinstieg](docs/maintenance.md)
und aufgabenbezogene Architektur-Entscheidungen unter `docs/adr/` nachladen.
Vor Implementierungs- oder Betriebsarbeiten ist diese Referenz Pflicht;
sie gehoert nicht zur Pflichtlektuere fuer reine API-Nutzung oder Recherche.

Bestehende Betriebsschranken gelten weiter: keine eigenmaechtigen Eingriffe in
laufende Dienste, keine Hand-Aenderungen auf den Bridge-Hosts. Aenderungen ueber
das Repository; Deploy ausschliesslich mit `scripts/bridge-deploy.sh` aus einem
committeten Stand und mit ausdruecklicher Freigabe. Bei anhaltenden Stoerungen
Rafael mit reproduzierbarem Befund informieren.
