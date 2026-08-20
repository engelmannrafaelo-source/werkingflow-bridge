"""
Ledger spool — die Durability-Zusage von ADR-0009 Schritt 1.

Was hier geprueft wird, ist genau eine Aussage: **eine Abrechnungszeile geht
nicht mehr verloren, wenn die Datenbank nicht mitspielt.** Vorher endete jeder
Fehler zwischen LLM-Antwort und INSERT in einem ERROR-Log und nicht
abgerechneter Nutzung.

Die Tests sind bewusst gegen das VERHALTEN geschrieben (was liegt auf Platte,
was wird nachgeholt, was wird abgezogen), nicht gegen die Dateiformate — das
Format darf sich aendern, die Zusage nicht.
"""
from __future__ import annotations

import json
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

for _mod in ["src.db.client", "src.pricing"]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()
import src.pricing as _pricing_stub  # noqa: E402
_pricing_stub.cost_eur = MagicMock(return_value=0.01)
_pricing_stub.PRICING_VERSION = "test-v1"

from src.activity import ledger_spool as spool  # noqa: E402


@pytest.fixture
def spool_dir(tmp_path, monkeypatch):
    """Isolierter Spool je Test. SPOOL_DIR wird zur Laufzeit gelesen, der
    Cache-Merker `_dir_ready` muss mit zurueckgesetzt werden."""
    d = tmp_path / "bridge-billing-spool"
    monkeypatch.setattr(spool, "SPOOL_DIR", str(d))
    monkeypatch.setattr(spool, "WORKER_NAME", "worker-test")
    monkeypatch.setattr(spool, "_dir_ready", None)
    monkeypatch.setattr(spool, "_undurable_calls", 0)
    monkeypatch.setenv("BRIDGE_LEDGER_SPOOL_ENABLED", "true")
    return d


def _rec(model="claude-sonnet-5"):
    return {"app_id": "werking-report", "user_id": "u", "model": model}


# ---------------------------------------------------------------------------
# Der Puffer selbst
# ---------------------------------------------------------------------------

def test_append_makes_the_call_owed(spool_dir):
    """Nach dem Append ist die Zeile geschuldet — auch ohne jede DB."""
    uid = spool.new_call_uid()
    assert spool.append_call(uid, _rec()) is True
    assert spool.spool_stats()["pending"] == 1


def test_ack_settles_it(spool_dir):
    uid = spool.new_call_uid()
    spool.append_call(uid, _rec())
    spool.ack(uid, spool.OUTCOME_WRITTEN)
    assert spool.spool_stats()["pending"] == 0


def test_vocabulary_biases_toward_retry():
    """Alles, was kein definitiver Ausgang ist — inklusive Unbekanntem —,
    gilt als weiterhin geschuldet. Auf einem Geldpfad ist die sichere Richtung
    'nochmal versuchen', nie 'stillschweigend verwerfen'."""
    assert spool.is_definitive(spool.OUTCOME_WRITTEN)
    assert spool.is_definitive(spool.OUTCOME_DUPLICATE)
    assert spool.is_definitive("skipped:no_tenant")
    assert not spool.is_definitive(spool.OUTCOME_FAILED)
    assert not spool.is_definitive("etwas voellig anderes")


@pytest.mark.asyncio
async def test_flush_replays_and_settles(spool_dir):
    uid = spool.new_call_uid()
    spool.append_call(uid, _rec())

    seen = []

    async def writer(**kwargs):
        seen.append(kwargs)
        return spool.OUTCOME_WRITTEN

    stats = await spool.flush_once(writer)
    assert stats["replayed"] == 1 and stats["written"] == 1
    assert spool.spool_stats()["pending"] == 0
    # Der Nachlauf reicht Identitaet UND Ursprungszeit durch — sonst waere die
    # nachgeholte Zeile eine zweite Zeile bzw. im falschen Zeitraum verbucht.
    assert seen[0]["_call_uid"] == uid
    assert isinstance(seen[0]["_call_ts"], float)
    assert seen[0]["model"] == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_flush_keeps_it_owed_while_the_db_is_down(spool_dir):
    """Der Kern: solange nicht geschrieben werden kann, bleibt die Zeile da."""
    uid = spool.new_call_uid()
    spool.append_call(uid, _rec())

    async def failing_writer(**kwargs):
        return spool.OUTCOME_FAILED

    for _ in range(3):
        stats = await spool.flush_once(failing_writer)
        assert stats["still_owed"] == 1
    assert spool.spool_stats()["pending"] == 1

    async def ok_writer(**kwargs):
        return spool.OUTCOME_WRITTEN

    await spool.flush_once(ok_writer)
    assert spool.spool_stats()["pending"] == 0


@pytest.mark.asyncio
async def test_definitive_skip_is_not_retried_forever(spool_dir):
    """Ein SELECT, das 'kein Tenant' antwortet, ist eine Antwort — kein
    Ausfall. Sonst wuerde ein korrekter Skip zur Endlosschleife."""
    spool.append_call(spool.new_call_uid(), _rec())

    calls = {"n": 0}

    async def writer(**kwargs):
        calls["n"] += 1
        return "skipped:no_tenant"

    await spool.flush_once(writer)
    await spool.flush_once(writer)
    assert calls["n"] == 1
    assert spool.spool_stats()["pending"] == 0


