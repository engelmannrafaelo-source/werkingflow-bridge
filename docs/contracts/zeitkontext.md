# Zeitkontext: Restzeit eines Jobs für das Modell (Abo-CLI)

Entscheid Bridge-Owner/Kanzler vom 10.11.2026. Code: `src/jobs/routes.py`
(Annahme), `src/jobs/selbstaufruf.py` (Übergabe an den Lauf),
`src/claude_cli.py` (settings-Datei je Lauf), `src/zeitkontext_hook.py` (Hook).

## Zweck

Der Aufrufer (Energy) kennt die Frist seines Laufs. Die Bridge transportiert
sie zum Modell, damit es an Werkzeug- und Entscheidungsgrenzen sieht, wie viel
Zeit bleibt. Die Bridge rechnet kein eigenes Budget, entscheidet nichts und
bricht nichts ab — der Aufrufer bleibt die einzige Quelle der Zeit.

## Feldvertrag (nur `POST /v1/jobs`)

| Kopfzeile | Wert | Pflicht |
|---|---|---|
| `X-Zeitkontext-Lauf-Ende` | ganze UTC-Epoch-Sekunden, Ende des Laufs | ja, sobald der Satz gesetzt ist |
| `X-Zeitkontext-Phase-Ende` | ganze UTC-Epoch-Sekunden, Ende der Arbeitsphase | nein (fehlt = `null`) |

- Phase-Ende ist `min(arbeit_bis, lauf.ende)` und damit stets `<=` Lauf-Ende.
  Ein Abschluss-/R-Ende wird nicht transportiert.
- Die Bridge speichert `attribution.zeitkontext =
  {"lauf_ende_epoch": int, "phase_ende_epoch": int|null}` — absolute Fristen,
  **keine Restzeit**, **nicht im payload**. `payload_digest` und damit die
  Idempotenz bleiben unberührt: dieselbe `Idempotency-Key`-Wiederholung mit
  anderem Zeitkontext ist derselbe Payload. Gespeichert bleibt dabei der
  Zeitkontext der ersten Annahme (gewollt: ein Replay liefert den ALTEN Job
  mit der ALTEN Frist).
- `422` (`detail.code = "zeitkontext_invalid"`), laut und vor dem Anlegen:
  kein Integer; unplausibel (vor Epoch 1600000000 oder mehr als 30 Tage
  voraus — fängt Millisekunden und „Restzeit statt Frist“); Phase-Ende ohne
  Lauf-Ende; Phase-Ende > Lauf-Ende; `zeitkontext` im Rumpf-`attribution`
  (es gibt genau einen Weg: die Kopfzeilen).
- Eine bereits verstrichene Frist ist gültig (der Hook meldet dann „Frist
  abgelaufen“).
- Ohne die Kopfzeilen ist das Verhalten unverändert.

## Wirkung

- Der Selbstaufruf eines Jobs liest den **gespeicherten** Zeitkontext aus dem
  Job-Store (nie aus einer Kopfzeile) und bindet ihn an seinen CLI-Lauf.
- Nur dann schreibt `ClaudeCodeCLI.run_completion` je Lauf eine temporäre
  settings-Datei (`--settings`) mit den Hooks `UserPromptSubmit` und
  `PostToolUse` (matcher `*`) und entfernt sie nach dem Lauf. Ohne Zeitkontext
  wird keine settings-Option gesetzt. Trägt der Lauf schon eine
  settings-Option, ist das ein lauter Fehler, kein Überschreiben.
- Der Hook rechnet `rest = ende - jetzt` und liefert dem Modell als
  `additionalContext`:
  `ZEITKONTEXT frist_utc=<ISO> rest_sekunden=<N>` und bei gesetzter Phase
  zusätzlich `phase_frist_utc=<ISO> phase_rest_sekunden=<M>`; nach Ablauf
  statt der Restzeit `Frist abgelaufen`. Jeder Fehler im Hook: leere Ausgabe,
  Exit 0 — der Hook bricht keinen Lauf.

## Grenzen

- Nur der Abo-CLI-Weg (`run_completion`) eines Job-Selbstaufrufs. Direkte
  Aufrufe ohne Job und Nicht-CLI-Provider bekommen keinen Zeitkontext.
- Wird der Selbstaufruf an einen anderen Worker weitergereicht
  (`x-bridge-chat-frist-bis`, Peer-Chat-Fall), läuft er dort ohne Zeitkontext
  und ohne Hinweis im Log; dieser Fall ist nur dokumentiert.
- Jeder Selbstaufruf macht einen zusätzlichen `get_job` (Owner nimmt das an).
- Die Angabe erreicht das Modell erst an der nächsten Hook-Grenze (Prompt-
  Eingang, Ende eines Werkzeugaufrufs); ein Lauf ohne Werkzeuge sieht sie
  einmal am Anfang.
- Kein Abbruch, keine Durchsetzung: die Frist ist ein Hinweis.
- Schlägt das Lesen des Jobs fehl, läuft der Job ohne Hinweis weiter; der
  Fehler steht im Log.

## Jobzeitbeleg

`GET /v1/jobs/{job_id}` liefert zusätzlich `created_at` und `finished_at`
(UTC-Epoch-Sekunden, float, 3 Nachkommastellen; `finished_at` ist `null`, bis
der Job terminal ist). Quelle ist die Store-Zeile.

## Annahme-Echo

`GET /v1/jobs/{job_id}` liefert zusätzlich `zeitkontext` = gespeicherter
`attribution.zeitkontext` (`{lauf_ende_epoch, phase_ende_epoch}`) oder `null`.
Nur dieses Feld, additiv. Im gemischten Rolling sieht der Aufrufer so, ob der
Job den Zeitkontext trägt: Energy darf sich auf den Hook erst nach vollendetem
Rolling verlassen; Prüfung per GET-Feld.
