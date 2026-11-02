"""Credit pool: reservation outcome on abort (02.11.2026).

(1) provably NOT sent to Anthropic  -> settle rejected=True (released)
(2) sent, outcome unknown           -> retained at upper bound + /unknown marker
Neutral fixtures, no network: fake coordinator + httpx.MockTransport.
Scenario test at the end follows the ``test_szenario_*`` convention of
tests/unit (the bridge has no pyramid test harness of its own).
"""
import asyncio
import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from src.providers import credit_pool as client
from src.providers import credit_pool_routes as ledger

BODY = {"model": "claude-haiku-5-5", "max_tokens": 10, "messages": []}
_REAL_CLIENT = httpx.AsyncClient
USAGE = {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


class Coordinator:
    """Fake coordinator with a tiny in-memory ledger (state per reservation)."""

    def __init__(self, reserve_error=None, reserve_delay=0.0, mark_delay=0.0, settle_delay=0.0,
                 commit_before_reply=False, accounts=1):
        self.calls = []
        self.state = {}
        self.marks = {}
        self.reserve_error = reserve_error
        self.reserve_delay = reserve_delay
        self.mark_delay = mark_delay
        self.settle_delay = settle_delay
        # True: /reserve commits the row first, THEN its reply is lost
        # (delay/error) -- the row exists although the client never saw it.
        self.commit_before_reply = commit_before_reply
        self.accounts = [{"id": f"a{i}", "org_id": "o", "grant_id": f"g{i}", "api_key": "k"}
                         for i in range(accounts)]

    async def __call__(self, method, path, data=None):
        self.calls.append(path)
        if path == "/accounts":
            return {"accounts": self.accounts}
        if path == "/reserve":
            if self.commit_before_reply:
                self.state[data["id"]] = "reserved"
            if self.reserve_delay:
                await asyncio.sleep(self.reserve_delay)
            if self.reserve_error:
                raise self.reserve_error
            self.state[data["id"]] = "reserved"
            return {"state": "reserved"}
        if path == "/settle":
            if self.settle_delay:
                await asyncio.sleep(self.settle_delay)
            if data["id"] not in self.state:
                raise HTTPException(503, "Anthropic credit coordinator rejected request")
            self.state[data["id"]] = "released" if data.get("rejected") else "settled"
            return {"state": self.state[data["id"]]}
        if path == "/unknown":
            if self.mark_delay:
                await asyncio.sleep(self.mark_delay)
            self.marks[data["id"]] = data["reason"]
            return {"state": "reserved", "outcome": "unknown"}
        raise AssertionError(path)

    def only(self):
        assert len(self.state) <= 1
        return next(iter(self.state.values()), None)


@pytest.fixture
def pool(monkeypatch):
    def make(generate, **kw):
        coord = Coordinator(**kw)

        async def handler(req):
            if req.url.path.endswith("count_tokens"):
                return httpx.Response(200, json={"input_tokens": 5})
            return await generate(req)

        monkeypatch.setattr(client, "coordinator", coord)
        monkeypatch.setattr(client.httpx, "AsyncClient",
                            lambda **k: _REAL_CLIENT(transport=httpx.MockTransport(handler), **k))
        return coord
    return make


def _raise(exc):
    async def gen(req):
        raise exc
    return gen


# --- (1) provably not sent -> released --------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ConnectTimeout("connect"),
                                 httpx.PoolTimeout("pool")])
async def test_connect_or_pool_failure_releases(pool, exc):
    coord = pool(_raise(exc))
    with pytest.raises(type(exc)):
        await client.post_messages(BODY, 10)
    assert coord.only() == "released" and coord.marks == {}


@pytest.mark.asyncio
async def test_coordinator_failure_on_reserve_releases_possible_row(pool):
    async def gen(req):
        raise AssertionError("must not post without reservation")
    coord = pool(gen, reserve_error=HTTPException(503, "coordinator unavailable"))
    with pytest.raises(HTTPException) as e:
        await client.post_messages(BODY, 10)
    assert e.value.status_code == 503
    assert coord.calls[-1] == "/settle"  # best-effort release; 404/503 only logged


@pytest.mark.asyncio
async def test_cancel_during_reserve_releases_and_never_posts(pool):
    posted = []

    async def gen(req):
        posted.append(1)
        return httpx.Response(200, json={"usage": USAGE})
    coord = pool(gen, reserve_delay=5)
    task = asyncio.create_task(client.post_messages(BODY, 10))
    while "/reserve" not in coord.calls:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert posted == [] and coord.calls[-1] == "/settle" and coord.marks == {}


