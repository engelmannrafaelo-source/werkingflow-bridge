"""Every explicit number link reaches comparison, regardless of numeric spelling."""
import json

import pytest

from src.erkunder.pruefkreis import prueferzahlen, zahlenbelege
from src.erkunder.zahlenverweise import zahlenverweise
from tests.erkunder.test_pruefkreis import evidence

SOURCES = {"messdaten/test.parquet": ["pump_signal"]}


def review(items):
    return '```erkunder-zahlenpruefung\n' + json.dumps({"vollstaendig": True, "zahlen": items}) + '\n```'


@pytest.mark.parametrize("link,expected", [
    ("[1e3](zahl:bad)", ("1e3", "bad")),
    ("[1e3](ZaHl:bad)", ("1e3", "bad")),
    ("[unlesbar](zahl:bad)", ("unlesbar", "bad")),
    ("[](zahl:bad)", ("", "bad")),
    ("[NaN](zahl:bad)", ("NaN", "bad")),
    ("[1.234,5](zahl:bad)", ("1.234,5", "bad")),
    ("[1](zahl:a.b/ä)", ("1", "a.b/ä")),
    ("[1](zahl:)", ("1", "")),
    ('[1e3](<zahl:bad> "Ergebnis")', ("1e3", "bad")),
    ("[**1e3**](zahl:bad)", ("1e3", "bad")),
    ("[`1e3`](zahl:bad)", ("1e3", "bad")),
    ("[1e3][n]\n\n[n]: zahl:bad", ("1e3", "bad")),
    ("[1e3][]\n\n[1e3]: zahl:bad", ("1e3", "bad")),
    ("[1e3]\n\n[1e3]: zahl:bad", ("1e3", "bad")),
    ("[1&#101;3](zahl:bad)", ("1e3", "bad")),
    ("[1\\[2\\]](zahl:bad)", ("1[2]", "bad")),
    ("[1](zahl:a(b))", ("1", "a(b)")),
    ("<zahl:bad>", ("zahl:bad", "bad")),
])
def test_structural_collection_does_not_filter_number_labels_or_ids(link, expected):
    assert zahlenverweise(link) == [expected]


def test_prose_code_and_non_number_links_are_not_number_claims():
    text = '11 Starts. `[7](zahl:code)` [8](https://example.invalid)\n\n```json\n"[9](zahl:example)"\n```'
    assert zahlenverweise(text) == []


def test_scientific_link_repro_compares_both_sides_and_requires_review_entry(tmp_path):
    report = evidence(tmp_path, "14").replace('zahl:starts', 'zahl:ok')
    path = tmp_path / 'skripte/values.json'
    record = json.loads(path.read_text())['starts']
    path.write_text(json.dumps({'ok': record, 'bad': {**record, 'wert': 7}}))
    report += '\n[1e3](zahl:bad)'
    reviewer = review([{"zitat": "[14](zahl:ok)", "zahl": "14", "id": "ok"}])
    compared, _ = zahlenbelege(tmp_path, report, SOURCES)
    coverage = prueferzahlen(tmp_path, report, reviewer, SOURCES)
    assert any("bad: Text '1e3', Skript 7" in finding for finding in compared)
    assert any('zahl:bad' in finding and 'fehlt im Zahlenabgleich' in finding and 'Skript 7' in finding
               for finding in coverage)


@pytest.mark.parametrize('shown', ['', 'unlesbar', '1.234,5', 'NaN', 'Infinity', '1e999999999'])
def test_invalid_number_is_a_finding_with_text_and_script(tmp_path, shown):
    report = evidence(tmp_path, shown, value=7)
    findings, _ = zahlenbelege(tmp_path, report, SOURCES)
    assert any(f'Text {shown!r}, Skript 7' in finding and 'ungültig' in finding for finding in findings)


@pytest.mark.parametrize('defect', ['missing_id', 'missing_value', 'null_value', 'empty_id'])
def test_missing_script_side_is_named(tmp_path, defect):
    report = evidence(tmp_path, '1e3', value=7)
    path = tmp_path / 'skripte/values.json'
    values = json.loads(path.read_text())
    if defect == 'missing_id':
        values.clear()
    elif defect == 'missing_value':
        del values['starts']['wert']
    elif defect == 'null_value':
        values['starts']['wert'] = None
    else:
        report = report.replace('zahl:starts', 'zahl:')
    path.write_text(json.dumps(values))
    findings, _ = zahlenbelege(tmp_path, report, SOURCES)
    assert any("Text '1e3', Skript" in finding and 'ungültig' in finding for finding in findings)


def test_valid_scientific_number_passes_comparison_and_coverage(tmp_path):
    report = evidence(tmp_path, '1e3', value=1000)
    reviewer = review([{'zitat': '[1e3](zahl:starts)', 'zahl': '1e3', 'id': 'starts'}])
    assert zahlenbelege(tmp_path, report, SOURCES)[0] == []
    assert prueferzahlen(tmp_path, report, reviewer, SOURCES) == []


