"""CHATFWD (12.10.2026): a chat call relayed to another worker must not
outlive its caller, must only go to a worker whose budget carries it, and the
429 must name the real reason.

Gegenprobe of the scenario 17:31-17:35Z (Dev-Bridge, efa0fdc's predecessor
600084c, same relay code): Energy's methodology prompt (est. 163,562 tokens)
hit worker2/worker4 (account at its weekly limit / out of budget); both
relayed it to worker1 with a fixed 30 s timeout. worker1 needed 64-119 s, so
the relaying worker gave up and answered 429 while worker1 ran the call to the
end — paid for, holding worker1's budget (CLI processes 100 s / 70 s old at
17:35:36Z). The next attempt queued on worker1 and got 429 too; the client saw
"All Anthropic accounts at weekly limit" although worker1's account was free.

Scaled down: the CLI stand-in needs ``LAUF_S`` seconds, the caller gives up
after ``AUFGABE_S`` (Energy/nginx/the relaying worker in the original). The
peer runs as a real uvicorn server, because only a real connection shows the
orphan: in-process (ASGITransport) the peer's handler is cancelled together
with the client, which hides exactly the defect.

Every test here was red on efa0fdc.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock as _MagicMock

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

import asyncio  # noqa: E402
import importlib.util  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import requests  # noqa: E402
import uvicorn  # noqa: E402

import src.claude_cli as claude_cli  # noqa: E402
import src.main as main  # noqa: E402
from src.claude_cli import ClaudeCodeCLI, RateLimitError  # noqa: E402
from src.middleware.bridge_error import raise_account_exhausted  # noqa: E402
from tests import sdk_strom as sdk  # noqa: E402

FRIST_HEADER = "x-bridge-chat-frist-bis"  # literal: the module is new
EST_TOKENS = 163_562  # the methodology prompt's estimate, worker log 17:31
LAUF_S = 3.0  # worker1 needed 64-119 s
AUFGABE_S = 1.0  # the relaying worker gave up after 30 s
BODY = {
    "model": "claude-sonnet-5-5",
    "messages": [{"role": "user", "content": "Funktionstest ohne Kundendaten."}],
}

# Pool at 17:31Z: worker2/worker3 at the weekly limit, worker4 with a budget
# too small for the prompt, worker1's budget held by two orphans of the same
# prompt. Numbers in the same order as KD's measurement (in flight 163-327k).
POOL_BELEGT = {
    "ki1": {"worker": "worker1", "available": True, "cooldown_remaining_s": 0,
            "effective_cap_tokens": 400_000, "current_in_flight_tokens": 327_124,
            "weekly_percent": 40.0},
    "ki2": {"worker": "worker2", "available": False, "cooldown_remaining_s": 7_200,
            "capacity_lock_remaining_s": 7_200, "usage_known": True,
            "effective_cap_tokens": 400_000, "current_in_flight_tokens": 0,
            "weekly_percent": 100.0},
    "ki3": {"worker": "worker3", "available": False, "cooldown_remaining_s": 9_000,
            "capacity_lock_remaining_s": 9_000, "usage_known": True,
            "effective_cap_tokens": 400_000, "current_in_flight_tokens": 0,
            "weekly_percent": 100.0},
    "ki4": {"worker": "worker4", "available": True, "cooldown_remaining_s": 0,
            "effective_cap_tokens": 120_000, "current_in_flight_tokens": 0,
            "weekly_percent": 94.0},
}
# Same pool before the orphans: worker1 carries the prompt.
POOL_FREI = {k: dict(v) for k, v in POOL_BELEGT.items()}
POOL_FREI["ki1"]["current_in_flight_tokens"] = 0
# Every account at its limit.
POOL_LIMIT = {k: dict(v, available=False) for k, v in POOL_BELEGT.items()}
POOL_LIMIT["ki1"].update(cooldown_remaining_s=1_800, capacity_lock_remaining_s=1_800,
                         usage_known=True, weekly_percent=100.0)
# worker4 resets first, but its cap cannot hold the prompt: not this call's retry.
POOL_LIMIT["ki4"].update(cooldown_remaining_s=10, capacity_lock_remaining_s=10)


def _cli(tmp_path) -> ClaudeCodeCLI:
    cli = object.__new__(ClaudeCodeCLI)
    cli.timeout = 30
    cli.cwd = tmp_path
    cli.claude_env_vars = {}
    cli.cache_dir = tmp_path
    cli.max_cache_size_mb = 10
    cli.file_discovery = MagicMock()
    return cli


class Lauf:
    """CLI stand-in that takes LAUF_S and records how it ended."""

    def __init__(self):
        self.ereignisse: list[str] = []

    def query(self):
        lauf = self

        async def query(prompt, options):
            lauf.ereignisse.append("start")
            try:
                yield sdk.init()
                await asyncio.sleep(LAUF_S)
                yield sdk.AssistantMessage(content=[sdk.TextBlock(text="OK")], model="m")
                yield sdk.result(usage={"input_tokens": 7000, "output_tokens": 1})
                lauf.ereignisse.append("ende")
            except (asyncio.CancelledError, GeneratorExit):
                lauf.ereignisse.append("abgebrochen")
                raise

        return query


def _freier_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def welt(monkeypatch, tmp_path):
    """One bridge app, reachable as worker1 over a real socket; the metrics
    reader answers with ``welt.pool``; the entry worker refuses calls that
    did not come through a relay (like worker2 at its weekly limit)."""
    monkeypatch.setenv("INSTANCE_NAME", "worker2")
    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("CLAUDE_SKIP_AUTH", "1")
    monkeypatch.setattr(
        claude_cli, "MessageParseError", type("MessageParseError", (Exception,), {})
    )
    lauf = Lauf()
    monkeypatch.setattr(claude_cli, "query", lauf.query())
    monkeypatch.setattr(main.claude_cli, "run_completion", _cli(tmp_path).run_completion)
    if importlib.util.find_spec("src.chat_weiterleitung") is not None:
        import src.chat_weiterleitung as cw

        # Scaled with the scenario: a few seconds instead of minutes.
        monkeypatch.setattr(cw, "CHAT_RELAY_MIN_REST_S", 0.5)
        monkeypatch.setattr(cw, "CHAT_RELAY_MARGIN_S", 0.2)
        monkeypatch.setattr(cw, "CHAT_RELAY_GRACE_S", 0.5)
        monkeypatch.setattr(cw, "AUFSICHT_TAKT_S", 0.05)

    port = _freier_port()

    class W:
        pass

    w = W()
    w.lauf = lauf
    w.port = port
    w.pool = POOL_FREI
    w.relay_kopf: list[dict] = []
    w.relay_timeouts: list = []
    w.zulassung: list = []
    w.refuse = "account_exhausted"

    async def admission(request):
        hop = request.headers.get("x-bridge-retry-count")
        w.zulassung.append(hop)
        request.state.adaptive_est_tokens = EST_TOKENS
        if not hop:
            if w.refuse == "rate_limit":
                raise RateLimitError("Anthropic seven_day limit hit", retry_after_seconds=7_200)
            raise_account_exhausted(7_200)

    real_client = httpx.AsyncClient

    class Weiche(httpx.AsyncBaseTransport):
        def __init__(self):
            self.netz = httpx.AsyncHTTPTransport()

        async def handle_async_request(self, request):
            if request.url.host == "metrics-reader":
                return httpx.Response(200, json={"accounts": w.pool})
            if request.url.host.startswith("worker"):
                w.relay_kopf.append(dict(request.headers, ziel=request.url.host))
            if request.url.host != "worker1":
                raise httpx.ConnectError(f"{request.url.host} is not reachable in this test")
            if request.url.host == "worker1":
                request.url = request.url.copy_with(host="127.0.0.1", port=port)
                request.headers["host"] = f"127.0.0.1:{port}"
                return await self.netz.handle_async_request(request)
            raise AssertionError(f"unexpected call {request.url}")

        async def aclose(self):
            await self.netz.aclose()

    def client(*args, **kwargs):
        w.relay_timeouts.append(kwargs.get("timeout"))
        kwargs["transport"] = Weiche()
        return real_client(*args, **kwargs)

    patches = [
        patch("src.main.validate_claude_code_auth", return_value=(True, {"method": "test"})),
        patch("src.main.verify_api_key", new_callable=AsyncMock),
        patch("src.main.enforce_pool_admission", side_effect=admission),
        patch.object(httpx, "AsyncClient", side_effect=client),
        patch("src.providers.fallback.get_fallback_tiers", side_effect=lambda t, **kw: [t]),
    ]
    for p in patches:
        p.start()
    server = uvicorn.Server(
        uvicorn.Config(main.app, host="127.0.0.1", port=port, lifespan="off", log_level="warning")
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    try:
        yield w
    finally:
        server.should_exit = True
        t.join(timeout=10)
        for p in reversed(patches):
            p.stop()
        if main.limiter is not None:
            main.limiter.reset()


def _post(w, headers=None, timeout=10.0):
    return requests.post(
        f"http://127.0.0.1:{w.port}/v1/chat/completions",
        json=BODY,
        headers=headers or {},
        timeout=timeout,
    )


def _abwarten(w, s=LAUF_S + 1.5):
    time.sleep(s)
    return list(w.lauf.ereignisse)


# --- Scenario 17:31Z: the caller gives up, the relayed run must end too -----


def test_szenario_aufrufer_gibt_auf_keine_waise(welt):
    """Energy → worker2 (limit) → relay → worker1 runs; the caller gives up
    before worker1 is done. On efa0fdc worker1 ran to the end (orphan)."""
    welt.pool = POOL_FREI
    with pytest.raises(requests.exceptions.ReadTimeout):
        _post(welt, timeout=AUFGABE_S)
    ereignisse = _abwarten(welt)
    assert welt.zulassung[:2] == [None, "1"], welt.zulassung  # really relayed
    assert ereignisse == ["start", "abgebrochen"], ereignisse


def test_relay_traegt_frist_und_wartet_bis_dahin(welt):
    """The relay carries the caller's deadline and waits for it — not a fixed
    30 s that is shorter than the run it starts."""
    welt.pool = POOL_FREI
    resp = _post(welt, timeout=LAUF_S + 10)
    assert resp.status_code == 200, resp.text[:300]
    assert welt.relay_kopf, "no relay happened"
    bis = float(welt.relay_kopf[0][FRIST_HEADER])
    assert bis > time.time() + 3000  # first worker: arrival + 3300 s
    relay_timeout = [t for t in welt.relay_timeouts if t not in (3.0,)][0]
    assert relay_timeout > LAUF_S and relay_timeout > 30.0, welt.relay_timeouts


def test_peer_bricht_an_der_frist_ab(welt):
    """A relayed call whose deadline passes mid-run is cancelled at the
    deadline, answered 504 — on efa0fdc it ran on (200 after LAUF_S)."""
    t0 = time.time()
    resp = _post(
        welt,
        headers={"x-bridge-retry-count": "1", FRIST_HEADER: repr(time.time() + 1.5)},
        timeout=LAUF_S + 5,
    )
    dauer = time.time() - t0
    assert resp.status_code == 504, resp.text[:300]
    assert resp.json()["error"]["reason"] == "relay_deadline_expired"
    assert dauer < LAUF_S, dauer
    assert _abwarten(welt, 0.5) == ["start", "abgebrochen"]


def test_peer_nimmt_bei_zu_kurzer_frist_nicht_an(welt):
    """Too little of the caller's deadline left: refused before any CLI run."""
    resp = _post(
        welt,
        headers={"x-bridge-retry-count": "1", FRIST_HEADER: repr(time.time() + 0.3)},
        timeout=LAUF_S + 5,
    )
    assert resp.status_code == 503, resp.text[:300]
    assert resp.json()["error"]["reason"] == "relay_deadline_too_short"
    assert _abwarten(welt, 0.3) == []