# --- (2) sent, outcome unknown -> retained + marked --------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [httpx.ReadTimeout("lost"), httpx.RemoteProtocolError("reset"),
                                 httpx.WriteError("broken pipe")])
async def test_transport_error_after_send_is_marked_not_released(pool, exc):
    coord = pool(_raise(exc))
    with pytest.raises(type(exc)):
        await client.post_messages(BODY, 10)
    assert coord.only() == "reserved"
    assert list(coord.marks.values()) == ["transport_error"]
    assert "/settle" not in coord.calls


@pytest.mark.asyncio
async def test_cancel_during_post_is_marked_and_cancellation_propagates(pool):
    started = asyncio.Event()

    async def gen(req):
        started.set()
        await asyncio.sleep(60)
    coord = pool(gen)
    task = asyncio.create_task(client.post_messages(BODY, 10))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert coord.only() == "reserved"
    assert list(coord.marks.values()) == ["cancelled"]


@pytest.mark.asyncio
async def test_marker_survives_a_second_cancellation(pool):
    started = asyncio.Event()

    async def gen(req):
        started.set()
        await asyncio.sleep(60)
    coord = pool(gen, mark_delay=0.05)
    task = asyncio.create_task(client.post_messages(BODY, 10))
    await started.wait()
    task.cancel()
    while "/unknown" not in coord.calls:
        await asyncio.sleep(0)
    task.cancel()  # impatient caller cancels the cleanup too
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.gather(*list(client._background))
    assert list(coord.marks.values()) == ["cancelled"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 529])
async def test_5xx_is_returned_and_marked(pool, status):
    async def gen(req):
        return httpx.Response(status, json={"error": "overloaded"})
    coord = pool(gen)
    response, meta = await client.post_messages(BODY, 10)
    assert response.status_code == status
    assert coord.only() == "reserved" and list(coord.marks.values()) == ["http_status"]


@pytest.mark.asyncio
async def test_paid_200_with_unreadable_usage_is_marked(pool):
    async def gen(req):
        return httpx.Response(200, json={"content": []})
    coord = pool(gen)
    response, _ = await client.post_messages(BODY, 10)
    assert response.status_code == 200
    assert coord.only() == "reserved" and list(coord.marks.values()) == ["unreadable_usage"]


@pytest.mark.asyncio
async def test_success_and_4xx_unchanged(pool):
    async def ok(req):
        return httpx.Response(200, json={"usage": USAGE, "content": []})
    coord = pool(ok)
    await client.post_messages(BODY, 10)
    assert coord.only() == "settled" and coord.marks == {}

    async def bad(req):
        return httpx.Response(400, json={"error": "invalid request"})
    coord = pool(bad)
    response, _ = await client.post_messages(BODY, 10)
    assert response.status_code == 400 and coord.only() == "released" and coord.marks == {}


@pytest.mark.asyncio
async def test_unreachable_coordinator_on_marker_keeps_original_error(pool, monkeypatch):
    coord = pool(_raise(httpx.ReadTimeout("lost")))
    original = coord.__call__

    async def failing(method, path, data=None):
        if path == "/unknown":
            raise HTTPException(503, "Anthropic credit coordinator unavailable")
        return await original(method, path, data)
    monkeypatch.setattr(client, "coordinator", failing)
    with pytest.raises(httpx.ReadTimeout):
        await client.post_messages(BODY, 10)
    assert coord.only() == "reserved"


# --- caller cancellation always propagates (no swallowed CancelledError) -----

async def _cancel_when(coord, path, task):
    while path not in coord.calls:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.gather(*list(client._background))


@pytest.mark.asyncio
async def test_cancel_during_settle_of_paid_200_propagates_and_still_settles(pool):
    async def ok(req):
        return httpx.Response(200, json={"usage": USAGE, "content": []})
    coord = pool(ok, settle_delay=0.05)
    task = asyncio.create_task(client.post_messages(BODY, 10))
    await _cancel_when(coord, "/settle", task)
    assert coord.only() == "settled" and coord.calls.count("/reserve") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status,text", [(401, "unauthorized"), (429, "rate limited"),
                                         (400, "Your credit balance is too low")])
