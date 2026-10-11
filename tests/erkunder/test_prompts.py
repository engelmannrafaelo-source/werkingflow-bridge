from src.erkunder.models import Auftrag
from src.erkunder.prompts import (
    PROMPT_VERSION,
    erkunder_prompt,
    harmonisierung_prompt,
    korrektur_prompt,
    pruefung_prompt,
)


def test_prompts(auftrag):
    a = Auftrag.model_validate(auftrag)
    for prompt in [
        erkunder_prompt(a),
        harmonisierung_prompt(a, []),
        pruefung_prompt(a),
        korrektur_prompt(a),
    ]:
        assert a.gegenstand in prompt and a.zweck in prompt
        assert f"/arbeit/{a.bericht_id}/eingang/" in prompt
        assert "2026-01-01" in prompt
        assert "vertiefung.md" not in prompt
        assert "zusätzlich fragt" not in prompt
        assert "{gegenstand}" not in prompt
    assert PROMPT_VERSION == "erkunder-prompts/4"
    assert "ausgefallen" not in harmonisierung_prompt(a, [])
    assert "trägt / trägt teilweise / trägt nicht" in pruefung_prompt(a)
    assert "Ersatzgröße" in pruefung_prompt(a)


def test_optional_context(auftrag):
    auftrag.update(auftrag="Warum taktet die Pumpe?", vertiefung_md="Pumpe vertiefen")
    a = Auftrag.model_validate(auftrag)
    assert "zusätzlich fragt er: Warum taktet die Pumpe?" in erkunder_prompt(a)
    assert "vertiefung.md" in erkunder_prompt(a)
    assert (
        "Einer der drei Erkunder ist ausgefallen (zeit); dir liegen zwei Gutachten vor."
        in (harmonisierung_prompt(a, [{"schritt": "erkunder-3", "grund": "zeit"}]))
    )


def test_pruefwissen_satz_nur_mit_pruefwissen(auftrag):
    ohne = Auftrag.model_validate(auftrag)
    assert "pruefwissen/" not in erkunder_prompt(ohne)
    auftrag["dateien"] = [{
        "ziel": "pruefwissen/kw-thema-a.md", "url": "https://example.test/x",
        "sha256": "a" * 64, "bytes": 1,
    }]
    a = Auftrag.model_validate(auftrag)
    for prompt in [erkunder_prompt(a), harmonisierung_prompt(a, []), pruefung_prompt(a),
                   korrektur_prompt(a)]:
        assert "In `pruefwissen/` liegen ausgewählte Fachdokumente" in prompt
