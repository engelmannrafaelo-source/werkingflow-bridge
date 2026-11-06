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
| gesamtzeit_s | RESEARCH_VERTRAG_GESAMTZEIT_S_MAX | = RESEARCH_GESAMTFRIST_S (3300) | gesamter Lauf inkl. Werkzeuge, Bibliothek, Prüfer, Rückrunde, Nachmessung | pool, cloud |
| transportzeit_s | RESEARCH_VERTRAG_TRANSPORTZEIT_S_MAX | 900 | je Messages-API-Aufruf, nie länger als Restfrist | cloud |

Importprüfung: `gesamtzeit_s ≤ RESEARCH_GESAMTFRIST_S`, `transportzeit_s ≤ gesamtzeit_s`,
`fortsetzungen ≥ 8`; sonst startet der Worker nicht. Ohne gesetztes
`RESEARCH_VERTRAG_GESAMTZEIT_S_MAX` ist die Obergrenze die Gesamtfrist der Zeitkette
selbst — eine kürzere Kette auf einem Host ist damit kein Startrisiko; nur ein
ausdrücklich darüber gesetzter Wert scheitert laut.

## Wünschen (Requestfeld `vertrag`)

`vertrag: {version?, ausgabe_tokens?, iterationen?, fortsetzungen?, suchen?, abrufe?,
perplexity_suchen?, dokument_abrufe?, gesamtzeit_s?, transportzeit_s?, verlangt?}` —
unbekannte Felder werden abgewiesen (FastAPI-Validierungsfehler 422 ohne
`fehler_code`). Regel: `effektiv = min(obergrenze, wunsch)`; ein Wunsch senkt nur.
`max_tokens` ist auf beiden Pfaden unwirksam (verbindlich ist
`vertrag.ausgabe_tokens`); `max_turns` senkt `iterationen`.

**Forderungen.** Eine unter der Obergrenze gesetzte Grenze ist eine Forderung an den
Pfad (er muss sie durchsetzen, sonst Ablehnung). `verlangt` (Liste von Grenznamen)
benennt die Forderungen ausdrücklich; die übrigen gesetzten Grenzen sind dann weich:
sie binden, wo der Pfad sie durchsetzen kann, und stehen sonst in
`nicht_durchsetzbar` ohne Ablehnung. `max_turns` ist nie eine Forderung — auch nicht
sein Default 30 im Selbstaufruf eines Jobs.

## Admission

1. Vor Routing: Vertrag bilden. `vertrag.version` ≠ aktuelle Version → nur als
   **vollständig bestimmter Vertrag** (gleiches Protokoll, alle neun Grenzen gesetzt,
   jede ≤ aktueller Obergrenze) übernommen (`quelle: snapshot`, Antwort nennt `version`
   und `server_version`; `max_turns` senkt auch hier `iterationen`; Forderungen sind
   die in `verlangt` genannten — fehlt `verlangt`, gilt jede Grenze unter der aktuellen
   Obergrenze als Forderung); sonst **HTTP 422**, `fehler_code vertrag_version_abweichend`,
   `retryable=false`, nichts gestartet.
2. Nach der Pfadentscheidung, vor Pool-Admission/Backend/Budget: ein Wunsch (unter der
   Obergrenze) für eine Grenze, die der Pfad nicht durchsetzen kann → **HTTP 422**,
   `fehler_code vertrag_nicht_durchsetzbar`, `retryable=false`. Kein Pfadwechsel, kein
   bezahlter Ersatz.

Beide 422-Antworten tragen den ResearchResponse-Rumpf (`status: error`, `vertrag`).
Eine Vertragsablehnung, die erst im Lauf fällt (Staging-Abo-Rückfall Cloud→Pool
wiederholt die Pool-Admission), wird ebenfalls als HTTP 422 beantwortet.

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
`nachkontrolle.rueckrunde_fehler`; `transport_timeout` (Cloud: ein Messages-API-Aufruf
hat innerhalb `transportzeit_s` nicht geantwortet; harte Obergrenze je Aufruf, nicht nur
je HTTP-Phase; kein Teilbericht; `retryable=false`). Jeder Abbruch an der Frist sagt
„the remote call may still be running“ — die Bridge hat aufgehört zu warten, ein Ende
des Fernaufrufs wird nicht behauptet.

