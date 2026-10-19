"""Real synthetic Word input, through the adapter used by /document/convert."""

import base64
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.privacy_service import document_converter as dc
from src.privacy_service.adapters import build_default_chain


@pytest.mark.skipif(not shutil.which("libreoffice"), reason="requires LibreOffice")
def test_docx_preserves_markdown_blocks_and_hard_break():
    content = (Path(__file__).parents[1] / "fixtures/docx/paragraphs.docx").read_bytes()
    result = build_default_chain().convert(content, "paragraphs.docx")
    markdown = result.markdown.strip()
    blocks = markdown.split("\n\n")
    for number in range(1, 6):
        expected = f"Absatz {number}: Dies ist ein eigenstaendiger Absatz."
        assert expected in [" ".join(block.split()) for block in blocks]
    assert re.search(r"^#+ Ueberschrift$", markdown, re.MULTILINE)
    assert re.search(
        r"^[*+-] Erster Punkt\n[*+-] Zweiter Punkt$", markdown, re.MULTILINE
    )
    assert "Vor dem Umbruch  \nNach dem Umbruch" in markdown
    assert re.search(
        r"\| Name\s*\| Wert\s*\|\n\|[ -]+\|[ -]+\|\n\| Alpha\s*\| 42\s*\|", markdown
    )
    assert result.fmt == "docx"
    assert result.metadata["original_size_bytes"] == len(content)
    assert "1. Nummer Eins\n2. Nummer Zwei" in markdown
    assert result.images
    for filename, image in result.images.items():
        assert f"]({filename})" in markdown
        assert base64.b64decode(image).startswith(b"\x89PNG")


def _export_html(
    monkeypatch, html, *, image=None, exit_code=0, write=True, bind=False
):
    """Stand in for Writer with a hand-written export.

    Such an export carries none of the paragraph sentinels Writer writes, so
    it cannot be bound to the converted document; tests using it are about
    the HTML-to-Markdown steps and take it as it is (``bind`` reconciles it
    anyway: a stub then stands for an export that lost every mark).
    """
    from src.privacy_service import word_provenance as wp

    monkeypatch.setattr(dc.shutil, "which", lambda name: "/test/libreoffice")
    if not bind:
        monkeypatch.setattr(wp, "apply_provenance", lambda soup, body, provenance: [])

    def run(command, **kwargs):
        root = Path(command[command.index("--outdir") + 1])
        assert kwargs["timeout"] == 300
        assert any(arg.startswith("-env:UserInstallation=file:") for arg in command)
        if write:
            (root / "input.html").write_text(html)
        if image is not None:
            (root / "figure.png").write_bytes(image)
        return SimpleNamespace(returncode=exit_code, stderr=b"export error", stdout=b"")

    monkeypatch.setattr(dc.subprocess, "run", run)


def test_word_html_keeps_inline_spaces_images_and_repairs_mojibake(monkeypatch):
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
    )
    _export_html(
        monkeypatch,
        """<html><body>
      <p><b>Ein</b> <i>Wort</i></p>
      <p>PrÃ¼fung GebÃ¤ude gemÃ¤ÃŸ</p>
      <ol><li><p>Eins</p></li><li><p>Zwei</p></li></ol>
      <p>Vorher<br/>\nNachher</p><p>Danach</p>
      <p><img src="figure.png" alt="Bild"/></p>
    </body></html>""",
        image=image,
    )
    markdown, _, images = dc.convert_docx_bytes(
        (Path(__file__).parents[1] / "fixtures/docx/paragraphs.docx").read_bytes()
    )
    assert "**Ein** *Wort*" in markdown
    assert "Prüfung Gebäude gemäß" in markdown
    assert "1. Eins\n2. Zwei" in markdown
    assert "Vorher  \nNachher\n\nDanach" in markdown
    assert "![Bild](figure.png)" in markdown
    assert base64.b64decode(images["figure.png"]) == image


@pytest.mark.parametrize("exit_code,write", [(1, True), (0, False)])
def test_word_export_failure_is_loud(monkeypatch, exit_code, write):
    _export_html(monkeypatch, "<body></body>", exit_code=exit_code, write=write)
    with pytest.raises(RuntimeError, match="Word HTML conversion failed"):
        dc.convert_docx_bytes(
            (Path(__file__).parents[1] / "fixtures/docx/paragraphs.docx").read_bytes()
        )


@pytest.mark.parametrize("source", ["../outside.png", "missing.png"])
def test_word_does_not_read_external_or_missing_images(monkeypatch, source):
    _export_html(monkeypatch, f'<body><img src="{source}"/></body>')
    with pytest.raises(RuntimeError, match="not a local export artifact"):
        dc.convert_docx_bytes(
            (Path(__file__).parents[1] / "fixtures/docx/paragraphs.docx").read_bytes()
        )


def test_external_image_is_visible_notice_without_fetch(monkeypatch):
    _export_html(
        monkeypatch, '<body><img src="https://example.invalid/image.png"/></body>'
    )
    markdown, _, images = dc.convert_docx_bytes(
        (Path(__file__).parents[1] / "fixtures/docx/paragraphs.docx").read_bytes()
    )
    assert "Extern verlinktes Bild nicht geladen" in markdown
    assert images == {}
