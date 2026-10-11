"""BR20: Prüfer und Korrektor sehen den Rechenstand der geprüften Fassung."""

import json
import re
from pathlib import Path

import httpx
import pytest

from src.erkunder import rechenstand as rechenstand_modul
from src.erkunder.leitstand import StepFailed
from src.erkunder.models import Auftrag, Pruefpunkt
from src.erkunder.prompts import NACHWEIS, korrektur_prompt, pruefung_prompt
from src.erkunder.pruefkreis import _artifact, pruefumfang, zahlenbelege
from src.erkunder.rechenstand import rechenstand
from tests.erkunder.test_leitstand import finish, setup


def _schnappschuss(places):
    """Record each step folder as the place finds it when the step starts."""
    seen = {}
    original = places.__class__.__call__

    async def transport(request):
        if request.method == "POST" and request.url.path == "/schritt":
            data = json.loads(request.content)
            folder = Path(data["ordner"])
            seen[data["schritt"]] = {
                "prompt": data["prompt"],
                "dateien": {
                    p.relative_to(folder).as_posix(): p.read_bytes()
                    for p in sorted(folder.rglob("*"))
                    if p.is_file()
                },
            }
        return await original(places, request)

    return seen, transport


async def _mit_schnappschuss(tmp_path, **kwargs):
    service, places, owners = await setup(tmp_path, **kwargs)
    seen, transport = _schnappschuss(places)
    await service.client.aclose()
    service.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    return service, places, owners, seen


async def test_pruefer_und_korrektor_sehen_den_rechenstand(tmp_path):
    service, _, owners, seen = await _mit_schnappschuss(tmp_path, review="Widerspruch")
    try:
        status = await finish(service)
        assert status["zustand"] == "fertig"
        root = service.directory("bericht-123")
        for review, report in (
            ("pruefung", "harmonisierung"),
            ("pruefung-korrektur", "harmonisierung-korrektur"),
        ):
            files = seen[review]["dateien"]
            for name in ("skripte/rechnung.py", "skripte/zahlen.json"):
                assert (
                    files["rechenstand/" + name] == (root / report / name).read_bytes()
                )
            assert "rechenstand/skripte/" in seen[review]["prompt"]
            assert "Rechenstand fehlt" not in files["maschinenbefunde.json"].decode()
        # The corrector continues on the previous version's calculation.
        files = seen["harmonisierung-korrektur"]["dateien"]
        assert (
            files["skripte/zahlen.json"]
            == (root / "harmonisierung/skripte/zahlen.json").read_bytes()
        )
        assert files["skripte/rechnung.py"] == b"# synthetic calculation"
        assert (
            "Rechenstand deiner Vorfassung"
            in seen["harmonisierung-korrektur"]["prompt"]
        )
        # Nested inputs belong to the place like every other input.
        owned = {Path(path).relative_to(root).as_posix() for path, *_ in owners}
        assert {
            "pruefung/rechenstand",
            "pruefung/rechenstand/skripte",
            "pruefung/rechenstand/skripte/zahlen.json",
            "harmonisierung-korrektur/skripte",
        } <= owned
        result = service.result("bericht-123")
        assert not any(
            k.startswith("rechenstand/") for k in result["skripte"]["pruefung"]
        )
    finally:
        await service.shutdown()


async def test_fehlender_rechenstand_ist_laut(tmp_path):
    service, _, _, seen = await _mit_schnappschuss(tmp_path)
    original = service.step

    async def step(ident, name, slot, prompt, inputs=None):
        await original(ident, name, slot, prompt, inputs)
        if name == "harmonisierung":
            for path in (service.directory(ident) / "harmonisierung/skripte").iterdir():
                path.unlink()

    service.step = step
    try:
        status = await finish(service)
        assert status["zustand"] == "fertig"
        files = seen["pruefung"]["dateien"]
        findings = json.loads(files["maschinenbefunde.json"])
        assert (
            "Rechenstand fehlt: harmonisierung/skripte/ enthält keine Dateien"
            in findings
        )
        assert not any(name.startswith("rechenstand/") for name in files)
        assert "rechenstand/" not in seen["pruefung"]["prompt"]
        # The gap is an open finding: the corrector receives it and can repair it.
        assert (
            "Rechenstand fehlt: harmonisierung/skripte/ enthält keine Dateien"
            in json.loads(
                seen["harmonisierung-korrektur"]["dateien"]["maschinenbefunde.json"]
            )
        )
        assert status["meta"]["korrekturrunden"] == 1
    finally:
        await service.shutdown()


def test_rechenstand_gleiche_grenzen_wie_die_uebrigen_eingaenge(tmp_path, monkeypatch):
    report = tmp_path / "harmonisierung"
    assert rechenstand(report, "rechenstand/") == (
        {},
        ["Rechenstand fehlt: harmonisierung/skripte/ ist kein Ordner der Fassung"],
    )
    (report / "skripte/teil/__pycache__").mkdir(parents=True)
    (report / "skripte/teil/__pycache__/x.pyc").write_bytes(b"\0")
    (report / "skripte/rechnen.py").write_text("print(1)")
    (report / "skripte/teil/ergebnisse.json").write_text("{}")
    (report / "skripte/gross.csv").write_bytes(b"x" * 50)
    (report / "skripte/daten").symlink_to(tmp_path)
    monkeypatch.setattr(rechenstand_modul, "MAX_RECHENSTAND_BYTES", 20)
    files, findings = rechenstand(report, "rechenstand/")
    assert files == {
        "rechenstand/skripte/rechnen.py": b"print(1)",
        "rechenstand/skripte/teil/ergebnisse.json": b"{}",
    }
    assert findings == [
        "Rechenstand nicht übergeben: skripte/daten (Verknüpfung)",
        "Rechenstand nicht übergeben: skripte/gross.csv (Gesamtgröße überschritten)",
    ]
    monkeypatch.setattr(
        rechenstand_modul,
        "read_bytes",
        lambda path: (_ for _ in ()).throw(ValueError("ergebnis zu gross")),
    )
    assert (
        "Rechenstand nicht übergeben: skripte/rechnen.py (ergebnis zu gross)"
        in rechenstand(report, "")[1]
    )