@pytest.mark.asyncio
async def test_unwritable_row_is_buried_loudly_never_dropped(spool_dir, monkeypatch):
    monkeypatch.setattr(spool, "MAX_ATTEMPTS", 2)
    uid = spool.new_call_uid()
    spool.append_call(uid, _rec())

    async def failing_writer(**kwargs):
        return spool.OUTCOME_FAILED

    for _ in range(5):
        await spool.flush_once(failing_writer)

    assert spool.spool_stats()["pending"] == 0
    dead = spool_dir / "dead.worker-test.jsonl"
    assert dead.exists(), "aufgegebene Geldzeile muss auffindbar bleiben"
    buried = json.loads(dead.read_text().strip().splitlines()[0])
    # Mit den vollstaendigen Originalargumenten — sonst ist sie nicht manuell
    # nachspielbar und 'aufgegeben' hiesse in Wahrheit 'verloren'.
    assert buried["uid"] == uid
    assert buried["r"]["app_id"] == "werking-report"
    assert "dead_reason" in buried


@pytest.mark.asyncio
async def test_orphan_of_a_dead_process_is_adopted(spool_dir):
    """Das ist es, was den Puffer einen OOM-Kill/Neustart ueberleben laesst."""
    spool._ensure_dir()
    dead_pid = 999999  # existiert nicht in /proc
    orphan = spool_dir / f"ledger.worker-test.{dead_pid}.jsonl"
    orphan.write_text(
        json.dumps(
            {"t": "c", "uid": "verwaist-1", "ts": time.time(), "n": 0, "r": _rec()}
        )
        + "\n"
    )

    replayed = []

    async def writer(**kwargs):
        replayed.append(kwargs["_call_uid"])
        return spool.OUTCOME_WRITTEN

    stats = await spool.flush_once(writer)
    assert replayed == ["verwaist-1"]
    assert stats["written"] == 1
    assert not orphan.exists(), "geleerte Waisen-Datei wird aufgeraeumt"


def test_live_siblings_file_is_left_alone(spool_dir):
    """Die Datei eines LEBENDEN Geschwisterprozesses gehoert ihm — sie zu
    uebernehmen hiesse, ihm die Datei unter den Haenden neu zu schreiben."""
    spool._ensure_dir()
    lebend = spool_dir / f"ledger.worker-test.{os.getppid()}.jsonl"
    lebend.write_text(
        json.dumps({"t": "c", "uid": "fremd", "ts": time.time(), "n": 0, "r": _rec()})
        + "\n"
    )
    tot = spool_dir / "ledger.worker-test.999999.jsonl"
    tot.write_text(
        json.dumps({"t": "c", "uid": "waise", "ts": time.time(), "n": 0, "r": _rec()})
        + "\n"
    )
    waisen = spool._orphan_files()
    assert str(tot) in waisen
    assert str(lebend) not in waisen


def test_corrupt_tail_does_not_block_the_rest(spool_dir):
    """Ein abgerissener Schreibvorgang (OOM mitten in der Zeile) darf nicht
    den ganzen Puffer blockieren."""
    uid = spool.new_call_uid()
    spool.append_call(uid, _rec())
    with open(spool._own_path(), "a") as f:
        f.write('{"t":"c","uid":"kaputt","ts":1.0,"n":0,"r":{"app_i')
    # Der intakte Satz bleibt zaehlbar, der abgerissene wird uebersprungen.
    assert spool.spool_stats()["pending"] == 1


def test_over_cap_refuses_loudly_and_counts(spool_dir, monkeypatch):
    """Eine volle Platte hat auf dieser Flotte schon einen Postgres erschlagen.
    Der Puffer nimmt dann nichts mehr an — aber er zaehlt mit, statt so zu tun,
    als sei alles in Ordnung."""
    monkeypatch.setattr(spool, "MAX_BYTES", 1)
    spool.append_call(spool.new_call_uid(), _rec())  # legt die Datei an
    assert spool.append_call(spool.new_call_uid(), _rec()) is False
    assert spool.spool_stats()["undurable_calls"] >= 1


# ---------------------------------------------------------------------------
# Boot-Gate: eine Sicherung, die sich lautlos abschaltet, ist keine
# ---------------------------------------------------------------------------

def test_boot_gate_refuses_an_unusable_spool(spool_dir, monkeypatch):
    monkeypatch.setattr(spool, "SPOOL_DIR", "/proc/dieses/geht/nicht")
    with pytest.raises(RuntimeError, match="ledger spool"):
        spool.assert_spool_ready()


def test_boot_gate_passes_when_usable(spool_dir):
    spool.assert_spool_ready()  # darf nicht werfen


def test_boot_gate_allows_an_explicit_no(spool_dir, monkeypatch):
    """Ausschalten ist erlaubt — aber nur als ausgesprochene Entscheidung."""
    monkeypatch.setenv("BRIDGE_LEDGER_SPOOL_ENABLED", "false")
    monkeypatch.setattr(spool, "SPOOL_DIR", "/proc/dieses/geht/nicht")
    spool.assert_spool_ready()  # kein Boot-Abbruch
