# Word-Korpus der unabhängigen Prüfungen (synthetisch, keine Kundendaten)

Herkunft: Opus-Zweitprüfungen R1–R3 der Bridge-Docx-Absätze (Belege `BRIDGE-DOCX-ABSAETZE-R2/R3-ZWEITPRUEFER-OPUS-belege`,
SHA256SUMS dort). Die vier großen Bombenproben (`bombe`, `h17_luegengroesse`, `h20a`, `h20b`) erzeugt
`tests/unit/test_word_korpus.py` selbst, statt sie einzuchecken.

Gate: `tests/unit/test_word_korpus.py` schickt jede Datei durch den echten `/document/convert`.
4xx nur für echte Gefahren, und dann startet Writer nicht:

| Probe | Soll |
|---|---|
| bombe, h20a, h20b, riesig | 413 Entpackbudget (gesamt 256 MiB; Teil > 10 MiB mit Verhältnis > 100:1) |
| h17_luegengroesse | 415, CRC-Fehler beim begrenzten Kopieren |
| falsch_pdf, falsch_zufall, falsch_zip | 415 „Kein Word-Dokument“ |
| alle übrigen | 200 mit Text; nicht sicher zuordenbare Elemente nur lokal mit sichtbarem Hinweis |

Inhaltliche Soll-Werte je Probe stehen in `EXPECT`/`FORBIDDEN` im Test.