# --- The relay target must carry the prompt ---------------------------------


@pytest.mark.parametrize("refuse", ["account_exhausted", "rate_limit"])
def test_kein_relay_auf_belegtes_budget_und_ehrliche_429(welt, refuse):
    """worker1's budget is held (327k of 400k in flight): a 164k prompt must
    not be sent there (efa0fdc: 500-token floor → relayed). The 429 names the
    pool's real reason and a retry_after that fits it — not 'All Anthropic
    accounts at weekly limit' with the weekly reset."""
    welt.pool = POOL_BELEGT
    welt.refuse = refuse
    resp = _post(welt, timeout=LAUF_S + 5)
    assert welt.relay_kopf == [], "relayed to a worker whose budget cannot carry it"
    assert welt.lauf.ereignisse == []
    assert resp.status_code == 429, resp.text[:300]
    err = resp.json()["error"]
    assert "All Anthropic accounts" not in err["message"]
    assert err["reason"] == "pool_capacity_busy"
    assert err["retry_after_s"] == 60
    assert resp.headers["retry-after"] == "60"
    zustand = {w["worker"]: w["zustand"] for w in err["pool"]["worker"]}
    assert zustand == {
        "worker1": "kapazitaet_belegt",
        "worker2": "konto_limit",
        "worker3": "konto_limit",
        "worker4": "zu_gross",
    }
    assert err["pool"]["benoetigt_tokens"] == EST_TOKENS


