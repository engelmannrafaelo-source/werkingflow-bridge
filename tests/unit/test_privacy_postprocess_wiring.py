"""Verdrahtung der Nachbearbeitung in src.main._smart_anonymize_core.

Schalter BRIDGE_PSEUDONYM_POSTPROCESS: an -> Antwort nachbearbeitet; aus -> Antwort des
Privacy-Dienstes unveraendert (Prod-Verhalten). Ein Fehler der Nachbearbeitung ist ein
Fehler des Aufrufs (status=error), nie die ungefilterte Antwort.
"""
import sys
from contextlib import asynccontextmanager
from unittest.mock import MagicMock as _MagicMock

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
    "src.identity.routes",
    "src.db.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

import src.main  # noqa: E402

TEXT = "Mieterin Verena Hollaus, geb. 14.03.1989, Messung 03.08.2026, Wärmepumpe WP1."
DIENST = {
    "status": "success",
    "anonymization_performed": True,
    "raw_anonymized_text": "Mieterin SN_PERSON_001, geb. SN_PHONE_NUMBER_002, Messung SN_PHONE_NUMBER_001, Wärmepumpe SN_ORGANIZATION_001.",
    "raw_entity_count": 4,
    "smart_anonymized_text": "Mieterin SN_PERSON_001, geb. SN_PHONE_NUMBER_002, Messung SN_PHONE_NUMBER_001, Wärmepumpe SN_ORGANIZATION_001.",
    "smart_entity_count": 4,
    "restored_entities": [],
    "mapping": {
        "SN_PERSON_001": "Verena Hollaus",
        "SN_PHONE_NUMBER_002": "14.03.1989",
        "SN_PHONE_NUMBER_001": "03.08.2026",
        "SN_ORGANIZATION_001": "WP1",
    },
    "detected_entities": [],
}


def _client(payload):
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)

    @asynccontextmanager
    async def track_call():
        yield 0

    client.track_call = track_call
    return client


async def _aufruf(monkeypatch, flag, payload=DIENST, **kw):
    monkeypatch.setenv("BRIDGE_ANONYMIZE_ENABLED", "true")
    if flag is None:
        monkeypatch.delenv("BRIDGE_PSEUDONYM_POSTPROCESS", raising=False)
    else:
        monkeypatch.setenv("BRIDGE_PSEUDONYM_POSTPROCESS", flag)
    with patch("src.main.get_privacy_client", return_value=_client(payload)), \
         patch("src.activity.ai_call_writer.persist_ai_call_activity", new=AsyncMock()), \
         patch("src.audit.recorder.record_audit_event", new=AsyncMock()), \
         patch("src.main._record_document_call_metrics"):
        return await src.main._smart_anonymize_core(MagicMock(), text=TEXT, prefix="SN", **kw)


@pytest.mark.asyncio
async def test_aus_laesst_dienst_antwort_unveraendert(monkeypatch):
    r = await _aufruf(monkeypatch, None)
    assert r.smart_anonymized_text == DIENST["smart_anonymized_text"]
    assert r.mapping == DIENST["mapping"]
    assert r.postprocessing is None


@pytest.mark.asyncio
async def test_an_bearbeitet_nach(monkeypatch):
    r = await _aufruf(monkeypatch, "true")
    assert r.status == "success"
    assert r.smart_anonymized_text == "Mieterin SN_PERSON_001, geb. SN_GEBURTSDATUM_001, Messung 03.08.2026, Wärmepumpe WP1."
    assert r.mapping == {"SN_PERSON_001": "Verena Hollaus", "SN_GEBURTSDATUM_001": "14.03.1989"}
    assert r.postprocessing["version"]


@pytest.mark.asyncio
async def test_known_entities_werden_durchgereicht(monkeypatch):
    r = await _aufruf(monkeypatch, "true", known_entities={"ENGDOCX_PERSON_007": "Verena Hollaus"})
    assert "ENGDOCX_PERSON_007" in r.smart_anonymized_text


@pytest.mark.asyncio
async def test_known_entities_ohne_schalter_wird_sichtbar_ignoriert(monkeypatch):
    r = await _aufruf(monkeypatch, None, known_entities={"ENGDOCX_PERSON_007": "Verena Hollaus"})
    assert r.postprocessing == {"active": False, "known_entities_ignored": 1}


@pytest.mark.asyncio
async def test_widerspruechliche_dienst_antwort_wird_fehler(monkeypatch):
    kaputt = {**DIENST, "smart_anonymized_text": "voellig anderer Text SN_PERSON_001"}
    r = await _aufruf(monkeypatch, "true", payload=kaputt)
    assert r.status == "error"
    assert r.smart_anonymized_text is None