@pytest.mark.parametrize("name", ["../flucht.json", "/abs.json", "a/../../b"])
async def test_eingang_ausserhalb_des_schrittordners_bricht_laut_ab(tmp_path, name):
    service, _, _ = await setup(tmp_path)
    try:
        directory = tmp_path / "schritt"
        directory.mkdir()
        with pytest.raises(StepFailed, match="eingang-pfad"):
            service.input_path(directory, "pruefung", name, 0)
    finally:
        await service.shutdown()


def test_pfadkonvention_prompt_und_regel_sagen_dasselbe(tmp_path):
    example = json.loads(re.search(r'`(\{"skript": [^`]+\})`', NACHWEIS).group(1))
    for key in ("skript", "ergebnis"):
        target = tmp_path / example[key]
        target.parent.mkdir(exist_ok=True)
        target.write_text("{}")
        assert _artifact(tmp_path, example[key]) == "{}"
    with pytest.raises(ValueError, match="'rechnen.py'.*beginnen mit skripte/"):
        _artifact(tmp_path, "rechnen.py")


def test_nachweis_mit_skripte_praefix_wird_abgeglichen(tmp_path):
    (tmp_path / "skripte").mkdir()
    (tmp_path / "skripte/rechnen.py").write_text("print(14)")
    (tmp_path / "skripte/ergebnisse.json").write_text(
        json.dumps(
            {
                "n": {
                    "wert": 14,
                    "einheit": "Starts",
                    "raster": "1 min",
                    "auswahl": "alle",
                    "quelle": "messdaten/a.parquet",
                    "kanaele": ["p"],
                }
            }
        )
    )
    block = {
        "kanaele": {"p": "pump"},
        "ergebnisdateien": [
            {"skript": "skripte/rechnen.py", "ergebnis": "skripte/ergebnisse.json"}
        ],
    }
    text = "[14](zahl:n)\n```erkunder-nachweis\n" + json.dumps(block) + "\n```\n"
    assert zahlenbelege(tmp_path, text, {"messdaten/a.parquet": ["pump"]}) == (
        [],
        {
            "skripte/rechnen.py": "print(14)",
            "skripte/ergebnisse.json": (
                tmp_path / "skripte/ergebnisse.json"
            ).read_text(),
        },
    )


def _urteil(status, missing, begruendung="Zeitraum enthält keinen Heizbetrieb"):
    return {
        "anlage": "kessel",
        "dokument_id": "kw-a",
        "fehlerbild_id": "f1",
        "status": status,
        "fehlende_kanaele": missing,
        "befund_verweis": None,
        "sicherheit": 0.5,
        "begruendung": begruendung,
    }


def _pruefe(value, missing):
    point = Pruefpunkt(
        anlage="kessel",
        dokument_id="kw-a",
        fehlerbild_id="f1",
        fehlende_kanaele=missing,
    )
    return pruefumfang(
        "```erkunder-pruefumfang\n" + json.dumps([value]) + "\n```", [point]
    )


def test_nicht_pruefbar_ohne_fehlenden_kanal_mit_begruendung_wird_angenommen():
    assert _pruefe(_urteil("nicht_pruefbar", []), []) == []
    assert _pruefe(_urteil("nicht_pruefbar", [], begruendung="  "), []) == [
        "Bibliotheksurteil ohne Begründung"
    ]


def test_fehlender_kanal_verlangt_weiter_teilweise_oder_nicht_pruefbar():
    assert _pruefe(
        _urteil("widerlegt", ["mischer_stellsignal"]), ["mischer_stellsignal"]
    ) == ["Vollständiges Urteil trotz fehlender Kanäle"]
    assert (
        _pruefe(_urteil("teilweise", ["mischer_stellsignal"]), ["mischer_stellsignal"])
        == []
    )


def test_statusregeln_stehen_bei_erkunder_und_pruefer(auftrag):
    a = Auftrag.model_validate(auftrag)
    for text in (NACHWEIS, pruefung_prompt(a)):
        assert (
            "Fehlt einem Punkt eine Messung, ist das Urteil "
            "`teilweise` oder `nicht_pruefbar`"
            in text
        )
        assert "`nicht_pruefbar` passt, wann immer" in text
    assert "Nicht prüfbare Punkte benennen die fehlende" not in NACHWEIS
    assert "rechenstand/" not in pruefung_prompt(a)
    assert "Rechenstand deiner Vorfassung" not in korrektur_prompt(a)
    assert "rechenstand/skripte/" in pruefung_prompt(a, True)
    assert "Rechenstand deiner Vorfassung" in korrektur_prompt(a, True)
