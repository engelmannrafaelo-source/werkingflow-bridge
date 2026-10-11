# Erkunder: Rechenstand im Prüfkreis und Statusregeln

## Rechenstand

Jeder Schritt arbeitet in einem frischen Ordner. Damit Prüfer und Korrektor die
Zahlen einer Fassung zuordnen können, legt der Leitstand den Rechenstand der
geprüften Fassung dazu (`src/erkunder/rechenstand.py`):

| Schritt | Eingang | Herkunft |
|---|---|---|
| `pruefung*` | `rechenstand/skripte/…` | `skripte/` der geprüften Fassung |
| `harmonisierung-korrektur*` | `skripte/…` | `skripte/` der Vorfassung, darauf wird weitergerechnet |

- Übergeben werden alle regulären Dateien unter `skripte/` (Skripte und
  Ergebnisdateien), Pythons `__pycache__` ausgenommen.
- Gelesen wird wie bei den übrigen Eingängen über `read_bytes`: keine
  Verknüpfungen, keine Sonderdateien, je Datei höchstens `MAX_FILE_BYTES`, dazu
  eine Gesamtgrenze `MAX_RECHENSTAND_BYTES`.
- Was nicht übergeben werden kann, wird zum Maschinenbefund
  („Rechenstand fehlt: …“, „Rechenstand nicht übergeben: …“). Der Befund steht in
  `maschinenbefunde.json` des Prüfers und in den offenen Befunden, damit die
  Korrektur ihn beheben kann.
- Ein Eingangsname, der aus dem Schrittordner hinausführt, bricht den Schritt
  mit `cli_fehler: eingang-pfad` ab.
- Der Prompt erwähnt den Rechenstand nur, wenn tatsächlich Dateien übergeben
  wurden.

## Pfade im Nachweisblock

`ergebnisdateien[].skript` und `.ergebnis` gelten ab dem Gutachtenordner und
beginnen mit `skripte/` (`pruefkreis._artifact`). Der Auftrag nennt dieselbe Form
mit einem Beispiel. Ein abweichender Pfad erscheint im Befund mit seinem Wortlaut.

## Statusregeln der Bibliotheksurteile

Der Leitstand prüft die Urteile maschinell (`pruefkreis._urteil`). Die Bedeutung der
Status steht beschreibend in `prompts.STATUSREGELN` und geht an Erkunder,
Harmonisierung, Korrektur und Prüfer:

- Fehlende Kanäle aus der Prüfliste stehen im Urteil. Fehlt ein Kanal, ist das
  Urteil `teilweise` oder `nicht_pruefbar`.
- `nicht_pruefbar` ist auch ohne fehlenden Kanal zulässig, wenn die Begründung
  sagt, warum die Daten keine Aussage tragen.
- `bestaetigt` verweist auf den eigenen Befund. Jedes Urteil hat eine nicht
  leere Begründung.

## Ausrollen

`erkunder-prompts/4` ist nur mit einem Worker gültig, der die Version kennt
(`Ergebnis.prompt_version`). Deshalb kommen die Worker vor oder zusammen mit der
Erkunder-Gruppe (Leitstand und Plätze, ein Image `bridge-erkunder`).
