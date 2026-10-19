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

## R5: Proben der Codex-Prüfung R4 (c01–c12)

Herkunft: `BRIDGE-DOCX-ABSAETZE-R4-PRUEFER-CODEX-belege/` (`eigene/`, `worker1/`, `security/`),
bytegleich übernommen; SHA256 in `bridge-docx-absaetze-20261019/r5/korpus-c-sha256.txt`.
Die eindeutigen Prüfzeichen (`QX…QZ`) müssen im mit CommonMark gerenderten Text **genau einmal**
stehen (`SENTINELS` im Test): weder still verloren noch doppelt.

| Probe | Soll |
|---|---|
| c01/c02 Fußnoten-Tabelle (nach/vor dem Absatz) | 200; die vier Zelltexte aus der Quelle angehängt, Hinweis „Fußnoten: 4 Absätze …“ |
| c03 mehrdeutige Kopfzeilentabelle | 200; Tabelle lokal ohne Spaltenzuordnung, jeder Zelltext einmal, kein „Kopfzeile … angehängt“ |
| c04 Text in vMerge-Folgezelle | 200; Text in der Ursprungszelle mit Hinweis, in `word_notices` benannt |
| c05 Platzhaltertext im Dokument | 200; Text bleibt, Tabelle als HTML |
| c06 Lesezeichen-Kollision, `<a name>` als Text | 200; Literaltext gerendert sichtbar |
| c07 verschachtelt + verbunden, c08 Kopfvarianten verschachtelt, c09 Textfeldtabelle | 200; vollständig |
| c10 paketabsolutes Bildziel (`/word/media/…`), c11 Hauptteil unter `content/` | 200; 900-MP-Bild vor Writer durch Hinweis ersetzt |
| c12 `voll.docx` mit Hauptteil unter `content/` | 200; versteckter/gelöschter Text, Kommentare, externes Bild bereinigt |

Bewusste Kompatibilitätsänderung seit R4: `h20a` (unreferenzierter 950-MiB-Teil) war früher
konvertierbar und ist jetzt 413, weil das Paket das Gesamtbudget von 256 MiB überschreitet.

Grenze: Pixelbudget gilt für Rasterformate, deren Kopf Pillow liest. WMF ohne placeable Header
bleibt unbilanziert, bei EMF misst Pillow den Rahmen, nicht eingebettete Rasterbilder. Eine
Speichergarantie für WMF/EMF ist damit **nicht** belegt.
