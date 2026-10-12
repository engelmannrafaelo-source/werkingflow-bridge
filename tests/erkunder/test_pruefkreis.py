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
    }) + "\n```\n"


def test_number_mismatch_and_rounding(tmp_path):
    text = evidence(tmp_path)
    findings, artifacts = zahlenbelege(tmp_path, text, {"messdaten/test.parquet": ["pump_signal"]})
    assert "Text '11', Skript 14" in findings[0]
    assert "skripte/values.json" in artifacts
    sources = {"messdaten/test.parquet": ["pump_signal"]}
    assert zahlenbelege(tmp_path, evidence(tmp_path, "0,0034", 0.00343), sources)[0] == []
    assert zahlenbelege(tmp_path, evidence(tmp_path, "0,0014", 0.00343), {"messdaten/test.parquet": ["pump_signal"]})[0]


def test_wrong_source_and_missing_selection(tmp_path):
    text = evidence(tmp_path, "14", source="messdaten/foreign.parquet")
    assert "Eingangsmanifest" in zahlenbelege(tmp_path, text, {"messdaten/test.parquet": ["pump_signal"]})[0][0]
    assert zahlenbelege(tmp_path, "No evidence", {})[0]


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
    findings = prueferzahlen(tmp_path, report, review, {"messdaten/test.parquet": ["pump_signal"]})
    assert "Text '11', Skript 14" in findings[0]


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


@pytest.mark.parametrize("defect", ["missing_reference_field", "extra_field"])
def test_judgment_contract_failure_enters_correction(defect):
    from src.erkunder.models import Pruefpunkt
    from src.erkunder.pruefkreis import pruefumfang

    point = Pruefpunkt(anlage="pump", dokument_id="kw-stoerung-pump", fehlerbild_id="f1", fehlende_kanaele=[])
    value = {"anlage": point.anlage, "dokument_id": point.dokument_id, "fehlerbild_id": "f1",
             "status": "widerlegt", "fehlende_kanaele": [], "befund_verweis": None,
             "sicherheit": 0.9, "begruendung": "Synthetic evidence"}
    if defect == "missing_reference_field":
        del value["befund_verweis"]
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


@pytest.mark.parametrize("channel", [None, "", "invented", 42, ["pump_signal"], {}])
def test_alias_requires_real_manifest_channel(tmp_path, channel):
    report = evidence(tmp_path, "14").replace('"pump_signal"', json.dumps(channel))
    findings, _ = zahlenbelege(tmp_path, report, {"messdaten/test.parquet": ["pump_signal"]})
    assert findings and "Messkanal im Quellenmanifest" in findings[0]


def test_real_channel_in_wrong_source_is_rejected(tmp_path):
    report = evidence(tmp_path, "14")
    sources = {"messdaten/test.parquet": ["other"], "messdaten/second.parquet": ["pump_signal"]}
    assert "nicht in Quelldatei" in zahlenbelege(tmp_path, report, sources)[0][0]


def test_empty_reviewer_list_cannot_approve_wrong_linked_numbers(tmp_path):
    from src.erkunder.pruefkreis import prueferzahlen

    # Links gleicht die Maschine selbst ab; der Prüfer muss sie nicht abschreiben.
    report = evidence(tmp_path, "11")
    review = '```erkunder-zahlenpruefung\n{"vollstaendig":true,"zahlen":[]}\n```'
    sources = {"messdaten/test.parquet": ["pump_signal"]}
    assert prueferzahlen(tmp_path, report, review, sources) == []
    assert "Text '11', Skript 14" in zahlenbelege(tmp_path, report, sources)[0][0]


@pytest.mark.parametrize("shown", ["14", "14,0", "11"])
def test_every_linked_spelling_is_checked_by_machine(tmp_path, shown):
    report = evidence(tmp_path, "14") + f" Tabelle: [{shown}](zahl:starts)."
    findings, _ = zahlenbelege(
        tmp_path, report, {"messdaten/test.parquet": ["pump_signal"]}
    )
    assert bool(findings) is (shown == "11")


