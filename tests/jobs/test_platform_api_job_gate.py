"""BR8: what the deploy gate in front of a platform-api recreation sees.

Store side: find_active counts the jobs that depend on a platform-api right now
(running, or pending and due), separately from parked ones; ``origin`` limits it
to jobs whose budget home is that bridge (ADR-0011). Probe side:
scripts/platform_api_job_gate.py asks the local store (all jobs) and every peer
store (jobs with THIS bridge as origin) and turns the answers into an exit code
for bridge-deploy.sh.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest

from src.jobs import store

PROBE = Path(__file__).resolve().parents[2] / "scripts" / "platform_api_job_gate.py"
NOW = datetime(2026, 10, 10, 4, 9, 19, tzinfo=timezone.utc)


def _row(job_id, status, is_active, origin="dev", deferred_until=None):
    return {"job_id": job_id, "kind": "research", "status": status, "origin": origin,
            "updated_at": NOW, "deferred_until": deferred_until, "is_active": is_active}


def _pool_returning(rows):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire
    return pool, conn


async def test_find_active_splits_running_from_parked(monkeypatch):
    pool, conn = _pool_returning([
        _row("job_prod_a", "running", True),
        _row("job_prod_b", "pending", True),
        _row("job_prod_c", "pending", False, deferred_until=NOW),
    ])
    monkeypatch.setattr(store, "get_pool", lambda: pool)
    result = await store.find_active(origin="DEV")
    assert result["active"] == 2 and result["waiting"] == 1
    assert [j["job_id"] for j in result["jobs"]] == ["job_prod_a", "job_prod_b", "job_prod_c"]
    sql, *params = conn.fetch.await_args.args
    assert params == ["dev"]
    assert "attribution->>'bridge_origin' = $1" in sql
    assert "status IN ('pending', 'running')" in sql


async def test_find_active_without_origin_counts_every_job(monkeypatch):
    pool, conn = _pool_returning([])
    monkeypatch.setattr(store, "get_pool", lambda: pool)
    assert (await store.find_active())["active"] == 0
    sql, *params = conn.fetch.await_args.args
    assert params == [] and "bridge_origin" not in sql.split("FROM")[1]


async def test_find_active_rejects_bad_limit():
    with pytest.raises(ValueError):
        await store.find_active(limit=0)


# --- the probe, against real HTTP ------------------------------------------------

class _Fake:
    """One platform-api: answers /v1/internal/jobs-maintenance/active."""

    def __init__(self, status=200, active=0, token="t"):
        self.status, self.active, self.token = status, active, token
        self.seen: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                url = urlparse(self.path)
                fake.seen.append({"path": url.path, "query": parse_qs(url.query),
                                  "token": self.headers.get("X-Bridge-Service-Token")})
                if fake.status != 200:
                    self.send_response(fake.status)
                    self.end_headers()
                    return
                body = json.dumps({"active": fake.active, "waiting": 0, "jobs": [
                    {"job_id": f"job_x_{i}", "kind": "research", "status": "running",
                     "origin": "dev"} for i in range(fake.active)]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


def _probe(local: _Fake, peer: _Fake | None, own="dev"):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FEDERATION", "BRIDGE_"))}
    env.update(BRIDGE_ORIGIN_ID=own, BRIDGE_SERVICE_TOKEN="local-token",
               PLATFORM_API_URL=local.url)
    if peer is not None:
        env["FEDERATION_PEERS"] = json.dumps(
            {"prod": {"platformUrl": peer.url, "tokenEnv": "FEDERATION_TOKEN_PROD"}})
        env["FEDERATION_TOKEN_PROD"] = "peer-token"
    # Exactly as the deploy runs it: the script over stdin, inside the worker.
    done = subprocess.run([sys.executable, "-I", "-"], input=PROBE.read_text(),
                          capture_output=True, text=True, env=env, timeout=30)
    return done.returncode, [json.loads(line) for line in done.stdout.splitlines()]


@pytest.fixture
def fakes():
    made = []

    def make(**kw):
        made.append(_Fake(**kw))
        return made[-1]

    yield make
    for fake in made:
        fake.close()


def test_br7_case_dev_job_on_prod_worker_blocks_the_dev_deploy(fakes):
    """The dev store is empty; the dev-origin job runs on a PROD worker. The
    gate must see it through the prod store, asked with origin=dev."""
    local, peer = fakes(active=0), fakes(active=1)
    rc, lines = _probe(local, peer)
    assert rc == 1
    assert peer.seen[0]["query"] == {"origin": ["dev"]}
    assert peer.seen[0]["token"] == "peer-token"
    assert local.seen[0]["query"] == {} and local.seen[0]["token"] == "local-token"
    assert lines[1]["target"] == "peer:prod" and lines[1]["state"] == "busy"


def test_all_idle_lets_the_deploy_go(fakes):
    rc, _ = _probe(fakes(), fakes())
    assert rc == 0


def test_local_jobs_block_too(fakes):
    rc, _ = _probe(fakes(active=2), fakes())
    assert rc == 1


def test_missing_endpoint_is_its_own_answer(fakes):
    rc, _ = _probe(fakes(), fakes(status=404))
    assert rc == 3


@pytest.mark.parametrize("status", [401, 500])
def test_any_other_failure_is_not_provable(fakes, status):
    rc, _ = _probe(fakes(), fakes(status=status))
    assert rc == 2


def test_unreachable_peer_is_not_provable(fakes):
    local = fakes()
    dead = fakes()
    dead.close()
    rc, lines = _probe(local, dead)
    assert rc == 2
    assert lines[1]["state"] == "error"


def test_busy_wins_over_missing(fakes):
    """Waiting is right as soon as one known job runs, even if another target
    cannot be seen yet."""
    rc, _ = _probe(fakes(active=1), fakes(status=404))
    assert rc == 1
