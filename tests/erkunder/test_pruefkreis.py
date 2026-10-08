"""Real Coordinator steps with synthetic script outputs and changing reviewers."""
import json

import pytest

from src.erkunder.pruefkreis import pruefurteil, zahlenbelege
from tests.erkunder.test_leitstand import body, finish, setup


def evidence(tmp_path, shown="11", value=14, source="messdaten/test.parquet"):
    scripts = tmp_path / "skripte"
    scripts.mkdir(exist_ok=True)
    (scripts / "calculate.py").write_text("print(14)")
    (scripts / "values.json").write_text(json.dumps({"starts": {
        "wert": value, "einheit": "Starts", "raster": "1 min", "auswahl": "rising edge >= 0.5",
        "quelle": source, "kanaele": ["p"],
    }}))
    return f"Starts [{shown}](zahl:starts)\n```erkunder-nachweis\n" + json.dumps({
        "kanaele": {"p": "pump_signal"}, "ergebnisdateien": [{
            "skript": "skripte/calculate.py", "ergebnis": "skripte/values.json",
        }],
    }) + "\n```"


def test_number_mismatch_and_rounding(tmp_path):
    text = evidence(tmp_path)
    findings, artifacts = zahlenbelege(tmp_path, text, {"messdaten/test.parquet"})
    assert "Text 11, Skript 14" in findings[0]
    assert "skripte/values.json" in artifacts
    assert zahlenbelege(tmp_path, evidence(tmp_path, "0,0034", 0.00343))[0] == []
    assert zahlenbelege(tmp_path, evidence(tmp_path, "0,0014", 0.00343))[0]


def test_wrong_source_and_missing_selection(tmp_path):
    text = evidence(tmp_path, "14", source="messdaten/foreign.parquet")
    assert "Eingangsmanifest" in zahlenbelege(tmp_path, text, {"messdaten/test.parquet"})[0][0]
    assert zahlenbelege(tmp_path, "No evidence")[0]


def test_explicit_review_no_word_matching():
    assert pruefurteil('trägt teilweise ist hier nur ein Zitat.\n```erkunder-pruefung\n{"befunde": []}\n```') == []
    with pytest.raises(ValueError):
        pruefurteil("trägt")


async def test_more_than_one_correction_and_upper_bound(tmp_path):
    service, places, _ = await setup(tmp_path, review="Widerspruch")
    order = body()
    order.auftrag.korrekturkreis = 3
    try:
        status = await finish(service, order)
        assert status["zustand"] == "fertig"
        assert places.attempts["pruefung-korrektur-3"] == 1
        assert status["meta"]["offene_befunde_anzahl"] == 1
        assert status["meta"]["korrekturrunden"] == 3
        result = service.result("bericht-123")
        assert result["gutachten_final"].startswith("harmonisierung-korrektur-3")
        assert "Offene Befunde nach Korrekturgrenze" in result["gutachten_final"]
        assert "skripte/zahlen.json" in result["skripte"]["harmonisierung-korrektur-3"]
    finally:
        await service.shutdown()


async def test_second_review_can_finish_without_last_correction(tmp_path):
    service, places, _ = await setup(tmp_path, review="Zahl falsch")
    original = service.step

    async def step(ident, name, slot, prompt, inputs=None):
        if name == "pruefung-korrektur-2":
            places.review = "trägt"
        await original(ident, name, slot, prompt, inputs)

    service.step = step
    order = body()
    order.auftrag.korrekturkreis = 3
    try:
        status = await finish(service, order)
        assert status["meta"]["offene_befunde_anzahl"] == 0
        assert status["meta"]["korrekturrunden"] == 2
        assert "harmonisierung-korrektur-3" not in places.attempts
    finally:
        await service.shutdown()