def test_alle_konten_am_limit_retry_after_vom_fruehesten_reset(welt):
    welt.pool = POOL_LIMIT
    welt.refuse = "rate_limit"
    resp = _post(welt, timeout=LAUF_S + 5)
    assert resp.status_code == 429
    err = resp.json()["error"]
    assert err["reason"] == "pool_accounts_limited"
    assert err["retry_after_s"] == 1_800  # worker1's account frees first, worker4 is too small
    assert resp.headers["retry-after"] == "1800"


def test_fristenkette_passt_zu_nginx():
    """The relaying worker waits (deadline + grace) less than nginx waits for
    the chat location; the nginx number is the one tests/research_cloud checks
    against docker/nginx.conf."""
    import src.chat_weiterleitung as cw
    from src.research_laufzeit import NGINX_RESEARCH_READ_TIMEOUT_S

    assert cw.NGINX_CHAT_READ_TIMEOUT_S == NGINX_RESEARCH_READ_TIMEOUT_S
    assert cw.CHAT_GESAMTFRIST_S + cw.CHAT_RELAY_GRACE_S < cw.NGINX_CHAT_READ_TIMEOUT_S
    assert cw.CHAT_RELAY_MARGIN_S < cw.CHAT_RELAY_MIN_REST_S
    cw.pruefe_staffelung()


