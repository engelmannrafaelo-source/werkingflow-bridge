"""Regenerate paragraphs.docx with python-docx; no customer document required."""

import base64
import io
from pathlib import Path

from docx import Document

document = Document()
for number in range(1, 6):
    document.add_paragraph(f"Absatz {number}: Dies ist ein eigenstaendiger Absatz.")
document.add_heading("Ueberschrift", 1)
for text in ("Erster Punkt", "Zweiter Punkt"):
    document.add_paragraph(text, style="List Bullet")
paragraph = document.add_paragraph("Vor dem Umbruch")
paragraph.add_run().add_break()
paragraph.add_run("Nach dem Umbruch")
table = document.add_table(rows=2, cols=2)
for cell, text in zip(
    [cell for row in table.rows for cell in row.cells], ["Name", "Wert", "Alpha", "42"]
):
    cell.text = text
for text in ("Nummer Eins", "Nummer Zwei"):
    document.add_paragraph(text, style="List Number")
document.add_picture(
    io.BytesIO(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
        )
    )
)
document.save(Path(__file__).with_name("paragraphs.docx"))
