"""CLI place as agentic-run adapter (ACLI): declared limits end loudly.

Streams in tests/fixtures/cli_2_1_285_lauf/ are what the real CLI 2.1.285
wrote against a local stand-in for the Messages API (no AI call), with
--include-partial-messages: werkzeug (Read, Write, answer), grenze (every
answer 400 input tokens, endless Read), ausgabe (output above max_tokens),
ueberlast (HTTP 529).
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from src.agentischer_lauf import http
from src.agentischer_lauf.adapter import Adapters, RunError
from src.agentischer_lauf.cli import CliLauf, StromZaehler
from src.agentischer_lauf.cli_platz import CliAusgang, CliPlatz
from src.agentischer_lauf.runtime import Runtime
from src.agentischer_lauf.storage import Storage
from src.agentischer_lauf.zulassung import verdrahte
from src.models import ResearchRequest
from src.principals import LEGACY_PRINCIPAL, Principal

from .test_contract import material, payload
from .test_transport import app

STROEME = Path(__file__).parent.parent / "fixtures" / "cli_2_1_285_lauf"


def strom(name):
    return [json.loads(z) for z in (STROEME / f"{name}.jsonl").read_text().splitlines()]


class Platz:
    """Replays a real stream through the observer, like CliPlatz does."""

    def __init__(self, zeilen, dateien=None, root=Path("/arbeit")):
        self.zeilen = zeilen
        self.dateien = dateien or {}
        self.root = root
        self.auftraege = []
        self.gelesen = 0

    async def lauf(self, auftrag, beobachter):
        self.auftraege.append(auftrag)
        for line in self.zeilen:
            self.gelesen += 1
            beobachter(line)
        return CliAusgang(self.zeilen, 0, dict(self.dateien))


def cli_runtime(tmp_path, platz, eingang=7, zugang="geheimer-zugang"):
    adapters = Adapters()
    run = CliLauf(
        platz,
        cli_modell="claude-sonnet-4-5",
        zugang=lambda: zugang,
        zaehler=AsyncMock(return_value=eingang),
    )
    adapters.register_cli("cli", run)
    rt = Runtime(adapters, Storage(tmp_path / "protected"), revision="test", max_bytes=100000)
    return rt, run


def cli_payload(**overrides):
    body = payload(model="cli", max_tokens=50)
    body["agentischer_lauf"]["werkzeuge"] = [
        {"name": "Read", "parameter": {}},
        {"name": "Write", "parameter": {}},
    ]
    body["agentischer_lauf"]["material"] = [material("notiz.txt", b"Notiz fuer den Lauf\n")]
    body["agentischer_lauf"]["ausgabe"] = {"modus": "text", "artefakte": ["ergebnis.txt"]}
    body["agentischer_lauf"]["grenzen"] = {"zeit_s": 60, "tokens_gesamt": 1000}
    body.update(overrides)
    return body


async def test_tool_run_succeeds_with_declared_limits(tmp_path):
    platz = Platz(strom("werkzeug"), {"ergebnis.txt": b"Ergebnis aus der Notiz\n"})
    rt, _ = cli_runtime(tmp_path, platz)
    result = await rt.execute(ResearchRequest(**cli_payload()), "tenant-a")
    assert result["status"] == "success" and result["content"] == "fertig"
    assert result["antwort_ende"] == "end_turn"
    assert result["grenze"]["hard_token_bound"] is False
    assert result["grenze"]["durchsetzung"] == "cli_laufend_gemessen"
    assert result["grenze"]["tokens_verbraucht"] == 90
    assert result["grenze"]["modellaufrufe"] == 3
    assert result["grenze"]["eingang_gezaehlt"] == 7
    assert result["usage"]["input_tokens"] == 30 and result["usage"]["output_tokens"] == 60
    calls = result["werkzeugaufrufe"]
    assert calls["vollstaendig"] and [e["name"] for e in calls["eintraege"]] == ["Read", "Write"]
    assert calls["eintraege"][0]["material_ids"] == ["notiz.txt"]
    assert calls["eintraege"][1]["artefakt_ids"]
    assert [a["pfad"] for a in result["artefakte"]] == ["ergebnis.txt"]
    auftrag = platz.auftraege[0]
    assert auftrag.max_tokens == 50 and auftrag.max_turns == 4
    assert auftrag.werkzeuge == ["Read", "Glob", "Grep", "Write", "Edit"]
    assert auftrag.prompt.startswith("Sortiere Titel eines Filmarchivs.\n\n")
    assert auftrag.material == {"notiz.txt": b"Notiz fuer den Lauf\n"}


async def test_running_usage_over_budget_stops_loudly(tmp_path):
    platz = Platz(strom("grenze"))
    rt, _ = cli_runtime(tmp_path, platz)
    body = cli_payload()
    body["agentischer_lauf"]["grenzen"]["tokens_gesamt"] = 700
    result = await rt.execute(ResearchRequest(**body), "tenant-a")
    assert result["status"] == "error" and result["fehler_code"] == "grenze_tokens"
    assert result["grenze"]["ursache"] == "laufende_nutzung_ueber_budget"
    assert result["grenze"]["tokens_verbraucht"] == 821
    assert result["retryable"] is False and result["content"] is None
    # Stopped at the second answer's message_start, not at the run's end.
    assert platz.gelesen < len(platz.zeilen) / 2
    assert result["usage"]["input_tokens"] == 800


async def test_output_above_answer_limit_stops_loudly(tmp_path):
    rt, _ = cli_runtime(tmp_path, Platz(strom("ausgabe")))
    result = await rt.execute(ResearchRequest(**cli_payload()), "tenant-a")
    assert result["fehler_code"] == "grenze_tokens"
    assert result["grenze"]["ursache"] == "ausgabe_ueber_grenze"


async def test_max_turns_is_unfinished(tmp_path):
    rt, _ = cli_runtime(tmp_path, Platz(strom("grenze")))
    body = cli_payload()
    body["agentischer_lauf"]["grenzen"]["tokens_gesamt"] = 100000
    result = await rt.execute(ResearchRequest(**body), "tenant-a")
    assert result["fehler_code"] == "lauf_unvollstaendig"


async def test_overload_is_repeatable_capacity(tmp_path):
    rt, _ = cli_runtime(tmp_path, Platz(strom("ueberlast")))
    result = await rt.execute(ResearchRequest(**cli_payload()), "tenant-a")
    assert result["fehler_code"] == "kapazitaet_belegt"
    assert result["retryable"] is True and result["retry_after_s"] == 60


async def test_counted_input_over_budget_never_starts(tmp_path):
    platz = Platz(strom("werkzeug"))
    rt, _ = cli_runtime(tmp_path, platz, eingang=990)
    result = await rt.execute(ResearchRequest(**cli_payload()), "tenant-a")
    assert result["fehler_code"] == "grenze_tokens"
    assert result["grenze"]["ursache"] == "eingang_ueber_budget"
    assert platz.auftraege == []


async def test_uncounted_input_is_unavailable(tmp_path):
    platz = Platz(strom("werkzeug"))
    rt, _ = cli_runtime(tmp_path, platz, eingang=None)
    result = await rt.execute(ResearchRequest(**cli_payload()), "tenant-a")
    assert result["fehler_code"] == "faehigkeit_nicht_verfuegbar"
    assert platz.auftraege == []


async def test_result_total_above_stream_counts(tmp_path):
    zeilen = strom("werkzeug")
    zeilen[-1] = {**zeilen[-1], "usage": {**zeilen[-1]["usage"], "input_tokens": 990}}
    rt, _ = cli_runtime(tmp_path, Platz(zeilen, {"ergebnis.txt": b"x"}))
    result = await rt.execute(ResearchRequest(**cli_payload()), "tenant-a")
    assert result["grenze"]["ursache"] == "nutzung_nachtraeglich_ueber_budget"


async def test_time_limit_from_place(tmp_path):
    class Langsam(Platz):
        async def lauf(self, auftrag, beobachter):
            self.auftraege.append(auftrag)
            raise RunError("grenze_zeit")

    platz = Langsam([])
    rt, _ = cli_runtime(tmp_path, platz)
    result = await rt.execute(ResearchRequest(**cli_payload()), "tenant-a")
    assert result["fehler_code"] == "grenze_zeit"
    assert 0 < platz.auftraege[0].zeit_s <= 60


async def test_credential_in_answer_is_refused(tmp_path):
    zeilen = strom("werkzeug")
    zeilen[-1] = {**zeilen[-1], "result": "fertig geheimer-zugang"}
    rt, _ = cli_runtime(tmp_path, Platz(zeilen, {"ergebnis.txt": b"x"}))
    result = await rt.execute(ResearchRequest(**cli_payload()), "tenant-a")
    assert result["fehler_code"] == "sandbox_fehler" and result["content"] is None


async def test_review_on_cli_shares_budget_and_reports(tmp_path):
    platz = Platz(strom("werkzeug"), {"ergebnis.txt": b"Ergebnis aus der Notiz\n"})
    rt, _ = cli_runtime(tmp_path, platz)
    body = cli_payload()
    body["agentischer_lauf"]["pruefung"] = {
        "auftrag": "Pruefe das Ergebnis.",
        "model": "cli",
        "werkzeuge": [{"name": "Read", "parameter": {}}, {"name": "Write", "parameter": {}}],
        "ausgabe": {"modus": "text", "artefakte": []},
        "max_turns": 4,
        "max_tokens": 50,
    }
    result = await rt.execute(ResearchRequest(**body), "tenant-a")
    assert result["status"] == "success"
    assert result["grenze"]["tokens_verbraucht"] == 180
    assert result["pruefung"]["grenze"]["tokens_verbraucht"] == 90
    assert "ergebnis.txt" in platz.auftraege[1].material


def test_cli_registered_only_with_declared_limits():
    run = CliLauf(Platz([]), cli_modell="m", zugang=lambda: "x", zaehler=AsyncMock())
    with pytest.raises(RunError):
        Adapters().register("cli", run)
    with pytest.raises(RunError):
        Adapters().register_cli("cli", object())
    run.erklaerte_grenzen = ("max_turns",)
    with pytest.raises(RunError):
        Adapters().register_cli("cli", run)


def test_stream_without_usage_is_unknown():
    zaehler = StromZaehler(100, 10)
    with pytest.raises(RunError) as caught:
        zaehler.zeile({"type": "stream_event", "event": {"type": "message_start", "message": {}}})
    assert caught.value.ursache == "nutzung_unbekannt"


# --- Gate 2: composition and admission ---------------------------------------


def konfig(tmp_path, **overrides):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    (tmp_path / "platz").mkdir(exist_ok=True)
    result = {
        "platz_wurzel": str(tmp_path / "platz"),
        "befehl": ["claude"],
        "settings": str(settings),
        "dedicated_uid": True,
        "max_bytes": 100000,
        "zugang_variable": "ACLI_TEST_ZUGANG",
        "zaehler_url": "http://127.0.0.1:9",
        "oauth": True,
        "modelle": {"cli": "claude-sonnet-4-5"},
        "werkzeuge": ["Read", "Write"],
        "max_zeit_s": 600,
        "max_tokens_gesamt": 5000,
        "max_turns": 10,
        "max_tokens_je_antwort": 100,
        "speicher": str(tmp_path / "protected"),
        "revision": "test",
    }
    result.update(overrides)
    return result


@pytest.fixture
def server(monkeypatch, tmp_path):
    principal = {"value": Principal(id=1, name="energie-dienst", allowed_apps=["app-x"],
                                    allowed_paths=["*"], monthly_cap_eur=None)}

    async def authorized(request, credentials):
        if principal["value"] is not None:
            request.state.principal = principal["value"]

    monkeypatch.setattr(http, "authorized", authorized)
    # Wiring tests run outside a place container; pruefe_platz has its own tests.
    from src.agentischer_lauf import zulassung
    monkeypatch.setattr(zulassung, "pruefe_platz", lambda *a, **k: None)
    monkeypatch.setattr(http, "_runtime", None)
    monkeypatch.setattr(http, "_admit", None)
    monkeypatch.setenv("ACLI_TEST_ZUGANG", "geheimer-zugang")
    return principal


def test_gate2_admission_lifts_capability_refusal(server, tmp_path):
    headers = {"X-App-ID": "app-x"}
    with TestClient(app()) as client:
        before = client.post("/v1/research", json=cli_payload(), headers=headers)
        assert before.status_code == 422
        assert before.json()["detail"]["code"] == "faehigkeit_nicht_verfuegbar"
        runtime, _ = verdrahte(konfig(tmp_path))
        platz = Platz(strom("werkzeug"), {"ergebnis.txt": b"Ergebnis\n"})
        provider = runtime.adapters.require("cli")
        provider.platz = platz
        provider.zaehler = AsyncMock(return_value=7)
        after = client.post("/v1/research", json=cli_payload(), headers=headers)
        assert after.status_code == 200, after.text
        assert after.json()["status"] == "success"
        assert after.json()["grenze"]["hard_token_bound"] is False


@pytest.mark.parametrize(
    "change",
    [
        lambda b: b["agentischer_lauf"].update(werkzeuge=[{"name": "List", "parameter": {}}]),
        lambda b: b["agentischer_lauf"]["grenzen"].update(tokens_gesamt=5001),
        lambda b: b["agentischer_lauf"]["grenzen"].update(zeit_s=601),
        lambda b: b.update(max_turns=11),
        lambda b: b.update(max_tokens=101),
        lambda b: b.update(model="synthetic"),
    ],
)
def test_gate2_refuses_unpermitted_tools_and_limits(server, tmp_path, change):
    verdrahte(konfig(tmp_path))
    body = cli_payload()
    change(body)
    with TestClient(app()) as client:
        out = client.post("/v1/research", json=body, headers={"X-App-ID": "app-x"})
    assert out.status_code == 422
    assert out.json()["detail"]["code"] == "faehigkeit_nicht_verfuegbar"


@pytest.mark.parametrize("who,app_id", [(None, "app-x"), ("legacy", "app-x"), ("own", "app-y")])
def test_gate2_requires_verified_tenant(server, tmp_path, who, app_id):
    verdrahte(konfig(tmp_path))
    if who is None:
        server["value"] = None
    elif who == "legacy":
        server["value"] = LEGACY_PRINCIPAL
    with TestClient(app()) as client:
        out = client.post("/v1/research", json=cli_payload(), headers={"X-App-ID": app_id})
    assert out.status_code == 403


def test_gate2_missing_isolation_fails_at_start(tmp_path):
    with pytest.raises(RunError):
        verdrahte(konfig(tmp_path, settings=str(tmp_path / "fehlt.json")))


# --- The place runner itself, with a stand-in command ---------------------------

KIND = r"""
import json, os, subprocess, sys, time
prompt = sys.stdin.read()
open("prompt.txt", "w").write(prompt)
mode = os.environ["ACLI_KIND"]
if mode == "enkel":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open("enkel.pid", "w").write(str(child.pid))
    if os.environ.get("ACLI_PIDDATEI"):
        open(os.environ["ACLI_PIDDATEI"], "w").write(str(child.pid))