def test_unvollstaendig_ueberdeckt_den_zitatabgleich_nicht(tmp_path):
    from src.erkunder.pruefkreis import prueferzahlen

    report = evidence(tmp_path, "14") + " Außerdem gab es 11 Starts."
    item = {"zitat": "Außerdem gab es 11 Starts.", "zahl": "11", "id": "starts"}
    review = (
        "```erkunder-zahlenpruefung\n"
        + json.dumps({"vollstaendig": False, "zahlen": [item]})
        + "\n```"
    )
    findings = prueferzahlen(tmp_path, report, review, {"messdaten/test.parquet": ["pump_signal"]})
    assert findings[0].startswith("Zahlenprüfung unvollständig")
    assert "Text '11', Skript 14" in findings[1]


@pytest.mark.parametrize("defect", ["channel", "unlinked_number"])
async def test_machine_evidence_reaches_correction_loop(tmp_path, defect):
    service, places, _ = await setup(tmp_path)
    original = places.__class__.__call__
    # Modify the generated first report/review before Coordinator hashes it.
    async def transport(request):
        response = await original(places, request)
        if request.method == 'GET' and request.url.host != 'download.test':
            name = request.url.path.split('/')[-1]
            data = places.running.get(name)
            if data and name == ('harmonisierung' if defect == 'channel' else 'pruefung'):
                from pathlib import Path
                path = Path(data['ordner']) / ('ergebnis.md' if defect == 'channel' else 'pruefung.md')
                text = path.read_text()
                if defect == 'channel':
                    text = text.replace('"p": "pump"', '"p": null')
                else:
                    text = text.replace(
                        '[{"zitat": "[14](zahl:n)", "zahl": "14", "id": "n"}]',
                        '[{"zitat": "[14](zahl:n)", "zahl": "14", "id": "nirgends"}]',
                    )
                path.write_text(text)
        return response

    import httpx
    await service.client.aclose()
    service.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    try:
        status = await finish(service)
        assert status['zustand'] == 'fertig'
        assert status['meta']['korrekturrunden'] == 1
        assert status['meta']['offene_befunde_anzahl'] == 0
        path = service.directory('bericht-123') / 'harmonisierung-korrektur/maschinenbefunde.json'
        findings = json.loads(path.read_text())
        assert any(
            ("Messkanal" if defect == "channel" else "Zahl nirgends") in text
            for text in findings
        )
    finally:
        await service.shutdown()


# --- PKLESE: der Leitstand liest das Prüferurteil, wie es gemeint ist ---------------
# Formen aus DS3 Lauf 1 (Diagnose 99762fe4): Runde 0, 2 und 3 schrieben Objekte.
OBJEKTFORMEN = [
    {"id": "B1", "ort": "Abschnitt 4", "befund": "Auslastung über Ersatzgröße"},
    {
        "id": "P11",
        "ort": "B4",
        "art": "Bewertung",
        "text": "Risiko hoch unbelegt",
        "schwere": "mittel",
    },
    {
        "id": "P4",
        "stelle": "skripte/pruefe_links.py",
        "art": "Nachweis",
        "beschreibung": "liest ../ergebnis.md",
        "schwere": "gering",
    },
]


def _urteil(befunde):
    return (
        "Prüfung.\n```erkunder-pruefung\n"
        + json.dumps({"befunde": befunde}, ensure_ascii=False)
        + "\n```"
    )


def test_objekt_befunde_werden_als_befunde_gelesen():
    findings = pruefurteil(_urteil(OBJEKTFORMEN))
    assert findings == [
        "id: B1; ort: Abschnitt 4; befund: Auslastung über Ersatzgröße",
        "id: P11; ort: B4; art: Bewertung; text: Risiko hoch unbelegt; schwere: mittel",
        "id: P4; stelle: skripte/pruefe_links.py; art: Nachweis;"
        " beschreibung: liest ../ergebnis.md; schwere: gering",
    ]


def test_echter_offener_befund_bleibt_offen_leere_liste_traegt():
    assert pruefurteil(_urteil(["B7: Extrapolation ohne Hinweis"])) == [
        "B7: Extrapolation ohne Hinweis"
    ]
    assert pruefurteil(_urteil([])) == []


@pytest.mark.parametrize(
    "befund", [42, "", "  ", {}, {"id": None, "text": ""}, ["liste"]]
)
def test_unlesbarer_befund_ist_formbefund_nie_gruen(befund):
    from src.erkunder.pruefkreis import pruefform

    with pytest.raises(ValueError, match=r"befunde\[1\]"):
        pruefurteil(_urteil([befund]))
    assert pruefform(
        _urteil([befund])
        + '\n```erkunder-zahlenpruefung\n{"vollstaendig":true,"zahlen":[]}\n```'
    )


