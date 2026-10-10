# Erkunder: Eingangsordner und Lesezugriffe je Schritt

## Eingang

Der Leitstand legt jeden Auftrag unter `/arbeit/<bericht_id>/eingang/` ab. Jede
Datei des Auftrags (`dateien[].ziel`) liegt in genau einem dieser Ordner
(`EINGANGSORDNER` in `src/erkunder/models.py`):

| Ordner | Inhalt |
|---|---|
| `messdaten/` | Messreihen (Parquet mit `sensor_id`, gehen ins Kanalmanifest) |
| `unterlagen/` | Kundenunterlagen als Text |
| `plan/` | Anlagenplan |
| `pruefwissen/` | ausgewählte Fachdokumente der Prüfbibliothek, je Datei ein Dokument mit Kennung und Titel |

Für `ziel` gilt in jedem Ordner dasselbe: `<ordner>/<name>`, Name 1–200
Zeichen, kein Unterordner, kein `..`, kein Backslash, keine Steuerzeichen (auch
kein Zeilenumbruch am Ende). Geprüft wird das an drei Stellen: bei
`POST /v1/jobs` (Worker), im Erkunder-Executor (Worker) und beim `/start` des
Leitstands.

Liegen Dateien in `pruefwissen/`, beschreibt der Prompt den Ordner (Fachwissen
hinter den Bibliotheks-Punkten im Vorwissen, gezielt mit Read/Grep zu lesen).
Ohne solche Dateien bleibt der Prompt unverändert. Alle Eingangsdateien stehen
mit SHA-256 in `eingang/quellen.md` und im Quellenanhang des Gutachtens.

**Größe.** Der Vertrag begrenzt weder die Anzahl der Dateien noch ihre Summe. Je
Datei gilt: `bytes` ist angegeben und wird beim Laden exakt geprüft, und das
Laden darf höchstens 120 s dauern. Die Dateien kommen über signierte URLs, nicht im
Job-Body. Für den Body (u. a. `vorwissen_md`) gilt nginx `client_max_body_size
75M`. Platz auf der Platte stellt das Volume `erkunder-arbeit`; eine Quote gibt es
nicht. `dateien.MAX_FILE_BYTES` (5 MB) gilt nur für Ergebnisse, die der Leitstand
aus den Schrittordnern liest, nicht für den Eingang.

## `meta.schritte[i].lesezugriffe`

Das Ergebnis (`erkunder-ergebnis/1`) trägt je Schritt optional:

```json
"lesezugriffe": [
  {"werkzeug": "read", "pfad": "pruefwissen/kw-thema-waermepumpe.md"},
  {"werkzeug": "grep", "pfad": "pruefwissen"},
  {"werkzeug": "glob", "pfad": "pruefwissen/*.md"},
  {"werkzeug": "bash", "pfad": "unterlagen/vertrag.txt"}
]
```

- `pfad` ist relativ zu `eingang/` (`.` = der Ordner selbst). Bei Glob ist es
  Pfad und Muster zusammen.
- Erhoben werden die Werkzeugaufrufe des Modells aus dem SDK-Strom
  (`ToolUseBlock` in `AssistantMessage`), in der Reihenfolge der Aufrufe. Wiederholte
  Aufrufe erscheinen wiederholt. Protokolliert wird der Aufruf, nicht dessen
  Erfolg. Aufrufe außerhalb von `eingang/` (eigener Schrittordner,
  Grep/Glob ohne Pfad) erscheinen nicht.
- `bash`: Ein Pfad unter `eingang/` steht im Text eines Shell-Befehls. Das ist ein
  Hinweis, kein Lesebeweis. Der Befehlstext wird nicht übernommen. Was ein
  Python-Skript aus `skripte/` liest, bleibt unsichtbar.
- **Fehlt das Feld, wurde nicht erhoben.** Das gilt für einen alten Platz, einen
  abgebrochenen Schritt (das Kind meldet dann nur den Fehler) und für Schritte aus
  der Zeit vor der Erhebung. Die Erhebung ohne einen einzigen Zugriff ist `[]`.
  Der Aufrufer meldet ein fehlendes Feld als „nicht erhoben“, nie als „nichts
  gelesen“.

## Ausrollen

Das neue Feld und `erkunder-prompts/3` sind nur mit einem Worker gültig, der sie
kennt: `Ergebnis` verbietet unbekannte Felder. Ein Leitstand, der vor dem Worker
neu ist, führt deshalb zu „Leitstand-Antwort ungueltig“. Deshalb kommen die
Worker vor oder zusammen mit der Erkunder-Gruppe. `pruefwissen/`-Aufträge sind
erst gültig, wenn beide Images neu sind.
