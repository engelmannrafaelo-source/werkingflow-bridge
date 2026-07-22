"""RequestLogStore: Rotation + tail-bounded query + Reader-TTL-Cache.

Hintergrund (2026-07-22): request_log.*.jsonl wuchsen ohne Rotation auf
4×750 MB; query() las jede Datei komplett → metrics-reader bei 389% CPU,
Healthcheck tot, Deploy-Validierung schlug fehl. Diese Tests nageln die drei
Gegenmaßnahmen fest: Writer-Rotation, budgetiertes Tail-Lesen mit EHRLICHEM
coverage_complete-Flag (kein Silent-Truncate) und den TTL-Cache im Reader.
"""

import json
import os
import time

import pytest

from src.middleware import bridge_metrics_store as store_mod
from src.middleware.bridge_metrics_store import RequestLogStore


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "METRICS_DIR", str(tmp_path))
    monkeypatch.setenv("INSTANCE_NAME", "workertest")
    s = RequestLogStore.__new__(RequestLogStore)
    s._worker_id = "workertest"
    s._jsonl_path = os.path.join(str(tmp_path), "request_log.workertest.jsonl")
    return s


def _write_entries(path, entries):
    with open(path, "a") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")


def _entry(ts, endpoint="/v1/x", status=200, dur=0.1):
    return {"ts": ts, "method": "GET", "endpoint": endpoint,
            "status": status, "duration_s": dur, "tools": False,
            "worker": "workertest", "client": "t"}


class TestRotation:
    def test_rotates_over_threshold(self, store, monkeypatch):
        monkeypatch.setattr(store_mod, "REQUEST_LOG_MAX_BYTES", 200)
        for _ in range(10):
            store.record("GET", "/v1/x", 200, 0.1)
        assert os.path.exists(store._jsonl_path + ".1"), "Alt-Generation fehlt"
        # aktive Datei ist nach der Rotation klein (nur Einträge seither) —
        # oder existiert gerade nicht, wenn der letzte Record selbst rotierte.
        if os.path.exists(store._jsonl_path):
            assert os.path.getsize(store._jsonl_path) <= 200 + 200
        # Folge-Record erzeugt die aktive Datei wieder
        store.record("GET", "/v1/x", 200, 0.1)
        assert os.path.exists(store._jsonl_path)

    def test_no_rotation_below_threshold(self, store, monkeypatch):
        monkeypatch.setattr(store_mod, "REQUEST_LOG_MAX_BYTES", 10_000_000)
        for _ in range(5):
            store.record("GET", "/v1/x", 200, 0.1)
        assert not os.path.exists(store._jsonl_path + ".1")


class TestTailBoundedQuery:
    def test_small_file_full_coverage(self, store):
        now = time.time()
        _write_entries(store._jsonl_path, [_entry(now - 10), _entry(now - 5)])
        res = store.query(hours=1)
        assert res["summary"]["total_requests"] == 2
        assert res["coverage_complete"] is True

    def test_tail_covers_cutoff(self, store, monkeypatch):
        """Tail-Fenster reicht bis vor den Cutoff → alles im Fenster gefunden."""
        now = time.time()
        # 200 alte Einträge (vor Cutoff) + 3 neue; Budget klein, aber der
        # Tail enthält mindestens einen vor-Cutoff-Eintrag → covered.
        old = [_entry(now - 7200) for _ in range(200)]
        new = [_entry(now - 10), _entry(now - 5), _entry(now - 1)]
        _write_entries(store._jsonl_path, old + new)
        size = os.path.getsize(store._jsonl_path)
        monkeypatch.setattr(store_mod, "_SCAN_BYTES_FLOOR", size // 2)
        monkeypatch.setattr(store_mod, "_SCAN_BYTES_CAP", size // 2)
        res = store.query(hours=1)
        assert res["summary"]["total_requests"] == 3
        assert res["coverage_complete"] is True

    def test_truncated_window_is_flagged_not_silent(self, store, monkeypatch):
        """Budget so klein, dass in-range-Einträge außerhalb des Fensters
        liegen → coverage_complete=False, KEIN stilles Fehlen."""
        now = time.time()
        in_range = [_entry(now - 1800) for _ in range(300)]
        _write_entries(store._jsonl_path, in_range)
        monkeypatch.setattr(store_mod, "_SCAN_BYTES_FLOOR", 512)
        monkeypatch.setattr(store_mod, "_SCAN_BYTES_CAP", 512)
        res = store.query(hours=1)
        assert res["coverage_complete"] is False
        assert res["summary"]["total_requests"] < 300

    def test_rotated_generation_extends_coverage(self, store, monkeypatch):
        """Reicht der Tail der aktiven Datei nicht, wird .1 mitgelesen."""
        now = time.time()
        _write_entries(store._jsonl_path + ".1",
                       [_entry(now - 7200), _entry(now - 900, endpoint="/v1/old-gen")])
        _write_entries(store._jsonl_path, [_entry(now - 10) for _ in range(50)])
        # Budget deckt die aktive Datei nur teilweise → .1 wird konsultiert
        monkeypatch.setattr(store_mod, "_SCAN_BYTES_FLOOR", 2048)
        monkeypatch.setattr(store_mod, "_SCAN_BYTES_CAP", 2048)
        res = store.query(hours=1)
        assert "/v1/old-gen" in res["endpoints"]
        assert res["coverage_complete"] is True

    def test_filters_and_aggregation_unchanged(self, store):
        now = time.time()
        _write_entries(store._jsonl_path, [
            _entry(now - 10, endpoint="/v1/a", status=200),
            _entry(now - 9, endpoint="/v1/a", status=500),
            _entry(now - 8, endpoint="/v1/b", status=200),
        ])
        res = store.query(hours=1, endpoint_filter="/v1/a")
        assert res["summary"]["total_requests"] == 2
        assert res["endpoints"]["/v1/a"]["errors"] == 1
        res_err = store.query(hours=1, status_filter="error")
        assert res_err["summary"]["total_requests"] == 1


class TestReaderTTLCache:
    def test_second_call_within_ttl_hits_cache(self, monkeypatch):
        from src.metrics_reader import main as reader_main
        calls = {"n": 0}

        class FakeStore:
            def query(self, **kw):
                calls["n"] += 1
                return {"entries": [], "summary": {}, "endpoints": {},
                        "period_hours": kw.get("hours"), "coverage_complete": True}

        monkeypatch.setattr(reader_main, "get_request_log", lambda: FakeStore())
        monkeypatch.setattr(reader_main, "_fetch_prod", lambda *a, **k: None)
        reader_main._REQUEST_LOG_CACHE.clear()
        r1 = reader_main.get_request_log_endpoint(hours=24, endpoint=None, status=None, limit=200)
        r2 = reader_main.get_request_log_endpoint(hours=24, endpoint=None, status=None, limit=200)
        assert calls["n"] == 1, "zweiter Call muss aus dem Cache kommen"
        assert r1 == r2
        # anderer Key = eigener Scan
        reader_main.get_request_log_endpoint(hours=1, endpoint=None, status=None, limit=200)
        assert calls["n"] == 2