@pytest.mark.parametrize("wert", ["inf", "-inf", "nan", "morgen"])
def test_ungueltige_frist_wird_abgelehnt(welt, wert):
    """CHATFWDR P1: a non-finite or unreadable deadline is refused (400),
    never turned into a deadline — and no run starts."""
    resp = _post(welt, headers={"x-bridge-retry-count": "1", FRIST_HEADER: wert})
    assert resp.status_code == 400, resp.text[:300]
    assert resp.json()["error"]["reason"] == "relay_deadline_invalid"
    assert welt.lauf.ereignisse == []


def test_frist_verlaengert_nie_ueber_die_eigene(welt, monkeypatch):
    """CHATFWDR P1: a forwarded deadline a day ahead is capped at this
    worker's own (here scaled to 1.5 s): the 3 s run ends at it."""
    import src.chat_weiterleitung as cw

    monkeypatch.setattr(cw, "CHAT_GESAMTFRIST_S", 1.5)
    resp = _post(
        welt,
        headers={"x-bridge-retry-count": "1", FRIST_HEADER: repr(time.time() + 86_400)},
        timeout=LAUF_S + 5,
    )
    assert resp.status_code == 504, resp.text[:300]
    assert _abwarten(welt, 0.5) == ["start", "abgebrochen"]


def test_voll_belegtes_budget_ist_kein_kontolimit():
    """CHATFWDR P2: the real pool state says available=false for a full
    in-flight budget (headroom 0) — that is capacity, 60 s, not an account
    limit with the peer's 7200 s."""
    import src.chat_weiterleitung as cw

    pool = {k: dict(v) for k, v in POOL_BELEGT.items()}
    pool["ki1"].update(available=False, current_in_flight_tokens=400_000,
                       usage_known=True, weekly_percent=40.0, session_percent=20.0)
    lage = cw.pool_lage(pool, benoetigt_tokens=EST_TOKENS, ausgeschlossen={"worker2"})
    assert lage["grund"] == cw.REASON_POOL_KAPAZITAET_BELEGT
    assert lage["retry_after_s"] == 60
    zustand = {w["worker"]: w["zustand"] for w in lage["worker"]}
    assert zustand["worker1"] == "kapazitaet_belegt"
    assert zustand["worker2"] == "konto_limit"


@pytest.mark.parametrize(
    "mult, effektiv, erwartet",
    [
        (1.0, 850_000, "zu_gross"),  # only the safety margin: no reset helps
        (0.5, 425_000, "konto_limit"),  # weekly squeeze: unsqueezed 850k carries 800k
        (0.0, 0, "konto_limit"),  # weekly ≥ 97 %: account limit whatever the size
    ],
)
def test_sicherheitsreserve_ist_kein_kontolimit(mult, effektiv, erwartet):
    """CHATFWDR R2: effective = cap × margin × multiplier. Only the multiplier
    is an account squeeze; the margin (adaptive 1M → 850k) is not."""
    import src.chat_weiterleitung as cw

    info = {"worker": "worker1", "available": True, "usage_known": True,
            "session_percent": 0.0, "weekly_percent": 0.0, "budget_multiplier": mult,
            "adaptive_cap_tokens": 1_000_000, "effective_cap_tokens": effektiv,
            "current_in_flight_tokens": 0}
    benoetigt = 900_000 if erwartet == "zu_gross" else 800_000
    lage = cw.pool_lage({"a": info}, benoetigt_tokens=benoetigt, ausgeschlossen=set())
    assert lage["worker"][0]["zustand"] == erwartet
    if erwartet == "zu_gross":
        assert lage["grund"] == cw.REASON_POOL_ZU_GROSS
        assert lage["retry_after_s"] is None
