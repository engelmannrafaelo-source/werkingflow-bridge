"""BRREFUSAL mit der ECHTEN Worker-CLI gegen die Messages-API-Attrappe. Kein KI-Aufruf.

Opt-in wie test_cachefix_echte_cli.py (BRIDGE_TEST_CLAUDE_BIN, eigene pytest-Sitzung).
Die Attrappe antwortet mit stop_reason "refusal" (Modus "refusal") bzw. erst refusal,
dann normal ("refusal_dann_ok"). Gemessen wird, wie viele Messages-Anfragen die CLI
schickt — der FK32-Folgezug ist die zweite — und was run_completion daraus macht.
"""

from __future__ import annotations

import pytest

from tests.integration.test_cachefix_echte_cli import (  # noqa: F401  (fixtures)
    CLI_BIN,
    attrappe,
    cli,
)

pytestmark = pytest.mark.skipif(
    not CLI_BIN, reason="BRIDGE_TEST_CLAUDE_BIN nicht gesetzt (echte CLI noetig)"
)


async def _lauf(cli, attrappe, monkeypatch, mode):  # noqa: F811
    url, out = attrappe(mode)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
    got, err = [], None
    try:
        async for c in cli.run_completion(model="claude-sonnet-5-5", max_turns=10,
                                          prompt="Human: Aufgabe: antworte mit OK."):
            got.append(c)
    except Exception as e:  # noqa: BLE001 — the test reads it below
        err = e
    anfragen = [f.read_text() for f in sorted(out.glob("req-*.verdict"))]
    return got, err, anfragen


@pytest.mark.parametrize("mode", ["refusal", "refusal_dann_ok"])
async def test_refusal_ein_aufruf_und_modell_abbruch(cli, attrappe, monkeypatch, mode):  # noqa: F811
    """Standard: the CLI does not retry — one Messages request — and the run
    ends as CliModellAbbruch (422 modell_abbruch, not retryable)."""
    from src.middleware.bridge_error import CliModellAbbruch

    monkeypatch.delenv("BRIDGE_CLI_REFUSAL_RETRY", raising=False)
    got, err, anfragen = await _lauf(cli, attrappe, monkeypatch, mode)
    assert anfragen == ["200 refusal"], anfragen
    assert isinstance(err, CliModellAbbruch), err
    assert err.retryable is False and err.refusal["category"] == "reasoning_extraction"
    assert not any("safeguards flagged" in str(c) for c in got)


async def test_mit_retry_schickt_die_cli_den_folgezug(cli, attrappe, monkeypatch):  # noqa: F811
    """What the switch turns off (FK32): with the CLI's retry the full context
    goes out a second time — and the bridge would hold that answer back
    anyway (nach_modellabbruch)."""
    monkeypatch.setenv("BRIDGE_CLI_REFUSAL_RETRY", "true")
    got, err, anfragen = await _lauf(cli, attrappe, monkeypatch, "refusal_dann_ok")
    assert anfragen == ["200 refusal", "200"], anfragen
    assert err is None
    from src.claude_cli import lauf_abschluss
    ab = lauf_abschluss(got)
    assert ab.modell_abbruch and ab.folgezuege == 1


async def test_normaler_lauf_unberuehrt(cli, attrappe, monkeypatch):  # noqa: F811
    monkeypatch.delenv("BRIDGE_CLI_REFUSAL_RETRY", raising=False)
    got, err, anfragen = await _lauf(cli, attrappe, monkeypatch, "regel")
    assert err is None and anfragen == ["200"]
    assert any("OK" in str(c) for c in got)