async def test_cancel_during_settle_of_4xx_never_tries_next_account(pool, status, text):
    posts = []

    async def rejected(req):
        posts.append(1)
        return httpx.Response(status, json={"error": text})
    coord = pool(rejected, settle_delay=0.05, accounts=2)
    task = asyncio.create_task(client.post_messages(BODY, 10))
    await _cancel_when(coord, "/settle", task)
    # Without the fix the loop moved on: count_tokens, /reserve, a 2nd post.
    assert coord.calls.count("/reserve") == 1 and posts == [1]
    assert list(coord.state.values()) == ["released"]


@pytest.mark.asyncio
async def test_4xx_without_cancel_still_moves_to_next_account(pool):
    async def rejected(req):
        return httpx.Response(401, json={"error": "unauthorized"})
    coord = pool(rejected, accounts=2)
    with pytest.raises(HTTPException) as e:
        await client.post_messages(BODY, 10)
    assert e.value.status_code == 429 and coord.calls.count("/reserve") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 529])
async def test_cancel_during_marker_of_5xx_propagates(pool, status):
    async def overloaded(req):
        return httpx.Response(status, json={"error": "overloaded"})
    coord = pool(overloaded, mark_delay=0.05, accounts=2)
    task = asyncio.create_task(client.post_messages(BODY, 10))
    await _cancel_when(coord, "/unknown", task)
    assert coord.calls.count("/reserve") == 1 and "/settle" not in coord.calls
    assert coord.only() == "reserved" and list(coord.marks.values()) == ["http_status"]


@pytest.mark.asyncio
async def test_cancel_during_marker_of_unreadable_200_propagates(pool):
    async def unreadable(req):
        return httpx.Response(200, json={"content": []})
    coord = pool(unreadable, mark_delay=0.05)
    task = asyncio.create_task(client.post_messages(BODY, 10))
    await _cancel_when(coord, "/unknown", task)
    assert coord.only() == "reserved" and list(coord.marks.values()) == ["unreadable_usage"]


@pytest.mark.asyncio
async def test_cancel_during_release_after_connect_error_raises_cancelled(pool):
    coord = pool(_raise(httpx.ConnectError("refused")), settle_delay=0.05)
    task = asyncio.create_task(client.post_messages(BODY, 10))
    await _cancel_when(coord, "/settle", task)  # CancelledError, not ConnectError
    assert coord.only() == "released"


@pytest.mark.asyncio
async def test_cancel_during_release_after_reserve_failure_raises_cancelled(pool):
    async def gen(req):
        raise AssertionError("must not post without reservation")
    coord = pool(gen, reserve_error=HTTPException(503, "coordinator unavailable"),
                 commit_before_reply=True, settle_delay=0.05)
    task = asyncio.create_task(client.post_messages(BODY, 10))
    await _cancel_when(coord, "/settle", task)  # CancelledError, not HTTPException
    assert coord.only() == "released"


@pytest.mark.asyncio
async def test_cancel_during_reserve_releases_the_committed_row(pool):
    """/reserve committed, its reply is lost because the caller is cancelled:
    the row exists in the ledger and the follow-up release frees it."""
    posted = []

    async def gen(req):
        posted.append(1)
        return httpx.Response(200, json={"usage": USAGE})
    coord = pool(gen, reserve_delay=5, commit_before_reply=True)
    task = asyncio.create_task(client.post_messages(BODY, 10))
    while "/reserve" not in coord.calls:
        await asyncio.sleep(0)
    assert coord.only() == "reserved"  # row exists before the reply
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.gather(*list(client._background))
    assert posted == [] and coord.only() == "released" and coord.marks == {}


@pytest.mark.asyncio
async def test_lost_reserve_reply_with_committed_row_is_released(pool):
    async def gen(req):
        raise AssertionError("must not post without reservation")
    coord = pool(gen, reserve_error=HTTPException(503, "coordinator unavailable"), commit_before_reply=True)
    with pytest.raises(HTTPException) as e:
        await client.post_messages(BODY, 10)
    assert e.value.status_code == 503 and coord.only() == "released"


# --- coordinator side without a database ------------------------------------

class FakeConn:
    def __init__(self, rows):
        self.rows = rows  # id -> dict(state, created_at, charged_usd, grant_id, usage)
        self.sql = []

    def transaction(self):
        return _Ctx(None)

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        if "FOR UPDATE" in sql:
            row = self.rows.get(args[0])
            return {"state": row["state"]} if row else None
        grants, after = args
        now = datetime.now(timezone.utc)
        res = [r for r in self.rows.values() if r["grant_id"] in grants and r["state"] == "reserved"]
        hang = [r for r in res if r["created_at"] < now - after]
        unk = [r for r in res if (r.get("usage") or {}).get("outcome") == "unknown"]
        return {"reserved": len(res), "oldest": min((r["created_at"] for r in res), default=None),
                "hanging": len(hang), "hanging_usd": sum((r["charged_usd"] for r in hang), Decimal(0)),
                "unknown": len(unk), "unknown_usd": sum((r["charged_usd"] for r in unk), Decimal(0)),
                "now": now}

    async def execute(self, sql, *args):
        self.sql.append(sql)
        self.rows[args[0]]["usage"] = {"outcome": "unknown", "reason": args[1]}


