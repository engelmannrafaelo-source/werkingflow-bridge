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


# ── BRREFUSAL R2 (BRREFUSALR P1): the CLI's other-model fallback ──────────────
# Category cyber and a refusal without text have a fallback model in the CLI
# (model_refusal_fallback); reasoning_extraction (FK32) has none. With only the
# same-model retry off, the second request went to another model and the run
# came back as finish_reason=stop. Each form: (attrappe sequence, category).
ACHT_FORMEN = {
    "cyber_dann_ok": ("folge:cyber,ok", "cyber"),
    "ohne_text_dann_ok": ("folge:cyber_leer,ok", "cyber"),
    "cyber_immer": ("folge:cyber", "cyber"),
    "cyber_zweimal_dann_ok": ("folge:cyber,cyber,ok", "cyber"),
    "reasoning_extraction_fk32": ("folge:refusal", "reasoning_extraction"),
    "cyber_nach_werkzeugzug": ("folge:tool,cyber,ok", "cyber"),
    "cyber_nach_max_tokens": ("folge:maxtok,cyber,ok", "cyber"),
    "reasoning_nach_werkzeugzug": ("folge:tool,refusal,ok", "reasoning_extraction"),
}
ANGEFRAGT = "claude-sonnet-5-5"


def _art(verdict: str) -> str:
    return verdict.split()[1]


@pytest.mark.parametrize("form", list(ACHT_FORMEN))
async def test_acht_formen_ein_refusal_aufruf_und_422(cli, attrappe, monkeypatch, form):  # noqa: F811
    """Default (both CLI follow-ups off): the refused request is the LAST one
    — no retry, no other model — and the run ends as 422 modell_abbruch.
    Tool turn / max_tokens continuation before it keep their own request."""
    from src.middleware.bridge_error import CliModellAbbruch

    monkeypatch.delenv("BRIDGE_CLI_REFUSAL_RETRY", raising=False)
    monkeypatch.delenv("BRIDGE_CLI_REFUSAL_FALLBACK", raising=False)
    mode, kategorie = ACHT_FORMEN[form]
    got, err, anfragen = await _lauf(cli, attrappe, monkeypatch, mode)
    vorlauf = [a for a in mode.split(":", 1)[1].split(",") if a in ("tool", "maxtok")]
    arten = [_art(a) for a in anfragen]
    assert arten == vorlauf + [arten[-1]], anfragen
    assert arten[-1] in ("refusal", "cyber", "cyber_leer"), anfragen
    assert all(a.endswith(f"model={ANGEFRAGT}") for a in anfragen), anfragen
    assert isinstance(err, CliModellAbbruch), (err, anfragen)
    assert err.refusal["category"] == kategorie
    assert err.refusal["quelle"] in ("model_refusal_no_fallback", "api_error_turn")
    antwort = err.envelope()
    assert antwort.status_code == 422 and "retry-after" not in antwort.headers


async def test_ersatzmodell_wird_nie_als_antwort_angenommen(cli, attrappe, monkeypatch):  # noqa: F811
    """Second net: a CLI that falls back anyway (here: the bridge switch lets
    it) — model_refusal_fallback ends the run as modell_abbruch, and nothing
    the fallback model writes reaches the caller."""
    from src.middleware.bridge_error import CliModellAbbruch

    monkeypatch.delenv("BRIDGE_CLI_REFUSAL_RETRY", raising=False)
    monkeypatch.setenv("BRIDGE_CLI_REFUSAL_FALLBACK", "true")
    got, err, anfragen = await _lauf(cli, attrappe, monkeypatch, "folge:cyber,ok")
    assert isinstance(err, CliModellAbbruch), (err, anfragen)
    assert err.refusal["quelle"] == "model_refusal_fallback"
    assert err.refusal["fallback_model"] and err.refusal["fallback_model"] != ANGEFRAGT
    assert err.envelope().status_code == 422
    texte = [
        b.get("text") if isinstance(b, dict) else getattr(b, "text", None)
        for c in got if isinstance(c, dict) for b in (c.get("content") or [])
    ]
    assert "OK" not in texte, texte
