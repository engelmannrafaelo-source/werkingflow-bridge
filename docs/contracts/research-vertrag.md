# Recherche-Ausführungsvertrag (research-vertrag/1)

Quelle: `src/research_vertrag.py` (P6-VERTRAG, 06.11.2026). Gilt für
`POST /v1/research` (sync und `async_mode`) und für `POST /v1/jobs` mit
`kind: research`, auf dem Abo-Pool-Pfad (CLI) und dem Research-Cloud-Pfad.
Ohne das Feld `vertrag` läuft eine Anfrage wie bisher (Legacy); nur
`max_turns` senkt wie zuvor die Pool-Turns.

## Lesen vor Admission

`GET /v1/research/vertrag` (gleiche Authentifizierung wie `/v1/models`) liefert
`version`, `grenzen` (Serverobergrenzen) und `capabilities`:

- `pfade.pool|cloud.wirksam` / `.nicht_durchsetzbar` — was der Pfad durchsetzt
  und was nicht (Pool/CLI: keine Ausgabe-Token-Grenze, keine Deckel für die
  CLI-eigenen WebSearch/WebFetch, keine Fortsetzungszählung, kein HTTP-Transport;
  Cloud: keine CLI-Turns);
- `geltung` je Grenze: gesamter Lauf / je Runde / je API-Anfrage / je Modellantwort;
- `fortsetzungen_je_runde` (8), `depth_budget`, `promotional_cap`, `zeit_minima`.

`version` = `research-vertrag/1+<sha256 über Obergrenzen UND Capabilities>[:12]`.
Jede geänderte Zahl (env `RESEARCH_VERTRAG_*_MAX`) und jede geänderte Semantik
ist eine neue Version.

| Grenze | env | Default | Geltung | wirksam auf |
|---|---|---|---|---|
| ausgabe_tokens | RESEARCH_VERTRAG_AUSGABE_TOKENS_MAX | 32000 | je Modellantwort (max_tokens) | cloud |
| iterationen | RESEARCH_VERTRAG_ITERATIONEN_MAX | 40 | Hauptlauf (CLI-Turns); Reparatur/Rückrunde eigene feste Deckel ≤ 40 | pool |
| fortsetzungen | RESEARCH_VERTRAG_FORTSETZUNGEN_MAX | 16 | gesamter Lauf, alle Runden; je Runde zusätzlich 8 | cloud |
| suchen | RESEARCH_VERTRAG_SUCHEN_MAX | 20 | je Messages-API-Anfrage (web_search max_uses) | cloud |
| abrufe | RESEARCH_VERTRAG_ABRUFE_MAX | 15 | je Messages-API-Anfrage (web_fetch max_uses) | cloud |
| perplexity_suchen | RESEARCH_VERTRAG_PERPLEXITY_MAX | 8 | gesamter Lauf | pool, cloud |
| dokument_abrufe | RESEARCH_VERTRAG_DOKUMENTE_MAX | 10 | gesamter Lauf | pool, cloud |
| gesamtzeit_s | RESEARCH_VERTRAG_GESAMTZEIT_S_MAX | 3300 | gesamter Lauf inkl. Werkzeuge, Bibliothek, Prüfer, Rückrunde, Nachmessung | pool, cloud |
| transportzeit_s | RESEARCH_VERTRAG_TRANSPORTZEIT_S_MAX | 900 | je Messages-API-Aufruf, nie länger als Restfrist | cloud |

Importprüfung: `gesamtzeit_s ≤ RESEARCH_GESAMTFRIST_S`, `transportzeit_s ≤ gesamtzeit_s`,
`fortsetzungen ≥ 8`; sonst startet der Worker nicht.

## Wünschen (Requestfeld `vertrag`)

`vertrag: {version?, ausgabe_tokens?, iterationen?, fortsetzungen?, suchen?, abrufe?,
perplexity_suchen?, dokument_abrufe?, gesamtzeit_s?, transportzeit_s?}` — unbekannte
Felder werden abgewiesen. Regel: `effektiv = min(obergrenze, wunsch)`; ein Wunsch
senkt nur. `max_tokens` ist auf beiden Pfaden unwirksam (verbindlich ist
`vertrag.ausgabe_tokens`); `max_turns` senkt `iterationen`.