def test_unlinked_number_identified_by_independent_reviewer_is_compared(tmp_path):
    from src.erkunder.pruefkreis import prueferzahlen

    report = evidence(tmp_path, "14") + " Außerdem gab es 11 Starts."
    review = "```erkunder-zahlenpruefung\n" + json.dumps({"vollstaendig": True, "zahlen": [
        {"zitat": "Außerdem gab es 11 Starts.", "zahl": "11", "id": "starts"},
    ]}) + "\n```"
    assert "Text 11, Skript 14" in prueferzahlen(tmp_path, report, review, {"messdaten/test.parquet"})[0]


async def test_reviewer_cannot_change_calculation_evidence(tmp_path):
    service, places, _ = await setup(tmp_path)
    original = service.step

    async def step(ident, name, slot, prompt, inputs=None):
        await original(ident, name, slot, prompt, inputs)
        if name == "pruefung":
            path = service.directory(ident) / "harmonisierung/skripte/zahlen.json"
            content = json.loads(path.read_text())
            content["n"]["wert"] = 11
            path.write_text(json.dumps(content))

    service.step = step
    try:
        status = await finish(service)
        assert status["zustand"] == "abbruch"
        assert "Nachweis-Integritaet" in status["fehler"]
    finally:
        await service.shutdown()


@pytest.mark.parametrize("defect", ["missing_reference_field", "no_missing_channel", "extra_field"])
def test_judgment_contract_failure_enters_correction(defect):
    from src.erkunder.models import Pruefpunkt
    from src.erkunder.pruefkreis import pruefumfang

    point = Pruefpunkt(anlage="pump", dokument_id="kw-stoerung-pump", fehlerbild_id="f1", fehlende_kanaele=[])
    value = {"anlage": point.anlage, "dokument_id": point.dokument_id, "fehlerbild_id": "f1",
             "status": "widerlegt", "fehlende_kanaele": [], "befund_verweis": None,
             "sicherheit": 0.9, "begruendung": "Synthetic evidence"}
    if defect == "missing_reference_field":
        del value["befund_verweis"]
    elif defect == "no_missing_channel":
        value["status"] = "nicht_pruefbar"
    else:
        value["invented"] = True
    report = "```erkunder-pruefumfang\n" + json.dumps([value]) + "\n```"
    assert pruefumfang(report, [point])


def test_empty_scope_requires_explicit_empty_judgment_block():
    from src.erkunder.pruefkreis import pruefumfang

    assert pruefumfang("report without judgment", [])
    assert pruefumfang("```erkunder-pruefumfang\n[]\n```", []) == []


@pytest.mark.parametrize("corrected", [False, True])
async def test_legacy_completed_report_survives_restart_without_false_clearance(tmp_path, corrected):
    import httpx

    from src.erkunder.leitstand import Coordinator
    from src.erkunder.models import Ergebnis
    from tests.erkunder.test_leitstand import Places

    service, _, _ = await setup(tmp_path, review="offen" if corrected else "trägt")
    await finish(service)
    state = service.states["bericht-123"]
    original = service.output("bericht-123", state["gutachten_schritt"])
    for field in ("gutachten_schritt", "pruefung_schritt", "offene_befunde", "korrekturrunden",
                  "nachweise", "nachweis_hashes"):
        state.pop(field)
    state["meta"]["prompt_version"] = "erkunder-prompts/1"
    for field in ("offene_befunde_anzahl", "pruefstatus", "korrekturrunden"):
        state["meta"].pop(field)
    service.save("bericht-123")
    await service.shutdown()
    resumed = Coordinator(service.root, client=httpx.AsyncClient(transport=httpx.MockTransport(Places())),
                          chown=lambda *args: None)
    await resumed.startup()
    try:
        assert resumed.result("bericht-123")["gutachten_final"] == original
        metadata = Ergebnis.model_validate(resumed.status("bericht-123")["meta"])
        assert metadata.pruefstatus == "altauftrag_ungeprueft"
        assert metadata.offene_befunde_anzahl is None
    finally:
        await resumed.shutdown()