class _Ctx:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *a):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Ctx(self.conn)


def _rows():
    now = datetime.now(timezone.utc)
    return {
        "r-old": {"grant_id": "grant-one", "state": "reserved", "charged_usd": Decimal("150"),
                  "created_at": now - timedelta(hours=30)},
        "r-new": {"grant_id": "grant-one", "state": "reserved", "charged_usd": Decimal("2"),
                  "created_at": now - timedelta(seconds=30)},
        "r-done": {"grant_id": "grant-one", "state": "settled", "charged_usd": Decimal("1"),
                   "created_at": now - timedelta(days=2)},
    }


@pytest.mark.asyncio
async def test_unknown_route_marks_only_reserved_rows(monkeypatch):
    conn = FakeConn(_rows())
    monkeypatch.setattr(ledger, "get_pool", lambda: FakePool(conn))
    conn.rows[uid := uuid4()] = conn.rows.pop("r-new")
    out = await ledger.unknown(ledger.Unknown(id=uid, reason="cancelled"))
    assert out["state"] == "reserved" and out["outcome"] == "unknown"
    assert conn.rows[uid]["usage"]["reason"] == "cancelled" and conn.rows[uid]["state"] == "reserved"
    conn.rows[done := uuid4()] = conn.rows.pop("r-done")
    assert (await ledger.unknown(ledger.Unknown(id=done, reason="cancelled")))["state"] == "settled"
    assert "usage" not in conn.rows[done]
    with pytest.raises(HTTPException) as e:
        await ledger.unknown(ledger.Unknown(id=uuid4(), reason="cancelled"))
    assert e.value.status_code == 404
    with pytest.raises(Exception):
        ledger.Unknown(id=uuid4(), reason="anything else")


@pytest.mark.asyncio
async def test_hanging_aggregate_has_age_sum_and_no_identities():
    conn = FakeConn(_rows())
    out = await ledger.hanging(conn, ["grant-one"])
    assert out["reserved_count"] == 2
    assert out["hanging_reserved_count"] == 1 and out["hanging_reserved_usd"] == 150.0
    assert 30 * 3600 - 5 <= out["oldest_reserved_age_s"] <= 30 * 3600 + 5
    assert out["hanging_after_s"] == 3600 and out["unknown_outcome_count"] == 0
    wire = json.dumps(out)
    for ident in ("r-old", "r-new", "grant-one"):
        assert ident not in wire
    empty = await ledger.hanging(FakeConn({}), ["grant-one"])
    assert empty["reserved_count"] == 0 and empty["oldest_reserved_age_s"] is None