def test_repeated_links_each_need_a_review_entry(tmp_path):
    report = evidence(tmp_path, '14') + '\n[14](zahl:starts)'
    item = {'zitat': '[14](zahl:starts)', 'zahl': '14', 'id': 'starts'}
    assert len(zahlenverweise(report)) == 2
    assert prueferzahlen(tmp_path, report, review([item]), SOURCES)
    assert prueferzahlen(tmp_path, report, review([item, item]), SOURCES) == []


def test_reference_link_invalid_label_is_not_hidden_by_valid_inline_link(tmp_path):
    report = evidence(tmp_path, '14') + '\n[unlesbar][n]\n\n[n]: zahl:starts'
    findings, _ = zahlenbelege(tmp_path, report, SOURCES)
    assert any("Text 'unlesbar', Skript 14" in finding for finding in findings)
    item = {'zitat': '[14](zahl:starts)', 'zahl': '14', 'id': 'starts'}
    assert prueferzahlen(tmp_path, report, review([item]), SOURCES)


async def test_scientific_link_finding_reaches_correction_and_re_review(tmp_path, monkeypatch):
    from pathlib import Path

    from tests.erkunder.test_leitstand import Places, finish, setup

    original = Places.__call__

    async def transport(self, request):
        response = await original(self, request)
        if request.method == 'GET' and request.url.path.endswith('/harmonisierung'):
            directory = Path(self.running['harmonisierung']['ordner'])
            report = directory / 'ergebnis.md'
            report.write_text(report.read_text() + '\n[1e3](zahl:bad)')
            output = directory / 'skripte/zahlen.json'
            values = json.loads(output.read_text())
            values['bad'] = {**values['n'], 'wert': 7}
            output.write_text(json.dumps(values))
        return response

    monkeypatch.setattr(Places, '__call__', transport)
    service, places, _ = await setup(tmp_path)
    try:
        status = await finish(service)
        assert status['meta']['korrekturrunden'] == 1
        assert status['meta']['offene_befunde_anzahl'] == 0
        assert places.attempts['pruefung-korrektur'] == 1
        path = service.directory('bericht-123') / 'harmonisierung-korrektur/maschinenbefunde.json'
        findings = json.loads(path.read_text())
        assert any("bad: Text '1e3', Skript 7" in finding for finding in findings)
        assert any('zahl:bad' in finding and 'fehlt im Zahlenabgleich' in finding for finding in findings)
    finally:
        await service.shutdown()


@pytest.mark.parametrize('quote,shown', [
    ('[1**000**](zahl:starts)', '1000'),
    ('[1&#101;3](zahl:starts)', '1e3'),
    ('[1**000**][n]', '1000'),
    ('Unverknüpfte Zahl: 1**000**.', '1000'),
])
def test_reviewer_quotes_use_the_same_visible_text_as_the_number_links(tmp_path, quote, shown):
    report = evidence(tmp_path, '1000', value=1000)
    if quote.startswith('['):
        report = report.replace('[1000](zahl:starts)', quote)
        items = []
    else:
        report += '\n' + quote
        items = [{'zitat': '[1000](zahl:starts)', 'zahl': '1000', 'id': 'starts'}]
    report += '\n\n[n]: zahl:starts\n'
    items.append({'zitat': quote, 'zahl': shown, 'id': 'starts'})
    assert zahlenbelege(tmp_path, report, SOURCES)[0] == []
    assert prueferzahlen(tmp_path, report, review(items), SOURCES) == []


def test_number_absent_from_visible_quote_is_rejected(tmp_path):
    report = evidence(tmp_path, '14')
    reviewer = review([{'zitat': '[14](zahl:starts)', 'zahl': '7', 'id': 'starts'}])
    assert 'Zahlenzitat nicht im Gutachten' in prueferzahlen(tmp_path, report, reviewer, SOURCES)[0]


def test_image_alt_link_is_not_an_active_number_link(tmp_path):
    report = evidence(tmp_path, '1e3', value=1000) + '\n![[7](zahl:bad)](image.png)'
    item = {'zitat': '[1e3](zahl:starts)', 'zahl': '1e3', 'id': 'starts'}
    assert zahlenverweise(report) == [('1e3', 'starts')]
    assert zahlenbelege(tmp_path, report, SOURCES)[0] == []
    assert prueferzahlen(tmp_path, report, review([item]), SOURCES) == []


def test_active_link_wrapping_an_image_still_requires_readable_number_text(tmp_path):
    report = evidence(tmp_path, '14') + '\n[![14](image.png)](zahl:starts)'
    links = zahlenverweise(report)
    assert len(links) == 2 and links[1][1] == 'starts'
    assert zahlenbelege(tmp_path, report, SOURCES)[0]
