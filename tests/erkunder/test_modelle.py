import pytest
from pydantic import ValidationError

from src.erkunder.models import Auftrag


def test_valid(auftrag):
    assert (
        Auftrag.model_validate(auftrag).model_dump(mode="json", by_alias=True)
        == {**auftrag, "pruefliste": []}
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bericht_id", "short"),
        ("bericht_id", "A" * 8),
        ("bericht_id", "a" * 81),
        ("bericht_id", "abcdefgh/"),
        ("schema", "erkunder-auftrag/2"),
        ("korrekturkreis", 0),
        ("korrekturkreis", 6),
        ("korrekturkreis", True),
        ("unknown", "x"),
    ],
)
def test_invalid(auftrag, field, value):
    auftrag[field] = value
    with pytest.raises(ValidationError):
        Auftrag.model_validate(auftrag)


@pytest.mark.parametrize(
    "ziel",
    [
        "../x",
        "messdaten/..x",
        "messdaten/a/b",
        "other/a",
        "messdaten/",
        "plan/" + "a" * 201,
    ],
)
def test_invalid_target(auftrag, ziel):
    auftrag["dateien"] = [
        {"ziel": ziel, "url": "https://example.test/x", "sha256": "a" * 64, "bytes": 1}
    ]
    with pytest.raises(ValidationError):
        Auftrag.model_validate(auftrag)


@pytest.mark.parametrize(
    "field,value", [("sha256", "bad"), ("bytes", -1), ("extra", 1)]
)
def test_invalid_file(auftrag, field, value):
    file = {
        "ziel": "messdaten/x",
        "url": "https://example.test/x",
        "sha256": "a" * 64,
        "bytes": 1,
        field: value,
    }
    auftrag["dateien"] = [file]
    with pytest.raises(ValidationError):
        Auftrag.model_validate(auftrag)


def test_modell_vorgabe_ist_sonnet(auftrag):
    assert Auftrag.model_validate(auftrag).modell == "claude-sonnet-5-5"


def test_modell_haiku_zugelassen(auftrag):
    auftrag["modell"] = "claude-haiku-5-5"
    assert Auftrag.model_validate(auftrag).modell == "claude-haiku-5-5"


def test_modell_unbekannt_abgewiesen(auftrag):
    auftrag["modell"] = "claude-opus-5-5"
    with pytest.raises(ValidationError):
        Auftrag.model_validate(auftrag)