@pytest.fixture
def configured(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    data = {"accounts": [{"id": "werking", "org_id": "org-one", "api_key": "secret-test-key",
        "billing_guard": "free_only_verified", "observed_at": now.isoformat(), "grants": [{"id": "grant-one",
        "granted_usd": 200, "remaining_usd": 200, "expires_at": (now + timedelta(days=3)).isoformat()}]}]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_ACCOUNTS_FILE", str(path))
    return path, data


class SummaryConn(FakeConn):
    async def fetchrow(self, sql, *args):
        if "AS requests" in sql:  # status()
            res = [r for r in self.rows.values() if r["grant_id"] == args[0]]
            return {"requests": len(res), "pending": sum(r["state"] == "reserved" for r in res),
                    "charged": sum((r["charged_usd"] for r in res), Decimal(0))}
        if "AS total" in sql:  # available()
            res = [r for r in self.rows.values() if r["grant_id"] == args[0]]
            total = sum((r["charged_usd"] for r in res), Decimal(0))
            return {"total": total, "recent": total}
        return await super().fetchrow(sql, *args)

    async def fetchval(self, sql, *args):
        return sum((r["charged_usd"] for r in self.rows.values()
                    if r["grant_id"] == args[0] and r["state"] == "reserved"), Decimal(0))


@pytest.mark.asyncio
async def test_status_and_summary_show_hanging_reservations(configured, monkeypatch):
    path, data = configured
    monkeypatch.setattr(ledger, "get_pool", lambda: FakePool(SummaryConn(_rows())))
    summary = await ledger.summary()
    assert summary["hanging_reserved_count"] == 1 and summary["hanging_reserved_usd"] == 150.0
    assert summary["reserved_count"] == 2 and summary["oldest_reserved_age_s"] >= 30 * 3600 - 5
    wire = json.dumps(summary)
    for secret in ("secret-test-key", "org-one", "grant-one", "werking", "r-old"):
        assert secret not in wire
    status = (await ledger.status())["accounts"][0]
    assert status["hanging_reserved_count"] == 1 and status["unknown_outcome_count"] == 0
    # Exhausted grant (not eligible) still shows its stuck money in /summary.
    data["accounts"][0]["grants"][0]["remaining_usd"] = 1
    path.write_text(json.dumps(data))
    assert (await ledger.summary())["hanging_reserved_usd"] == 150.0


def test_metrics_snapshot_passes_aggregates_and_rejects_garbage(monkeypatch):
    from src.metrics_reader import api_reserve
    now = datetime.now(timezone.utc).isoformat()
    base = {"pool_id": "anthropic-subscription-api-reserve", "currency": "USD", "shared_across_bridges": True,
            "state": "available", "granted_usd": 200, "remaining_usd": 200, "available_usd": 40,
            "reserved_usd": 152, "account_count": 1, "eligible_accounts": 1, "stale_accounts": 0,
            "blocked_accounts": 0, "measured_at": now, "checked_at": now, "next_expiry_at": now,
            "automatic_paid_fallback": False, "supported_operations": ["text", "vision"],
            "supports_tools": False, "supports_claude_code": False, "requires_stateless_request": True,
            "reserved_count": 2, "oldest_reserved_age_s": 108000, "hanging_reserved_count": 1,
            "hanging_reserved_usd": 150.0, "hanging_after_s": 3600, "unknown_outcome_count": 1,
            "unknown_outcome_usd": 2.0, "reservation_ids": ["must-not-pass"]}

    def serve(payload):
        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, *a):
                return json.dumps(payload).encode()
        monkeypatch.setattr(api_reserve.urllib.request, "urlopen", lambda *a, **k: R())
        api_reserve._cached = None
        return api_reserve.api_reserve_snapshot()

    monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_URL", "http://pool")
    monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_TOKEN", "t")
    out = serve(base)
    assert out["hanging_reserved_usd"] == 150.0 and out["oldest_reserved_age_s"] == 108000
    assert out["unknown_outcome_count"] == 1 and "reservation_ids" not in out
    # Older coordinator without the fields still works.
    legacy = {k: v for k, v in base.items() if k not in api_reserve._COUNTS and k != "oldest_reserved_age_s"}
    assert serve(legacy)["state"] == "available"
    assert serve({**base, "hanging_reserved_usd": "150"}).get("reason") == "credit_coordinator_unavailable"
    api_reserve._cached = None


# --- regression scenario (the 02.11.2026 Energy-pool incident) ----------------

@pytest.mark.asyncio
async def test_szenario_abgebrochene_laeufe_haengen_nicht_still(pool):
    """A batch of pool calls is cancelled by its caller's deadline. Before the
    fix every one of them stayed 'reserved' unmarked forever. Now: calls
    cancelled before the post are released; calls cancelled during the post
    stay debited (may be billed) but are marked and therefore visible."""
    gate = asyncio.Event()

    async def gen(req):
        gate.set()
        await asyncio.sleep(60)
    coord = pool(gen)
    sent = [asyncio.create_task(client.post_messages(BODY, 10)) for _ in range(3)]
    await gate.wait()
    for _ in range(5):
        await asyncio.sleep(0)
    coord.reserve_delay = 60  # next callers hang in /reserve
    unsent = [asyncio.create_task(client.post_messages(BODY, 10)) for _ in range(2)]
    while coord.calls.count("/reserve") < 5:
        await asyncio.sleep(0)
    for t in sent + unsent:
        t.cancel()
    results = await asyncio.gather(*sent, *unsent, return_exceptions=True)
    assert all(isinstance(r, asyncio.CancelledError) for r in results)
    states = list(coord.state.values())
    assert states.count("reserved") == 3  # unsent ones never committed in the fake
    assert sorted(coord.marks.values()) == ["cancelled"] * 3
    assert all(coord.state[i] == "reserved" for i in coord.marks)
    assert coord.calls.count("/settle") == 2  # release attempts for the unsent
