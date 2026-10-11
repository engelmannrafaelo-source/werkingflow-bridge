# Agentischer Lauf über den CLI-Platz

Der allgemeine Modus `agentischer-lauf/1` kann über die vorhandene Isolation
des Erkunder-Platzes laufen. Die CLI fährt ihre Werkzeugschleife selbst; die
Bridge setzt keine harte Gesamtgrenze durch und sagt das in jedem Ergebnis
(`grenze.hard_token_bound = false`, `grenze.durchsetzung = "cli_laufend_gemessen"`).

## Erklärte Grenzen

| Grenze | Durchsetzung | Ende bei Überschreiten |
|---|---|---|
| Runden | `--max-turns` = `max_turns` der Phase | `lauf_unvollstaendig` (CLI-Ende `error_max_turns`) |
| Ausgabe je Antwort | `CLAUDE_CODE_MAX_OUTPUT_TOKENS` = `max_tokens`; die CLI sendet ihn als `max_tokens` jeder Anfrage | gemeldete Ausgabe darüber: `grenze_tokens`, Ursache `ausgabe_ueber_grenze` |
| Laufzeit | `grenzen.zeit_s` ab Annahme; die Prozessgruppe wird getötet | `grenze_zeit` |
| Eingang | Startauftrag vor dem Start mit dem Zähler des Anbieters gemessen (`count_tokens`), keine Schätzung | `grenze_tokens`, Ursache `eingang_ueber_budget`; ohne Zähler `faehigkeit_nicht_verfuegbar` |
| laufende Nutzung | Summe aus `message_start` (Eingang, Cache) und `message_delta` (Ausgabe) jeder Antwort gegen das Restbudget | Prozessgruppe getötet, `grenze_tokens`, Ursache `laufende_nutzung_ueber_budget` |
| Abschluss | Summen des CLI-Ergebnisses gegen den Strom; der größere Wert zählt | `grenze_tokens`, Ursache `nutzung_nachtraeglich_ueber_budget` bzw. `nutzung_unbekannt` |

Die CLI meldet eine Antwort erst, nachdem die Anfrage gesendet ist. Eine
einzelne Antwort kann das Budget deshalb um ihren eigenen Eingang plus
`max_tokens` überschreiten; das Ergebnis meldet den tatsächlichen Verbrauch.

## Beleg am CLI-Code (2.1.285)

- Die Schleife gibt die Assistentennachricht bei `content_block_stop` aus und
  aktualisiert die Nutzung erst bei `message_delta` (`case"message_delta":{if(Ha=oee(Ha,Ps.usage)`).
  Die `assistant`-Zeile trägt daher nur den Stand von `message_start`
  (`output_tokens` vorläufig).
- `--include-partial-messages` gibt jedes Rohereignis als `stream_event` aus
  („emitted only when partial messages are requested“). Damit stehen
  `message_start.message.usage` und `message_delta.usage` vor dem Ergebnis im Strom.
- Gemessen gegen eine lokale Messages-Attrappe: `message_delta` liefert die
  endgültige Ausgabe je Antwort; jede Anfrage trägt `max_tokens` aus
  `CLAUDE_CODE_MAX_OUTPUT_TOKENS`; `--tools` legt genau die an die API
  gemeldeten Werkzeuge fest; ein 529 endet als `is_error:true` mit
  `api_error_status:529`.

Die Ströme liegen als Testvorlagen unter `tests/fixtures/cli_2_1_285_lauf/`.

## Fehlercodes des CLI-Wegs

`kapazitaet_belegt` (wiederholbar, `retry_after_s`) bei `api_error_status`
429/503/529, `anbieter_fehler` bei anderen Anbieterfehlern, `sandbox_fehler`
bei einem Fehler ohne Anbieterstatus oder einem Zugang in Ergebnis oder
Artefakt, `artefakt_integritaet` bei Links, Spezialdateien oder Übergröße.

## Werkzeuge

Die Vertragsnamen bleiben dieselben wie im Sandbox-Weg. Der Platz bildet sie
auf CLI-Werkzeuge ab: `Read` → Read, Glob, Grep; `List` → Glob; `Write` →
Write, Edit. Alles andere ist über `--tools` nicht verfügbar und zusätzlich
über `--disallowedTools` gesperrt.

## Isolation und Ablage

Je Phase ein frischer Ordner `<platz_wurzel>/<schlüssel>/` mit
schreibgeschütztem `eingang/` (Material) und Arbeitsordner `lauf/`. Das ist
die Anordnung, die der Platz-Hook (`/etc/erkunder/settings.json`) schon
prüft; im Platz-Container ist `platz_wurzel` daher `/arbeit`. Eigene
Prozessgruppe, minimale Umgebung, Zugang nur in der Umgebung des Kindes.
Mit `dedicated_uid` zusätzlich das Aufräumen des Platzes (alle Prozesse der
UID, adoptierte Kinder, eigene Dateien in /tmp, IPC); nur zulässig, wo die
UID allein dem Platz gehört. Ausgaben werden nur für deklarierte Ziele über
die begrenzten Deskriptor-Lesezugriffe des Platzes gelesen und auf den Zugang
geprüft; danach wird der Ordner entfernt.

## Zulassung (Gate 2)

`zulassung.verdrahte` setzt `http.configure(runtime, admit)` aus der Datei, die
`AGENTISCHER_LAUF_KONFIG` nennt. Ohne diese Datei bleibt der Modus bei
422 `faehigkeit_nicht_verfuegbar`. Die Zulassung verlangt:

- einen verifizierten Mandanten aus dem Principal (`principal/app`); der
  geteilte Schlüssel nur bei `geteilter_schluessel: true`,
- Modell und angeforderte Werkzeuge beider Phasen aus der erlaubten Menge,
- ausdrücklich gesetzte Grenzen innerhalb der Servermaxima
  (`max_zeit_s`, `max_tokens_gesamt`, `max_turns`, `max_tokens_je_antwort`).

Felder der Konfiguration: `platz_wurzel`, `befehl`, `settings`,
`dedicated_uid`, `max_bytes`, `zugang_variable` (Name der Umgebungsvariable mit
dem Zugang), `cli_zugang_variable`, `zaehler_url`, `oauth`, `modelle`
(Vertragsname → CLI-Modell), `werkzeuge`, die vier Maxima, `speicher`,
`revision`, optional `geteilter_schluessel`.