def test_pruefform_nennt_beide_bloecke():
    from src.erkunder.pruefkreis import pruefform

    assert len(pruefform("kein Block")) == 2
    good = (
        _urteil([])
        + '\n```erkunder-zahlenpruefung\n{"vollstaendig":false,"zahlen":[]}\n```'
    )
    assert (
        pruefform(good) == []
    )  # unvollständig ist ein lesbares Urteil, keine Formfrage


TABELLE = (
    "| Größe | Wert |\n| --- | --- |\n"
    "| Starts | [14](zahl:starts) |\n| Zuschaltungen | 11 |\n"
)


@pytest.mark.parametrize(
    "zitat",
    [
        # Tabellenzeile über den Umbruch
        "| Starts | [14](zahl:starts) | | Zuschaltungen |",
        "Starts |[14](zahl:starts)||Zuschaltungen",
        "Starts | [14](zahl:starts) |\n| Zuschaltungen",
    ],
)
def test_zitat_ueber_tabellen_zeilenumbruch_wird_gefunden(tmp_path, zitat):
    from src.erkunder.pruefkreis import fundstelle, prueferzahlen

    report = evidence(tmp_path, "14") + TABELLE
    assert (
        fundstelle(report, zitat)
        == report.splitlines().index("| Starts | [14](zahl:starts) |") + 1
    )
    review = (
        "```erkunder-zahlenpruefung\n"
        + json.dumps(
            {
                "vollstaendig": True,
                "zahlen": [
                    {"zitat": "[14](zahl:starts)", "zahl": "14", "id": "starts"},
                    {"zitat": zitat, "zahl": "14", "id": "starts"},
                ],
            }
        )
        + "\n```"
    )
    assert (
        prueferzahlen(
            tmp_path, report, review, {"messdaten/test.parquet": ["pump_signal"]}
        )
        == []
    )


def test_fliesstext_zitat_ueber_zeilenumbruch_und_fundstelle_in_meldung(tmp_path):
    from src.erkunder.pruefkreis import prueferzahlen

    report = evidence(tmp_path, "14") + "Außerdem gab es\n11 Starts."
    review = (
        "```erkunder-zahlenpruefung\n"
        + json.dumps(
            {
                "vollstaendig": True,
                "zahlen": [
                    {"zitat": "[14](zahl:starts)", "zahl": "14", "id": "starts"},
                    {
                        "zitat": "Außerdem gab es 11 Starts.",
                        "zahl": "11",
                        "id": "starts",
                    },
                ],
            }
        )
        + "\n```"
    )
    findings = prueferzahlen(
        tmp_path, report, review, {"messdaten/test.parquet": ["pump_signal"]}
    )
    line = report.splitlines().index("Außerdem gab es") + 1
    assert findings == [
        f"Zahl starts: Text '11', Skript 14; Auswahl: rising edge >= 0.5"
        f" (Gutachten Zeile {line}: 'Außerdem gab es 11 Starts.')"
    ]


def test_echtes_fehlzitat_bleibt_befund_und_nennt_das_zitat(tmp_path):
    from src.erkunder.pruefkreis import prueferzahlen

    report = evidence(tmp_path, "14")
    review = (
        "```erkunder-zahlenpruefung\n"
        + json.dumps(
            {
                "vollstaendig": True,
                "zahlen": [
                    {"zitat": "[14](zahl:starts)", "zahl": "14", "id": "starts"},
                    {"zitat": "Es gab 12 Stopps.", "zahl": "12", "id": "starts"},
                    {"zitat": "Starts [14](zahl:starts)", "zahl": "15", "id": "starts"},
                ],
            }
        )
        + "\n```"
    )
    findings = prueferzahlen(
        tmp_path, report, review, {"messdaten/test.parquet": ["pump_signal"]}
    )
    assert findings == [
        "Zahlenprüfung: Zahlenzitat nicht im Gutachten: 'Es gab 12 Stopps.'"
        " (Zahl '12', ID starts)",
        "Zahlenprüfung: Zahl '15' steht nicht im Zitat 'Starts [14](zahl:starts)'"
        " (Gutachten Zeile 1)",
    ]