**Wiederholung auf demselben Pfad (Cloud).** Der bestehende zweite Versuch bei einem
Cloud-Fehler bleibt für unklassifizierte Fehler und für benannte, als wiederholbar
gekennzeichnete Ursachen (z. B. `privacy_service_unreachable`). Eine **benannte Ursache
mit endgültigem Urteil** (`laufzeit_erschoepft`, `transport_timeout`, `vertrag_*`,
`privacy_gate_failed`, `modell_abbruch`) wird nicht wiederholt. Entscheidung für den
Transport-Timeout: der Fernaufruf kann noch laufen (ein zweiter Lauf zahlt die Recherche
doppelt), und die Restfrist trägt selten einen zweiten vollen Lauf — dieselbe Regel wie
beim Lese-Timeout des Job-Selbstaufrufs. Die Frist beginnt bei einer Wiederholung nie
neu: `frist_bis` liest die am Request gemerkte Frist zurück.

## Dauerhafte Jobs

`POST /v1/jobs` (`kind: research`): der Client-Rumpf bleibt unverändert und ist
weiterhin der Idempotenz-Fingerabdruck (`payload_digest`); Replay mit gleichem
Owner+Key+Rumpf findet den Job auch nach geänderten Serverobergrenzen. Der beim Anlegen
wirksame Vertrag wird als Snapshot (`version` + neun Grenzen + `verlangt`) serverseitig
in `attribution.research_vertrag` persistiert (nach der Idempotenz-Suche, vor dem
Anlegen; ein rumpfgelieferter Wert wird entfernt). Der Snapshot spiegelt, was der
Selbstaufruf bindet: `iterationen` folgt dem `max_turns` des Rumpfs bzw. dem
ResearchRequest-Default 30. Der Selbstaufruf — erster Start und Wiederaufnahme nach
Neustart — sendet den Snapshot als `vertrag`; weil `verlangt` nur die beim Anlegen
geforderten Grenzen nennt, erzeugt ein Snapshot auf dem Cloud-Pfad keine Forderung nach
`iterationen` und auf dem Pool-Pfad keine nach einer reinen Cloud-Grenze. Eine neue
Serverpolicy führt ihn nur unverändert aus (Regel „vollständig bestimmter Vertrag“)
oder lehnt ihn ausdrücklich ab (`vertrag_version_abweichend`), nie still neu
interpretiert.
Ungültiger Wunsch beim Anlegen → 400 `vertrag_ungueltig`; fremde, nicht übernehmbare
Version → 422 `vertrag_version_abweichend`; kein Job.

## Verhaltensänderungen gegenüber dem Stand vor dem Vertrag (06.11.2026)

- **Pool:** `max_turns` war unbegrenzt (jeder Wert ging an die CLI; fehlend = kein
  Deckel). Jetzt gilt `iterationen = min(40, max_turns)`; fehlendes `max_turns` bedeutet
  die Obergrenze 40. Perplexity-/Dokument-Deckel kommen aus dem Vertrag (Defaults
  unverändert 8/10); die In-Process-Werkzeuge brechen an der Restfrist ab.
- **Cloud:** neu eine Gesamtfrist (`gesamtzeit_s`, Default = RESEARCH_GESAMTFRIST_S =
  3300 s) über Modellaufrufe, Werkzeuge, Bibliothek, Prüfer, Rückrunde und Nachmessung;
  vorher gab es nur das HTTP-Timeout je Aufruf. Prüfung/Rückrunde/Nachmessung starten
  nur noch mit ausreichender Restzeit. `fortsetzungen` zählt über alle Runden (Obergrenze
  16 = bisheriges Maximum 8 + 8; je Runde weiterhin 8). Ein Transport-Timeout ist jetzt
  `fehler_code transport_timeout` mit `retryable=false` (vorher: unbehandelte Ausnahme).
- **Antworten:** neues Feld `vertrag`; `laufzeit` auch auf dem Cloud-Pfad; HTTP 422 für
  Vertragsablehnungen (vorher gab es keine).
- **Jobs:** `attribution.research_vertrag` wird serverseitig gesetzt; Rumpf und
  Idempotenz-Fingerabdruck unverändert.

## Bekannte Grenzen

- Pool/CLI setzt `ausgabe_tokens`, `fortsetzungen`, `suchen`, `abrufe`, `transportzeit_s`
  nicht durch (ausgewiesen, Forderungen danach werden abgelehnt).
- `suchen`/`abrufe` gelten je Messages-API-Anfrage; die Lauf-Summe wird gemeldet, nicht
  erzwungen.
- Die Jobs-Anlage kennt den Pfad nicht; eine am Pfad nicht durchsetzbare Forderung endet
  erst im Selbstaufruf (HTTP 422 → Job `error` mit `fehler_code`).
- Ein Abbruch an der Frist ist ein Abbruch des Wartens; ob der Fernaufruf weiterläuft,
  wird nicht behauptet.
- Testseitig: die Netzsperre (`tests/netzsperre.py`) ist je Testmodul scharf; die
  gemeinsame `tests/conftest.py` importiert `src.activity.ledger_client` vor ihr
  (Import ohne Netzverkehr).
