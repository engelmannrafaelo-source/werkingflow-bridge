"""filter_content with a cache boundary marker (BRTRUNC2 item 6, from CACHE4R).

filter_content collapses newline runs and strips, and it runs BEFORE
split_prompt cuts the marker out. A marker inside a run of three newlines or
after a trailing newline broke the run / the strip: the CLI then got one
newline more than without a boundary (CACHE4R: 18 of 61 Energy cases —
"source at the end", "only sources", "empty parts"). Promise of CACHE2: the
text with a boundary is byte-identical to the text without one.
"""

from __future__ import annotations

import itertools

import pytest

from src.cache_boundary import SENTINEL, STATUS_APPLIED, split_prompt
from src.message_adapter import MessageAdapter


def _ohne_grenze(text):
    return MessageAdapter.filter_content(text)


def _mit_grenze(text, pos):
    return split_prompt(
        MessageAdapter.filter_content(text[:pos] + SENTINEL + text[pos:]), True
    )


# The CACHE4R shapes: a source that ends on "\n", then "\n\nAufgabe"; source at
# the end; only sources.
_FAELLE = {
    "quelle_vor_aufgabe": (
        "<QUELLE>\nText der Quelle\n",
        "\n\nAufgabe: fasse zusammen.",
    ),
    "quelle_am_ende": ("Aufgabe: fasse zusammen.\n\n<QUELLE>\nText\n", "\n"),
    "nur_quellen": ("<Q1>\nA\n</Q1>\n", "\n\n\n"),
    "leerer_schlussteil": ("Auftrag\n\n<Q>\nB\n", ""),
}


@pytest.mark.parametrize("name", sorted(_FAELLE))
def test_cache4r_faelle_bytegleich(name):
    vorn, hinten = _FAELLE[name]
    text = vorn + hinten
    prompt, at, status = _mit_grenze(text, len(vorn))
    assert status == STATUS_APPLIED
    assert prompt == _ohne_grenze(text)
    assert 0 < at <= len(prompt)


def _sichtbar(text):
    return "".join(ch for ch in text if not ch.isspace())


def test_jede_stelle_jeder_text_bytegleich():
    """Every boundary position in short texts built from the characters that
    matter (newlines, spaces, letters): with boundary == without, byte for byte,
    and the boundary stays between the same visible characters."""
    alphabet = ["\n", " ", "a", "b"]
    for laenge in range(1, 8):
        for zeichen in itertools.product(alphabet, repeat=laenge):
            text = "".join(zeichen)
            if not text.strip():
                continue
            erwartet = _ohne_grenze(text)
            for pos in range(len(text) + 1):
                gefiltert = MessageAdapter.filter_content(
                    text[:pos] + SENTINEL + text[pos:]
                )
                assert gefiltert.count(SENTINEL) == 1, (text, pos)
                assert gefiltert.replace(SENTINEL, "") == erwartet, (text, pos)
                vor = gefiltert[: gefiltert.index(SENTINEL)]
                assert _sichtbar(vor) == _sichtbar(text[:pos]), (text, pos)


def test_grenze_bleibt_an_ihrer_stelle():
    vorn = "Stabiler Teil\n\n\n\n"
    prompt, at, _ = _mit_grenze(vorn + "Wechselnder Teil", len(vorn))
    assert prompt == "Stabiler Teil\n\nWechselnder Teil"
    assert prompt[:at] in ("Stabiler Teil", "Stabiler Teil\n", "Stabiler Teil\n\n")
    assert prompt[at:].endswith("Wechselnder Teil")


def test_ohne_marke_unveraendert():
    text = "  a\n\n\n\nb \n"
    assert MessageAdapter.filter_content(text) == "a\n\nb"


def test_doppelte_marke_bleibt_laut():
    text = "a" + SENTINEL + "\n\n\nb" + SENTINEL
    gefiltert = MessageAdapter.filter_content(text)
    assert gefiltert.count(SENTINEL) == 2
    _, at, status = split_prompt(gefiltert, True)
    assert at is None and status.startswith("lost:")
