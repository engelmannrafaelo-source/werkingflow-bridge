"""Tests für die OpenAlex-Content-API-Anbindung (`src/openalex_content.py`).

Kein Netz — `requests.get` wird gestubbt. Für den echten Live-Call siehe
`test_openalex_content_integration.py` (skipif ohne OPENALEX_API_KEY).

Deckt die beiden real gemessenen GROBID-TEI-Formvarianten ab (03.09.2026):
  - `<text><body><div>...</div></body><back>...</back></text>` (Normalfall)
  - `<text><div>...</div><back>...</back></text>` ohne <body>-Wrapper (Fall, der die
    ursprüngliche `.//tei:body`-XPath-Version stumm auf den Abstract zurückfallen ließ)
"""
import gzip
import logging

import pytest
import requests

from src import openalex_content as oc

TEI_NS = 'xmlns="http://www.tei-c.org/ns/1.0"'


def _tei(abstract: str, body_xml: str) -> bytes:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<TEI {TEI_NS}>
<teiHeader><fileDesc><titleStmt><title>T</title></titleStmt>
<sourceDesc><biblStruct><analytic></analytic></biblStruct></sourceDesc></fileDesc>
<profileDesc><abstract><div><p>{abstract}</p></div></abstract></profileDesc></teiHeader>
<text xml:lang="en">
{body_xml}
<back><div type="references"><listBibl><biblStruct><note>sollte nicht im body landen</note></biblStruct></listBibl></div></back>
</text>
</TEI>"""
    return gzip.compress(xml.encode("utf-8"))


def _tei_with_body_wrapper(abstract: str, article_text: str) -> bytes:
    return _tei(abstract, f"<body><div><p>{article_text}</p></div></body>")


def _tei_without_body_wrapper(abstract: str, article_text: str) -> bytes:
    return _tei(abstract, f"<div><p>{article_text}</p></div>")


class _Resp:
    def __init__(self, status_code, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


ENGLISH_ARTICLE = ("The quick brown fox jumps over the lazy dog and this is definitely "
                    "an English sentence with the and of to in for that with this are on as by.")
KOREAN_ARTICLE = "이것은 한국어 문장입니다 화재실의 연기를 제어하는 방법은 우리가 사용하는 공간에서"


def test_kein_api_key_ueberspringt_ohne_netzcall(monkeypatch, caplog):
    called = []
    monkeypatch.setattr(requests, "get", lambda *a, **kw: called.append(1))

    with caplog.at_level(logging.INFO, logger="openalex_content"):
        assert oc.fetch_fulltext("W123", None) is None

    assert called == []
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_404_ist_kein_alarm(monkeypatch, caplog):
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _Resp(404))

    with caplog.at_level(logging.INFO, logger="openalex_content"):
        assert oc.fetch_fulltext("W123", "key") is None

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert "kein Volltext im Content-Index" in caplog.text


def test_401_ist_alarm(monkeypatch, caplog):
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _Resp(401))

    with caplog.at_level(logging.INFO, logger="openalex_content"):
        assert oc.fetch_fulltext("W123", "invalid-key") is None

    assert any(r.levelno >= logging.WARNING for r in caplog.records)
    assert "OPENALEX_API_KEY" in caplog.text


def test_unerwarteter_status_faellt_fail_soft(monkeypatch, caplog):
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _Resp(500))

    with caplog.at_level(logging.INFO, logger="openalex_content"):
        assert oc.fetch_fulltext("W123", "key") is None

    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_netzfehler_faellt_fail_soft_ohne_exception(monkeypatch, caplog):
    def boom(*a, **kw):
        raise requests.ReadTimeout("timed out")

    monkeypatch.setattr(requests, "get", boom)

    with caplog.at_level(logging.INFO, logger="openalex_content"):
        assert oc.fetch_fulltext("W123", "key") is None


def test_kaputtes_gzip_faellt_fail_soft(monkeypatch, caplog):
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _Resp(200, content=b"not-gzip"))

    with caplog.at_level(logging.INFO, logger="openalex_content"):
        assert oc.fetch_fulltext("W123", "key") is None

    assert any(r.levelno >= logging.WARNING for r in caplog.records)
    assert "Parse-Fehler" in caplog.text


def test_body_mit_wrapper_wird_verwendet(monkeypatch):
    xml = _tei_with_body_wrapper("An abstract about foxes.", ENGLISH_ARTICLE)
    monkeypatch.setattr(requests, "get",
                         lambda *a, **kw: _Resp(200, content=xml, headers={"x-ratelimit-credits-used": "100"}))

    r = oc.fetch_fulltext("W123", "key")

    assert r["kind"] == "fulltext"
    assert r["language"] == "en"
    assert r["degraded_to_abstract"] is False
    assert "quick brown fox" in r["text"]
    assert "sollte nicht im body landen" not in r["text"]
    assert r["credits_used"] == 100


def test_body_ohne_wrapper_wird_trotzdem_gefunden(monkeypatch):
    """Regression: <div> direkt unter <text> ohne <body> — reale zweite Formvariante."""
    xml = _tei_without_body_wrapper("An abstract about foxes.", ENGLISH_ARTICLE)
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _Resp(200, content=xml))

    r = oc.fetch_fulltext("W123", "key")

    assert r["kind"] == "fulltext"
    assert "quick brown fox" in r["text"]
    assert "sollte nicht im body landen" not in r["text"]  # <back> bleibt ausgeschlossen


def test_fremdsprachiger_body_degradiert_auf_abstract(monkeypatch, caplog):
    xml = _tei_with_body_wrapper("An English abstract about smoke control.", KOREAN_ARTICLE)
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _Resp(200, content=xml))

    with caplog.at_level(logging.INFO, logger="openalex_content"):
        r = oc.fetch_fulltext("W123", "key")

    assert r["kind"] == "abstract"
    assert r["language"] == "other"
    assert r["degraded_to_abstract"] is True
    assert r["text"] == r["abstract"]
    assert KOREAN_ARTICLE not in r["text"]


def test_kein_body_ist_keine_degradierung(monkeypatch):
    """Kein Volltext im Dokument (GROBID hat keinen body geliefert) != Sprach-Degradierung."""
    xml = _tei("Just an abstract, no body at all.", "")
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _Resp(200, content=xml))

    r = oc.fetch_fulltext("W123", "key")

    assert r["kind"] == "abstract"
    assert r["degraded_to_abstract"] is False
    assert r["text"] == "Just an abstract, no body at all."


def test_weder_body_noch_abstract_ergibt_none(monkeypatch, caplog):
    xml = _tei("", "")
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _Resp(200, content=xml))

    with caplog.at_level(logging.INFO, logger="openalex_content"):
        assert oc.fetch_fulltext("W123", "key") is None

    assert "kein extrahierbarer Text" in caplog.text


def test_volle_openalex_url_wird_normalisiert(monkeypatch):
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        return _Resp(200, content=_tei_with_body_wrapper("abstract", ENGLISH_ARTICLE))

    monkeypatch.setattr(requests, "get", fake_get)
    oc.fetch_fulltext("https://openalex.org/W123", "key")

    assert seen["url"].endswith("/W123.grobid-xml")


def test_fetch_fulltexts_respektiert_deckel_und_reihenfolge(monkeypatch):
    calls = []

    def fake_fetch(wid, api_key, **kw):
        calls.append(wid)
        return {"text": f"text-{wid}"}

    monkeypatch.setattr(oc, "fetch_fulltext", fake_fetch)

    out = oc.fetch_fulltexts(["W1", "W2", "W3"], "key", max_fetches=2)

    assert calls == ["W1", "W2"]
    assert set(out.keys()) == {"W1", "W2"}


def test_fetch_fulltexts_ohne_key_ruft_nie_netz(monkeypatch):
    monkeypatch.setattr(oc, "fetch_fulltext", lambda *a, **kw: pytest.fail("darf nicht aufgerufen werden"))

    assert oc.fetch_fulltexts(["W1"], None) == {}


def test_fetch_fulltexts_bricht_bei_einzelfehler_nicht_ab(monkeypatch):
    def fake_fetch(wid, api_key, **kw):
        if wid == "W1":
            return None
        return {"text": f"text-{wid}"}

    monkeypatch.setattr(oc, "fetch_fulltext", fake_fetch)

    out = oc.fetch_fulltexts(["W1", "W2"], "key", max_fetches=2)

    assert list(out.keys()) == ["W2"]