## Admission

1. Vor Routing: Vertrag bilden. `vertrag.version` ≠ aktuelle Version → nur als
   **vollständig bestimmter Vertrag** (gleiches Protokoll, alle neun Grenzen gesetzt,
   jede ≤ aktueller Obergrenze) übernommen (`quelle: snapshot`, Antwort nennt `version`
   und `server_version`); sonst **HTTP 422**, `fehler_code vertrag_version_abweichend`,
   `retryable=false`, nichts gestartet.
2. Nach der Pfadentscheidung, vor Pool-Admission/Backend/Budget: ein Wunsch (unter der
   Obergrenze) für eine Grenze, die der Pfad nicht durchsetzen kann → **HTTP 422**,
   `fehler_code vertrag_nicht_durchsetzbar`, `retryable=false`. Kein Pfadwechsel, kein
   bezahlter Ersatz.

Beide 422-Antworten tragen den ResearchResponse-Rumpf (`status: error`, `vertrag`).

## Wirksamkeit

- Pool: `max_turns = iterationen`; `perplexity_search`/`fetch_document` mit
  `perplexity_suchen`/`dokument_abrufe` und der Restfrist; Gesamtfrist =
  `min(weitergetragene Frist, gesamtzeit_s)`; CLI-Prozess wird an der Frist terminiert.
- Cloud: `ResearchCloudConfig` aus dem Vertrag; `suchen`/`abrufe` zusätzlich durch
  depth-Budget und Promotional-Cap gesenkt (`begrenzt_durch: depth|promotional`);
  jeder Messages-API-Aufruf wartet `min(transportzeit_s, Restfrist)`; Werkzeuge,
  Prüfer und Nachmessung laufen unter der Restfrist; Prüfung/Rückrunde/Nachmessung
  nur bei ausreichender Restzeit (benannt in `nachkontrolle.uebersprungen`).
  `fortsetzungen` zählt über alle Runden; die Rückrunde erhält keine neue Reserve.

## Antwort

`vertrag` in jeder ResearchResponse (Erfolg und Fehler, sync und async-Datei):
`version`, `server_version`, `quelle` (anfrage|snapshot), `grenzen` (exakt wirksame
Werte), `begrenzt_durch` je Grenze (server|client|depth|promotional|snapshot),
`geltung`, `pfad`, `nicht_durchsetzbar`. `laufzeit` jetzt auf beiden Pfaden.

Fehlersemantik: `laufzeit_erschoepft` (Hauptlauf/Fortsetzung/Modellaufruf an der Frist;
kein Teilbericht; `retryable=false`); Rückrunde an der Frist → Bericht von vorher,
`nachkontrolle.rueckrunde_fehler`; Transport-Timeout → Fehler nennt `transportzeit_s`.
Jeder Abbruch an der Frist sagt „the remote call may still be running“ — die Bridge
hat aufgehört zu warten, ein Ende des Fernaufrufs wird nicht behauptet.

## Dauerhafte Jobs

`POST /v1/jobs` (`kind: research`): der Client-Rumpf bleibt unverändert und ist
weiterhin der Idempotenz-Fingerabdruck (`payload_digest`); Replay mit gleichem
Owner+Key+Rumpf findet den Job auch nach geänderten Serverobergrenzen. Der beim Anlegen
wirksame Vertrag wird als Snapshot (`version` + neun Grenzen) serverseitig in
`attribution.research_vertrag` persistiert (nach der Idempotenz-Suche, vor dem Anlegen;
ein rumpfgelieferter Wert wird entfernt). Der Selbstaufruf — erster Start und
Wiederaufnahme nach Neustart — sendet den Snapshot als `vertrag`; eine neue Serverpolicy
führt ihn nur unverändert aus (Regel „vollständig bestimmter Vertrag“) oder lehnt ihn
ausdrücklich ab (`vertrag_version_abweichend`), nie still neu interpretiert.
Ungültiger Wunsch beim Anlegen → 400 `vertrag_ungueltig`; fremde, nicht übernehmbare
Version → 422 `vertrag_version_abweichend`; kein Job.