def _pruefung_umschreiben(service, places, rewrite):
    """Rewrite a reviewer's pruefung.md before the coordinator reads it."""
    import httpx

    original = places.__class__.__call__

    async def transport(request):
        response = await original(places, request)
        if request.method == "GET" and request.url.host != "download.test":
            name = request.url.path.split("/")[-1]
            data = places.running.get(name)
            if data and name.startswith("pruefung"):
                from pathlib import Path
                path = Path(data["ordner"]) / "pruefung.md"
                if path.exists():
                    path.write_text(
                        rewrite(name, places.attempts[name], path.read_text())
                    )
        return response

    return httpx.AsyncClient(transport=httpx.MockTransport(transport))


def _befunde_ersetzen(text, value):
    head, rest = text.split("```erkunder-pruefung\n", 1)
    _, tail = rest.split("\n```", 1)
    return head + "```erkunder-pruefung\n" + value + "\n```" + tail


def _meta_gueltig(service):
    from src.erkunder.models import Ergebnis

    Ergebnis.model_validate(service.status("bericht-123")["meta"])


async def test_objekt_befunde_erreichen_den_korrektor_ohne_formbefund(tmp_path):
    service, places, _ = await setup(tmp_path, review="Widerspruch")
    objects = json.dumps({"befunde": OBJEKTFORMEN}, ensure_ascii=False)
    await service.client.aclose()
    service.client = _pruefung_umschreiben(
        service,
        places,
        lambda name, attempt, text: (
            _befunde_ersetzen(text, objects) if name == "pruefung" else text
        ),
    )
    try:
        status = await finish(service)
        assert status["zustand"] == "fertig"
        assert places.attempts["pruefung"] == 1  # lesbar: keine Formrückfrage
        path = (
            service.directory("bericht-123")
            / "harmonisierung-korrektur/maschinenbefunde.json"
        )
        findings = json.loads(path.read_text())
        assert (
            "id: P4; stelle: skripte/pruefe_links.py; art: Nachweis;"
            " beschreibung: liest ../ergebnis.md; schwere: gering" in findings
        )
        assert not any(
            "Prüfurteil" in finding or "Prüfbefunde" in finding for finding in findings
        )
        korrektur = next(
            c for c in places.calls if c["schritt"] == "harmonisierung-korrektur"
        )
        assert "`skripte/rechnung.py`, `skripte/zahlen.json`" in korrektur["prompt"]
        _meta_gueltig(service)
    finally:
        await service.shutdown()


async def test_unlesbares_urteil_geht_an_den_pruefer_zurueck(tmp_path):
    service, places, _ = await setup(tmp_path)
    await service.client.aclose()
    service.client = _pruefung_umschreiben(
        service,
        places,
        lambda name, attempt, text: (
            _befunde_ersetzen(text, '{"befunde": [42]}')
            if (name, attempt) == ("pruefung", 1)
            else text
        ),
    )
    try:
        status = await finish(service)
        assert status["zustand"] == "fertig"
        assert places.attempts["pruefung"] == 2
        assert "harmonisierung-korrektur" not in places.attempts  # nie an den Korrektor
        assert status["meta"]["pruefstatus"] == "widerspruchsfrei"
        assert [
            s["versuch"] for s in status["meta"]["schritte"] if s["name"] == "pruefung"
        ] == [1, 2]
        retry = [c for c in places.calls if c["schritt"] == "pruefung"][1]
        assert "befunde[1] ist weder ein Text" in retry["prompt"]
        folder = service.directory("bericht-123") / "pruefung"
        assert '"befunde": [42]' in (folder / "pruefung-entwurf.md").read_text()
        # Unabhängigkeit: nur der eigene Entwurf, keine Vorprüfung, keine Vorfassung.
        assert sorted(p.name for p in folder.iterdir() if p.is_file()) == [
            "gutachten.md",
            "maschinenbefunde.json",
            "pruefung-entwurf.md",
            "pruefung.md",
        ]
        assert status["fertig"] <= status["gesamt"]
        _meta_gueltig(service)
    finally:
        await service.shutdown()