if mode == "link":
    os.symlink("/etc/hostname", "ergebnis.txt")
else:
    open("ergebnis.txt", "w").write("ok " + os.environ.get("ZUGANG_TEST", "")[:0])
try:
    open("../eingang/notiz.txt", "a").write("x")
    print(json.dumps({"type": "probe", "eingang_beschreibbar": True}), flush=True)
except OSError:
    print(json.dumps({"type": "probe", "eingang_beschreibbar": False}), flush=True)
print(json.dumps({"type": "probe", "args": sys.argv[1:], "env": sorted(os.environ)}), flush=True)
for _ in range(1000 if mode in ("enkel", "lang") else 0):
    print(json.dumps({"type": "tick"}), flush=True)
    time.sleep(0.01)
"""


def kind_platz(tmp_path, mode, **env):
    script = tmp_path / "kind.py"
    script.write_text(KIND)
    (tmp_path / "arbeit").mkdir()
    return CliPlatz(
        tmp_path / "arbeit",
        befehl=[sys.executable, str(script)],
        settings=None,
        zugang_variable="ZUGANG_TEST",
        zusatz_env={"ACLI_KIND": mode, **env},
    )


def auftrag(**overrides):
    from src.agentischer_lauf.cli_platz import CliAuftrag

    values = dict(
        schluessel="lauf1",
        prompt="Auftrag",
        systemtext="System",
        model="m",
        max_turns=3,
        max_tokens=50,
        werkzeuge=["Read"],
        material={"notiz.txt": b"Notiz"},
        ausgaben=["ergebnis.txt"],
        zeit_s=20,
        zugang="geheimer-zugang",
    )
    values.update(overrides)
    return CliAuftrag(**values)


async def test_place_stops_process_group_and_cleans_up(tmp_path):
    pid_datei = tmp_path / "enkel.pid"
    platz = kind_platz(tmp_path, "enkel", ACLI_PIDDATEI=str(pid_datei))
    seen = []

    def beobachter(line):
        seen.append(line)
        if line["type"] == "tick":
            raise RunError("grenze_tokens")

    with pytest.raises(RunError) as caught:
        await platz.lauf(auftrag(), beobachter)
    assert caught.value.code == "grenze_tokens"
    assert seen[0] == {"type": "probe", "eingang_beschreibbar": False}
    args, env = seen[1]["args"], seen[1]["env"]
    assert args[args.index("--tools") + 1] == "Read"
    assert args[args.index("--max-turns") + 1] == "3"
    assert "--include-partial-messages" in args
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" in env and "ZUGANG_TEST" in env
    assert not any(k.startswith(("AWS_", "INFISICAL", "AI_BRIDGE")) for k in env)
    assert list((tmp_path / "arbeit").iterdir()) == []
    # The grandchild of the killed process group is gone as well. Only this
    # run's own grandchild is asked, by its PID: a scan of all /proc saw the
    # sleepers of other runs and sessions (BRUMBAU2R P2-5).
    enkel = int(pid_datei.read_text())
    frist = time.monotonic() + 2
    while lebt(enkel) and time.monotonic() < frist:
        time.sleep(0.05)
    assert not lebt(enkel)


async def test_place_time_limit(tmp_path):
    platz = kind_platz(tmp_path, "lang")
    with pytest.raises(RunError) as caught:
        await platz.lauf(auftrag(zeit_s=0.3), lambda line: None)
    assert caught.value.code == "grenze_zeit"


async def test_place_refuses_linked_output(tmp_path):
    platz = kind_platz(tmp_path, "link")
    with pytest.raises(RunError) as caught:
        await platz.lauf(auftrag(), lambda line: None)
    assert caught.value.code == "artefakt_integritaet"
    assert list((tmp_path / "arbeit").iterdir()) == []


async def test_place_returns_declared_outputs_only(tmp_path):
    platz = kind_platz(tmp_path, "kurz")
    ausgang = await platz.lauf(auftrag(), lambda line: None)
    assert ausgang.dateien == {"ergebnis.txt": b"ok "}
    assert ausgang.returncode == 0


# --- Real CLI 2.1.285 against the local Messages stand-in (no AI call) --------

CLI = os.environ.get("ACLI_CLI_BINARY")
ATTRAPPE = os.environ.get("ACLI_ATTRAPPE")


@pytest.mark.skipif(not (CLI and ATTRAPPE), reason="ACLI_CLI_BINARY/ACLI_ATTRAPPE not set")
@pytest.mark.parametrize("szenario", ["werkzeug", "grenze"])
async def test_real_cli_against_stand_in(tmp_path, szenario):
    import asyncio
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    log = tmp_path / "requests.jsonl"
    server = await asyncio.create_subprocess_exec(
        sys.executable, "-I", ATTRAPPE, szenario, str(port), str(log)
    )
    try:
        await asyncio.sleep(0.7)
        (tmp_path / "arbeit").mkdir()
        platz = CliPlatz(
            tmp_path / "arbeit",
            befehl=[CLI],
            settings=None,
            zugang_variable="ANTHROPIC_API_KEY",
            zusatz_env={
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                "HTTPS_PROXY": "http://127.0.0.1:9",
                "NO_PROXY": "127.0.0.1",
            },
        )
        adapters = Adapters()
        adapters.register_cli(
            "cli",
            CliLauf(platz, cli_modell="claude-sonnet-4-5", zugang=lambda: "attrappe-schluessel",
                    zaehler=AsyncMock(return_value=7)),
        )
        rt = Runtime(adapters, Storage(tmp_path / "protected"), revision="t", max_bytes=100000)
        body = cli_payload()
        body["max_turns"] = 10
        body["agentischer_lauf"]["grenzen"]["tokens_gesamt"] = 1000
        started = time.monotonic()
        result = await rt.execute(ResearchRequest(**body), "tenant-a")
        dauer = time.monotonic() - started
    finally:
        server.kill()
        await server.wait()
    requests = [json.loads(z) for z in log.read_text().splitlines()]
    print(f"\n{szenario}: status={result['status']} fehler_code={result['fehler_code']} "
          f"grenze={result['grenze']} anfragen={len(requests)} dauer={dauer:.1f}s")
    assert all(r["max_tokens"] == 50 for r in requests)
    assert all(r["tools"] == ["Edit", "Glob", "Grep", "Read", "Write"] for r in requests)
    assert list((tmp_path / "arbeit").iterdir()) == []
    if szenario == "werkzeug":
        assert result["status"] == "success", result
        assert result["artefakte"][0]["pfad"] == "ergebnis.txt"
        assert result["werkzeugaufrufe"]["eintraege"][0]["material_ids"] == ["notiz.txt"]
    else:
        assert result["status"] == "error"
        assert result["fehler_code"] == "grenze_tokens"
        assert result["grenze"]["ursache"] == "laufende_nutzung_ueber_budget"
        # Killed at the third answer (3 x 420 > 1000), long before max_turns=10.
        assert len(requests) == 3


# --- ACLIR P1: the time limit kills the whole process group at once ---------


def lebt(pid):
    try:
        state = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state not in ("Z", "X")


async def test_place_time_limit_kills_group_at_once(tmp_path):
    """The child alone would tick for 10 s and its grandchild sleep 60 s:
    grenze_zeit must end both right away, not wait for them to finish."""
    pid_datei = tmp_path / "enkel.pid"
    platz = kind_platz(tmp_path, "enkel", ACLI_PIDDATEI=str(pid_datei))
    start = time.monotonic()
    with pytest.raises(RunError) as caught:
        await platz.lauf(auftrag(zeit_s=0.5), lambda line: None)
    assert caught.value.code == "grenze_zeit"
    assert time.monotonic() - start < 5
    enkel = int(pid_datei.read_text())
    frist = time.monotonic() + 2
    while lebt(enkel) and time.monotonic() < frist:
        time.sleep(0.05)
    assert not lebt(enkel)
    assert list((tmp_path / "arbeit").iterdir()) == []


# --- ACLIR P3: the kept stream has a total bound -----------------------------


async def test_place_stream_total_is_bounded(tmp_path):
    platz = kind_platz(tmp_path, "lang")
    platz.max_strom_bytes = 2000
    with pytest.raises(RunError) as caught:
        await platz.lauf(auftrag(), lambda line: None)
    assert caught.value.code == "protokoll_unvollstaendig"
    assert list((tmp_path / "arbeit").iterdir()) == []


@pytest.mark.parametrize("value", [0, 64_000_001])
def test_place_stream_bound_is_validated(tmp_path, value):
    with pytest.raises(ValueError):
        CliPlatz(tmp_path, befehl=["x"], settings=None, max_strom_bytes=value)


# --- ACLIR P2: only a verified place starts ----------------------------------


def test_gate2_requires_dedicated_uid(server, tmp_path):
    with pytest.raises(RunError) as caught:
        verdrahte(konfig(tmp_path, dedicated_uid=False))
    assert caught.value.code == "faehigkeit_nicht_verfuegbar"


def _stat(uid, mode):
    from types import SimpleNamespace
    return SimpleNamespace(st_uid=uid, st_mode=mode)


_ROOT_ORDNER = _stat(0, 0o40755)
_SETTINGS = "/etc/erkunder/settings.json"


def _platz_pruefung(wurzel, settings, *, uid=1101, schreibbar=False, ordner=None,
                    pfad=_SETTINGS):
    """Stand-in stat/access. ``ordner`` overrides single folders above the
    settings (default: root, 0755); ``schreibbar`` is True (every path) or a
    set of paths the place could write."""
    from src.agentischer_lauf.zulassung import pruefe_platz

    infos = {"/arbeit/platz1": wurzel, pfad: settings,
             "/etc/erkunder": _ROOT_ORDNER, "/etc": _ROOT_ORDNER, "/": _ROOT_ORDNER,
             **(ordner or {})}
    gefolgt = []

    def stat(path, follow_symlinks=True):
        gefolgt.append(follow_symlinks)
        info = infos.get(str(path))
        if info is None:
            raise FileNotFoundError(path)
        return info

    def access(path, mode):
        return schreibbar is True or (bool(schreibbar) and str(path) in schreibbar)

    try:
        pruefe_platz(
            Path("/arbeit/platz1"), Path(pfad), uid=uid, stat=stat, access=access
        )
    finally:
        # Never follows a symlink: a link is checked as the link (P2-3).
        assert gefolgt and not any(gefolgt)


def test_place_check_accepts_the_place_container_layout():
    _platz_pruefung(_stat(1101, 0o40700), _stat(0, 0o100644))


@pytest.mark.parametrize("fall", [
    dict(uid=0, wurzel=_stat(0, 0o40700)),
    dict(wurzel=_stat(1000, 0o40700)),
    dict(wurzel=_stat(1101, 0o40750)),
    dict(wurzel=None),
    dict(settings=_stat(1101, 0o100644)),
    dict(schreibbar=True),
    dict(settings=None),
    # BRUMBAU2R P2-3: the folders above the settings, and symlinks.
    dict(settings=_stat(0, 0o120777)),
    dict(ordner={"/etc/erkunder": _stat(1101, 0o40755)}),
    dict(ordner={"/etc/erkunder": _stat(0, 0o40777)}),
    dict(ordner={"/etc/erkunder": _stat(0, 0o40775)}),
    dict(ordner={"/etc": _stat(0, 0o120777)}),
    dict(ordner={"/etc/erkunder": None}),
    dict(schreibbar={"/etc/erkunder"}),
    dict(schreibbar={"/"}),
    dict(pfad="erkunder/settings.json",
         ordner={"erkunder": _ROOT_ORDNER, ".": _ROOT_ORDNER}),
])
def test_place_check_refuses_anything_else(fall, caplog):
    args = dict(wurzel=_stat(1101, 0o40700), settings=_stat(0, 0o100644))
    args.update(fall)
    with pytest.raises(RunError) as caught:
        _platz_pruefung(args.pop("wurzel"), args.pop("settings"), **args)
    assert caught.value.code == "faehigkeit_nicht_verfuegbar"
    assert "kein isolierter Platz" in caplog.text


def test_place_check_on_this_host_refuses_a_symlinked_settings(tmp_path):
    """Real lstat: a link to a root-owned, read-only file is still refused."""
    from src.agentischer_lauf.zulassung import pruefe_platz

    link = tmp_path / "settings.json"
    link.symlink_to("/etc/hostname")
    with pytest.raises(RunError):
        pruefe_platz(tmp_path, link, uid=1101)


def test_place_check_on_this_host_refuses_a_plain_folder(tmp_path):
    """Real stat: a test folder with a settings file the user owns is no place."""
    from src.agentischer_lauf.zulassung import pruefe_platz

    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    with pytest.raises(RunError):
        pruefe_platz(tmp_path, settings)


# --- Bash runs in the CLI place, only a verified one --------------------------


def _bash():
    from src.agentischer_lauf.models import Tool
    return [Tool(name="Bash", parameter={})]


def test_bash_on_cli_needs_a_dedicated_place(tmp_path):
    run = CliLauf(kind_platz(tmp_path, "kurz"), cli_modell="m", zugang=lambda: "x",
                  zaehler=AsyncMock())
    with pytest.raises(RunError) as caught:
        run.werkzeuge(_bash())
    assert caught.value.code == "faehigkeit_nicht_verfuegbar"
    run.platz.dedicated_uid = True
    assert run.werkzeuge(_bash()) == ["Bash"]


def test_bash_in_the_cli_command_line(tmp_path):
    platz = kind_platz(tmp_path, "kurz")
    line = platz.befehlszeile(auftrag(werkzeuge=["Read", "Bash"]))
    assert line[line.index("--tools") + 1] == "Read,Bash"
    assert "Bash" not in line[line.index("--disallowedTools") + 1].split(",")


def test_runtime_admits_bash_through_a_verified_cli_place(tmp_path):
    platz = kind_platz(tmp_path, "kurz")
    runtime, _ = cli_runtime(tmp_path, platz)
    raw = cli_payload()
    raw["agentischer_lauf"]["werkzeuge"].append({"name": "Bash", "parameter": {}})
    body = ResearchRequest(**raw)
    with pytest.raises(RunError) as caught:
        runtime.preflight(body)
    assert caught.value.code == "faehigkeit_nicht_verfuegbar"
    platz.dedicated_uid = True
    runtime.preflight(body)
