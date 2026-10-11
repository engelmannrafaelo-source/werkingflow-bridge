"""CACHEFIX mit der ECHTEN Worker-CLI gegen eine Attrappe der Messages-API.

Kein KI-Aufruf: ANTHROPIC_BASE_URL zeigt auf tests/integration/messages_api_attrappe.py.
Durch das echte ``run_completion`` und das echte claude_code_sdk. Opt-in, weil die CLI
nicht im Repo liegt:

    BRIDGE_TEST_CLAUDE_BIN=/pfad/zu/node_modules/.bin/claude \
        pytest tests/integration/test_cachefix_echte_cli.py

(Version wie Dockerfile.worker: @anthropic-ai/claude-code@2.1.285.) In eigener
pytest-Sitzung starten: andere Unit-Module ersetzen claude_code_sdk durch Stubs.

``ENABLE_PROMPT_CACHING_1H=1`` laesst die CLI mit API-Key so markieren wie auf den
Abo-Workern (1h an System und Umgebungsblock) — das war die Lage in DBD2.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest

CLI_BIN = os.environ.get("BRIDGE_TEST_CLAUDE_BIN")
pytestmark = pytest.mark.skipif(
    not CLI_BIN,
    reason="BRIDGE_TEST_CLAUDE_BIN nicht gesetzt (echte CLI 2.1.285 noetig)",
)

STABIL = "Stabiler Vorspann, ueber Aufrufe gleich. " * 40
AUFGABE = "Aufgabe: antworte mit OK."


@pytest.fixture
def attrappe(tmp_path):
    from tests.integration.messages_api_attrappe import handler

    def start(mode: str):
        out = tmp_path / f"mitschnitt-{mode}"
        out.mkdir()
        srv = HTTPServer(("127.0.0.1", 0), handler(str(out), mode))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}", out

    servers: list = []
    yield start
    for s in servers:
        s.shutdown()


@pytest.fixture
def cli(monkeypatch, tmp_path):
    import src.claude_cli as claude_cli
    from src.claude_cli import ClaudeCodeCLI

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(
        "PATH", f"{Path(CLI_BIN).parent}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    monkeypatch.setenv("DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("CLAUDE_CODE_MAX_RETRIES", "0")
    monkeypatch.setenv("ENABLE_PROMPT_CACHING_1H", "1")
    monkeypatch.setenv("INSTANCE_NAME", "worker-cachefix-cli")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        "src.auth.validate_claude_code_auth",
        lambda: (True, {"valid": True, "method": "stub"}),
    )
    auth = Mock()
    auth.get_claude_code_env_vars.return_value = {}
    monkeypatch.setattr("src.auth.auth_manager", auth)
    work = tmp_path / "work"
    work.mkdir()
    inst = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
    inst.timeout = 90
    inst.cwd = work
    inst.cache_dir = tmp_path
    inst.max_cache_size_mb = 10
    # Wie die Auth-Variablen der Bridge: nach der Pruefung "kein ANTHROPIC_API_KEY
    # im Prozess-Env" gesetzt. Der Schluessel ist eine Attrappe fuer die Attrappe.
    inst.claude_env_vars = {"ANTHROPIC_API_KEY": "sk-ant-attrappe"}
    inst.file_discovery = Mock()
    inst.file_discovery.discover_files_from_sdk_messages.return_value = []
    inst.file_discovery.discover_files_from_directory_scan.return_value = []
    assert claude_cli.query is not None
    return inst


async def _lauf(cli, base_url, monkeypatch, **kw):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", base_url)
    return [
        c
        async for c in cli.run_completion(model="claude-sonnet-5-5", max_turns=1, **kw)
    ]


def _marken(out: Path):
    """cache_control aller Bloecke der ersten Messages-Anfrage, in API-Reihenfolge."""
    for f in sorted(out.glob("req-*.json")):
        req = json.loads(f.read_text())
        if "messages" not in req:
            continue
        marks = [
            b.get("cache_control")
            for b in req.get("system") or []
            if isinstance(b, dict)
        ]
        for m in req["messages"]:
            if isinstance(m["content"], list):
                marks += [b.get("cache_control") for b in m["content"]]
        return marks, (f.with_suffix(".verdict")).read_text()
    raise AssertionError(f"keine Messages-Anfrage in {out}")


async def test_grenze_mit_1h_cli_laeuft_durch(cli, attrappe, monkeypatch):
    """(b) normale Antwort: mit Grenze stimmt die ttl-Reihenfolge, die Attrappe
    antwortet 200, der Lauf liefert OK. Auf 276d146 rot: 400 als Inhalt."""
    url, out = attrappe("regel")
    prompt = "Human: " + STABIL + "\n" + AUFGABE
    chunks = await _lauf(
        cli, url, monkeypatch, prompt=prompt, cache_boundary=len("Human: " + STABIL)
    )
    marks, verdict = _marken(out)
    assert verdict == "200", (verdict, marks)
    assert {"type": "ephemeral"} in marks  # unsere Grenze ist angekommen
    assert all(m is None or m.get("ttl", "5m") == "5m" for m in marks), marks
    assert any("OK" in str(c) for c in chunks)
    assert not any("API Error" in str(c) for c in chunks)


async def test_ohne_grenze_bleibt_die_cli_bei_ihrer_ttl(cli, attrappe, monkeypatch):
    url, out = attrappe("regel")
    await _lauf(cli, url, monkeypatch, prompt="Human: " + AUFGABE)
    marks, verdict = _marken(out)
    assert verdict == "200"
    assert {"type": "ephemeral", "ttl": "1h"} in marks  # unveraendert: CLI-eigene Wahl


async def test_api_fehler_ist_ein_fehler(cli, attrappe, monkeypatch):
    """(a) die API weist ab: CliApiError mit Status und Ursache, kein Inhalt."""
    from src.middleware.bridge_error import CliApiError

    url, _ = attrappe("immer400")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
    got = []
    with pytest.raises(CliApiError) as ei:
        async for c in cli.run_completion(
            model="claude-sonnet-5-5", max_turns=1, prompt="Human: " + AUFGABE
        ):
            got.append(c)
    assert ei.value.status == 400
    assert "cache_control.ttl" in ei.value.text
    assert not any("API Error" in str(c) for c in got)