async def test_zweimal_unlesbar_endet_laut_und_geht_nicht_an_den_korrektor(tmp_path):
    service, places, _ = await setup(tmp_path)
    await service.client.aclose()
    service.client = _pruefung_umschreiben(
        service,
        places,
        lambda name, attempt, text: (
            _befunde_ersetzen(text, '{"befunde": [{}]}') if name == "pruefung" else text
        ),
    )
    try:
        status = await finish(service)
        assert (
            status["zustand"] == "abbruch"
        )  # weder grün noch als offener Befund gezählt
        assert status["schritt"] == "pruefung"
        assert "auch nach Formrückfrage unlesbar" in status["fehler"]
        assert places.attempts["pruefung"] == 2
        assert "harmonisierung-korrektur" not in places.attempts
    finally:
        await service.shutdown()


def _hinweise_setzen(befunde, hinweise):
    value = json.dumps({"befunde": befunde, "hinweise": hinweise}, ensure_ascii=False)
    return lambda name, attempt, text: _befunde_ersetzen(text, value)


async def test_hinweis_allein_sperrt_nicht(tmp_path, caplog):
    service, places, _ = await setup(tmp_path)
    await service.client.aclose()
    hint = {
        "id": "P7",
        "stelle": "B9",
        "beschreibung": "Faktor vertretbar, Quelle nennen",
        "schwere": "gering",
    }
    service.client = _pruefung_umschreiben(
        service, places, _hinweise_setzen([], [hint])
    )
    caplog.set_level("INFO", logger="src.erkunder.leitstand")
    try:
        status = await finish(service)
        assert status["meta"]["pruefstatus"] == "widerspruchsfrei"
        assert status["meta"]["offene_befunde_anzahl"] == 0
        assert "harmonisierung-korrektur" not in places.attempts
        final = service.result("bericht-123")["gutachten_final"]
        assert "## Hinweise des Prüfers (halten das Gutachten nicht an)" in final
        assert "beschreibung: Faktor vertretbar, Quelle nennen" in final
        assert "Offene Befunde" not in final
        assert service.states["bericht-123"]["runden"] == [
            {
                "runde": 0,
                "pruefung": "pruefung",
                "befunde": 0,
                "hinweise": 1,
                "maschinenbefunde": 0,
                "formmeldungen": 0,
                "offen": 0,
            }
        ]
        assert any(
            "pruefrunde runde=0 pruefung=pruefung befunde=0 hinweise=1"
            in r.getMessage()
            for r in caplog.records
        )
    finally:
        await service.shutdown()


async def test_sperrender_befund_bleibt_offen_hinweis_wird_nicht_gezaehlt(tmp_path):
    service, places, _ = await setup(tmp_path)
    await service.client.aclose()
    service.client = _pruefung_umschreiben(
        service,
        places,
        _hinweise_setzen(
            [
                {
                    "id": "P1",
                    "stelle": "B11",
                    "beschreibung": "Vermerk widerspricht Skriptwert",
                }
            ],
            ["B7: Extrapolationshinweis ergänzen"],
        ),
    )
    order = body()
    order.auftrag.korrekturkreis = 1
    try:
        status = await finish(service, order)
        assert status["meta"]["pruefstatus"] == "offen"
        assert status["meta"]["offene_befunde_anzahl"] == 1  # nur der sperrende Punkt
        korrektur = next(
            c for c in places.calls if c["schritt"] == "harmonisierung-korrektur"
        )
        assert (
            "`antwort-pruefer.md`" in korrektur["prompt"]
            and "nicht ins Gutachten" in korrektur["prompt"]
        )
        runden = service.states["bericht-123"]["runden"]
        assert [(r["befunde"], r["hinweise"], r["formmeldungen"]) for r in runden] == [
            (1, 1, 0),
            (1, 1, 0),
        ]
        assert runden[1]["antwort_pruefer"] is False
        _meta_gueltig(service)
    finally:
        await service.shutdown()


async def test_formmeldung_wird_getrennt_protokolliert(tmp_path):
    service, places, _ = await setup(tmp_path)
    await service.client.aclose()
    service.client = _pruefung_umschreiben(
        service,
        places,
        lambda name, attempt, text: (
            _befunde_ersetzen(text, '{"befunde": [42]}')
            if (name, attempt) == ("pruefung", 1)
            else text
        ),
    )
    try:
        status = await finish(service)
        assert status["meta"]["offene_befunde_anzahl"] == 0
        assert service.states["bericht-123"]["runden"][0]["formmeldungen"] == 1
    finally:
        await service.shutdown()
