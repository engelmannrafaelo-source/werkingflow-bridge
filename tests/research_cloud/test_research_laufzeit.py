"""BRREF-LAUFZEIT (12.10.2026): one overall deadline per /v1/research run.

Befund koordination/BRREF-LAUFZEIT-befund.md: Energy run b took 2392 s — main
run 282 s, gap check 149 s, return round 1722 s, re-check 238 s — while the
job's self-call, the cross-worker retry and MAX_TIMEOUT were all 2400 s, and
execution_time_seconds said 282 s. Here: the time chain is ordered and has one
source; every CLI call of a run gets the remaining time; optional rounds are
skipped (named) instead of overrunning; a mandatory round that hits the
deadline is laufzeit_erschoepft; a failed return round is visible; the run
time covers the whole call.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# First: test_luecken_pool sets its sys.modules stand-ins before src.main is
# imported (with src.main first, the pool's MCP server is the wrong class).
from tests.research_cloud.test_luecken_pool import ALT, FakeCli, _make_req, _run  # isort: skip
from tests.research_cloud.test_luecken_pool import persist, pplx_on  # noqa: F401  isort: skip

import src.main  # noqa: E402
import src.research_laufzeit as rl  # noqa: E402

# ── the time chain: inner < outer, one source ───────────────────────────────

def test_staffelung_der_fristen():
    w = rl.staffelung()
    assert w["max_timeout_s"] <= w["gesamtfrist_s"] == 3300
    assert w["gesamtfrist_s"] < w["cross_worker_s"] == 3400
    assert w["cross_worker_s"] < w["nginx_research_s"] == 3450
    assert w["nginx_research_s"] < w["job_selbstaufruf_s"] == 3500
    assert w["job_selbstaufruf_s"] < w["energy_poller_s"] == 3600
    rl.pruefe_staffelung()


@pytest.mark.parametrize("kaputt", [
    {"job_selbstaufruf_s": 3300},          # the 2400/2400 situation of 12.10.
    {"cross_worker_s": 3600},
    {"nginx_research_s": 2500},            # the 2500 s of BRLZR P1
    {"nginx_research_s": 3500},
    {"max_timeout_s": 4000},
])
def test_falsche_staffelung_faellt_laut(kaputt):
    with pytest.raises(ValueError, match="time chain out of order"):
        rl.pruefe_staffelung(dict(rl.staffelung(), **kaputt))


def _nginx_read_timeouts() -> dict:
    """proxy_read_timeout per location block of docker/nginx.conf."""
    import re
    from pathlib import Path

    conf = (Path(__file__).resolve().parents[2] / "docker" / "nginx.conf").read_text()
    out, location = {}, None
    for line in conf.splitlines():
        m = re.match(r"\s*location\s+(.+?)\s*\{", line)
        if m:
            location = m.group(1)
        m = re.match(r"\s*proxy_read_timeout\s+(\d+)s;", line)
        if m and location:
            out.setdefault(location, []).append(float(m.group(1)))
    return out


def test_nginx_research_location_steht_in_der_staffel():
    """BRLZR P1: a job re-dispatched through the LB after a capacity 429 runs
    behind these two locations — their read timeout is a step of the chain."""
    t = _nginx_read_timeouts()
    for location in ("~ ^/v1/(chat/completions|research)", "@production_streaming"):
        assert t[location] == [rl.NGINX_RESEARCH_READ_TIMEOUT_S], location


def test_redispatch_geht_durch_die_research_location():
    """The re-dispatch path is the one the nginx step guards."""
    from src.jobs import executors
    assert "/v1/research" in executors.JOB_REDISPATCH_PATHS


def test_job_selbstaufruf_liest_die_eine_quelle():
    from src.jobs import executors
    assert executors.RESEARCH_SELF_CALL_TIMEOUT_S == rl.JOB_SELBSTAUFRUF_TIMEOUT_S


@pytest.mark.asyncio
async def test_cross_worker_retry_wartet_laenger_und_traegt_die_frist_weiter():
    import httpx

    gesehen = {}

    class Client:
        def __init__(self, timeout):
            gesehen["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers):
            gesehen["headers"] = headers
            r = MagicMock(status_code=200, headers={})
            r.json.return_value = {"status": "success"}
            return r

    bis = time.time() + 1234.0
    request = MagicMock()
    request.headers = {}
    request.url.path = "/v1/research"
    request.state = SimpleNamespace(cached_body_dict={"query": "q"}, research_frist_bis=bis)
    with (
        patch("src.main._pick_alternative_worker", new=AsyncMock(return_value="worker2")),
        patch.object(httpx, "AsyncClient", Client),
        patch("src.main._relay_headers", return_value=[]),
    ):
        await src.main._cross_worker_retry(request, "worker1")
    assert gesehen["timeout"] == rl.CROSS_WORKER_TIMEOUT_S
    assert float(gesehen["headers"][rl.FRIST_HEADER]) == bis


def test_weitergetragene_frist_gilt_auf_dem_peer():
    request = MagicMock()
    request.headers = {rl.FRIST_HEADER: repr(time.time() + 100.0)}
    request.state = SimpleNamespace()
    lz = rl.Laufzeit.fuer_anfrage(request)
    assert 95 <= lz.frist_s <= 100
    # never later than a fresh deadline
    request.headers = {rl.FRIST_HEADER: repr(time.time() + 99999.0)}
    request.state = SimpleNamespace()
    assert rl.Laufzeit.fuer_anfrage(request).frist_s <= rl.GESAMTFRIST_S


# ── the agent is stopped at its deadline ────────────────────────────────────

@pytest.mark.asyncio
async def test_cli_frist_beendet_den_agenten(tmp_path, monkeypatch):
    """run_completion with timeout_s: the SDK query is cancelled at the
    deadline and its finally runs (the real SDK closes the transport and
    terminates the CLI process there) — nothing runs on and costs."""
    import src.claude_cli as claude_cli
    from tests import sdk_strom as sdk
    from tests.unit.test_limit_an_der_form import _make_cli

    beendet = {"finally": False}

    async def query(prompt, options):
        try:
            yield sdk.init()
            await asyncio.sleep(30)
            yield sdk.result("success")
        finally:
            beendet["finally"] = True

    monkeypatch.setattr(claude_cli, "query", query)
    cli = _make_cli(tmp_path)
    got = []
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        async for c in cli.run_completion(prompt="p", model="m", timeout_s=0.3):
            got.append(c)
    assert time.monotonic() - t0 < 5
    assert beendet["finally"] is True
    assert any(isinstance(c, dict) and c.get("subtype") == "timeout_incomplete" for c in got)


# ── research: mandatory rounds, optional rounds, run time ───────────────────

class _ZeitCli(FakeCli):
    """FakeCli that records timeout_s per role and can time out a role."""

    def __init__(self, workdir, *, timeout_in=None, **kw):
        super().__init__(workdir, **kw)
        self.timeouts = {}
        self.timeout_in = timeout_in

    def _rolle(self, kw):
        from src.research_cloud.luecken import PRUEF_MODELL
        if kw.get("resume_workdir") is not None:
            return "nachhol" if "does not exist" in kw["prompt"] else "rueckrunde"
        return "pruefer" if kw.get("model") == PRUEF_MODELL else "research"

    def __call__(self, **kw):
        rolle = self._rolle(kw)
        self.timeouts.setdefault(rolle, []).append(kw.get("timeout_s"))
        if rolle == self.timeout_in:
            self.calls.setdefault(rolle, []).append(kw)

            async def abgelaufen():
                yield {"content": [{"type": "text", "text": "teil"}]}
                yield {"type": "result", "subtype": "timeout_incomplete", "is_error": True}
                raise TimeoutError()
            return abgelaufen()
        return super().__call__(**kw)


@pytest.mark.asyncio
@pytest.mark.usefixtures("persist", "pplx_on")
async def test_jeder_cli_aufruf_bekommt_die_restzeit(tmp_path):
    fake = _ZeitCli(tmp_path, mit_datei=True)
    result = await _run(fake)
    assert result.status == "success"
    for rolle in ("research", "pruefer", "rueckrunde"):
        assert fake.timeouts[rolle] and all(0 < t <= rl.GESAMTFRIST_S for t in fake.timeouts[rolle]), rolle
    # The return round leaves time for the re-check after it.
    assert fake.timeouts["rueckrunde"][0] <= rl.GESAMTFRIST_S - rl.PRUEFER_MIN_REST_S
    # Run time of the whole call, split per round.
    lz = result.laufzeit
    assert set(lz["runden"]) >= {"hauptlauf", "pruefer", "rueckrunde", "nachmessung"}
    assert lz["frist_s"] <= rl.GESAMTFRIST_S
    assert result.execution_time_seconds >= sum(lz["runden"].values()) - 0.05
    assert result.nachkontrolle["status"] == "durchgefuehrt"
    assert result.nachkontrolle["rueckrunde"] is True
    assert result.nachkontrolle["nachmessung"] == "durchgefuehrt"


@pytest.mark.asyncio
@pytest.mark.usefixtures("persist", "pplx_on")
async def test_hauptlauf_an_der_frist_ist_laufzeit_erschoepft(tmp_path):
    fake = _ZeitCli(tmp_path, mit_datei=True, timeout_in="research")
    result = await _run(fake)
    assert result.status == "error" and result.content is None
    assert result.fehler_code == "laufzeit_erschoepft"
    assert result.retryable is False
    assert "hauptlauf" in result.laufzeit["runden"]
    assert fake.calls["pruefer"] == [] and fake.calls["rueckrunde"] == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("persist", "pplx_on")
async def test_reparaturrunde_an_der_frist_ist_laufzeit_erschoepft(tmp_path):
    fake = _ZeitCli(tmp_path, timeout_in="nachhol")   # inline answer -> repair round
    result = await _run(fake)
    assert result.status == "error"
    assert result.fehler_code == "laufzeit_erschoepft" and result.retryable is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("persist", "pplx_on")
async def test_rueckrunde_an_der_frist_ist_sichtbar_gescheitert(tmp_path):
    """Never a silent success: the complete report from before goes out, and
    nachkontrolle says the return round failed and why."""
    fake = _ZeitCli(tmp_path, mit_datei=True, timeout_in="rueckrunde")
    result = await _run(fake)
    assert result.status == "success" and result.content == ALT
    nk = result.nachkontrolle
    assert nk["status"] == "gescheitert" and nk["rueckrunde"] is False
    assert nk["grund"].startswith("laufzeit")
    assert fake.datei.read_text(encoding="utf-8") == ALT


@pytest.mark.asyncio
@pytest.mark.usefixtures("persist", "pplx_on")
async def test_rueckrunde_wird_uebersprungen_wenn_die_zeit_nicht_reicht(tmp_path, monkeypatch):
    monkeypatch.setattr(rl, "GESAMTFRIST_S", rl.PRUEFER_MIN_REST_S + rl.RUECKRUNDE_MIN_REST_S - 60)
    fake = _ZeitCli(tmp_path, mit_datei=True)
    result = await _run(fake)
    assert result.status == "success" and result.content == ALT
    assert fake.calls["rueckrunde"] == []
    assert len(fake.calls["pruefer"]) == 1
    nk = result.nachkontrolle
    assert nk["status"] == "uebersprungen" and nk["grund"].startswith("rueckrunde: laufzeit")


@pytest.mark.asyncio
@pytest.mark.usefixtures("persist", "pplx_on")
async def test_pruefung_wird_uebersprungen_wenn_die_zeit_nicht_reicht(tmp_path, monkeypatch):
    monkeypatch.setattr(rl, "GESAMTFRIST_S", rl.PRUEFER_MIN_REST_S - 60)
    fake = _ZeitCli(tmp_path, mit_datei=True)
    result = await _run(fake)
    assert result.status == "success"
    assert fake.calls["pruefer"] == [] and fake.calls["rueckrunde"] == []
    assert result.nachkontrolle["status"] == "uebersprungen"
    assert result.nachkontrolle["grund"].startswith("pruefung: laufzeit")


@pytest.mark.asyncio
@pytest.mark.usefixtures("persist", "pplx_on")
async def test_frist_schon_vorbei_startet_keinen_lauf(tmp_path):
    request = MagicMock()
    request.headers = {rl.FRIST_HEADER: repr(time.time() - 5)}
    request.state = SimpleNamespace()
    fake = _ZeitCli(tmp_path, mit_datei=True)
    with patch.object(src.main.claude_cli, "run_completion", side_effect=fake):
        result = await src.main._execute_research_impl(_make_req(), None, request=request)
    assert result.status == "error" and result.fehler_code == "laufzeit_erschoepft"
    assert fake.calls["research"] == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("persist", "pplx_on")
async def test_job_uebernimmt_laufzeit_erschoepft(tmp_path):
    from src.error_contract import JobFailure
    from src.jobs import executors

    result = await _run(_ZeitCli(tmp_path, mit_datei=True, timeout_in="research"))
    with pytest.raises(JobFailure) as ei:
        executors._raise_on_status_error("research", result.model_dump())
    assert ei.value.retryable is False
    assert ei.value.details.get("fehler_code") == "laufzeit_erschoepft"


def test_nachkontrolle_status_regeln():
    from src.research_cloud.luecken import Nachkontrolle

    nk = Nachkontrolle(pruefer=None)
    nk.rueckrunde_grund = "keine_offenen_luecken"
    assert nk.status()["status"] == "durchgefuehrt"
    nk = Nachkontrolle(pruefer=None)
    nk.rueckrunde, nk.rueckrunde_fehler = True, "laufzeit: …"
    assert nk.status()["status"] == "gescheitert" and nk.status()["rueckrunde"] is False
    nk = Nachkontrolle(pruefer=None)
    nk.rueckrunde_grund = "budget_erschoepft"
    assert nk.status()["status"] == "uebersprungen"


# ── BRLZR P2: a failed re-check is not a performed one ─────────────────────

class _NachmessungTimeout(FakeCli):
    """Codex BRLZR counter-sample: the re-check after the return round times out."""

    def __call__(self, **kw):
        from src.research_cloud.luecken import PRUEF_MODELL
        if kw.get("model") == PRUEF_MODELL and self.calls["pruefer"]:
            async def fail():
                raise TimeoutError("review: deadline in recheck")
                yield
            return fail()
        return super().__call__(**kw)


@pytest.mark.asyncio
@pytest.mark.usefixtures("persist", "pplx_on")
async def test_nachmessung_timeout_ist_oeffentlich_gescheitert(tmp_path):
    result = await _run(_NachmessungTimeout(tmp_path, mit_datei=True))
    nk = result.nachkontrolle
    assert result.status == "success"  # the revised report still goes out
    assert nk["status"] == "gescheitert"
    assert nk["nachmessung"] == "gescheitert"
    assert nk["grund"].startswith("nachmessung: nachher: TimeoutError")
    assert nk["rueckrunde"] is True


def test_nachmessung_fehler_im_status():
    from src.research_cloud.luecken import Nachkontrolle

    nk = Nachkontrolle(pruefer=None)
    nk.rueckrunde, nk.rueckrunde_grund = True, "luecken_ohne_suche"
    nk.nachmessung_fehler = "nachher: TimeoutError: x"
    st = nk.status()
    assert (st["status"], st["nachmessung"]) == ("gescheitert", "gescheitert")
    assert nk.as_meta()["nachmessung_fehler"] == "nachher: TimeoutError: x"
