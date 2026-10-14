"""BRIDGETEXT (14.10.2026): an answer the CLI continued comes back whole.

Befund TEILE (werking-energy, Mühl): the worker CLI runs with
CLAUDE_CODE_MAX_OUTPUT_TOKENS=64000. At that limit it withholds the cut message,
appends a meta user turn ("Output token limit hit. Resume directly …") and lets
the model continue — up to 3 times (CLI 2.1.296). usage is the sum over all
calls (Mühl's long description: 71 197 output tokens), but parse_claude_message
returned at the FIRST assistant message with text: the chat route answered 200
with part 1 only. The SSE stream was never affected — it sends every text
block as it comes, joined directly.

Streams go through the REAL run_completion (only `query` is replaced) and the
real chat routes; stream-json lines go through the real SDK parser hook, so
message ids and stop fields arrive as on a worker. Per CLI call: a thinking
block, then the text block — one assistant message each, same message id.
"""
from __future__ import annotations

# First: it puts SDK stand-ins in place where the SDK is not installed.
from tests.unit.test_chat_modell_abbruch import (  # noqa: F401  (fixture)
    UserMessage,
    _chunks,
    _parsed,
    _stream,
    _sync_chat,
    frischer_zustand,
)
from src.claude_cli import FORTSETZUNG_KEY, antwort_teile, lauf_abschluss  # noqa: E402
from tests import sdk_strom as sdk  # noqa: E402

META_TEXT = (
    "Output token limit hit. Resume directly — no apology, no recap of what you "
    "were doing. Pick up mid-thought if that is where the cut happened. "
    "Break remaining work into smaller pieces."
)
USAGE = {"input_tokens": 3, "cache_creation_input_tokens": 0,
         "cache_read_input_tokens": 0, "output_tokens": 64000}
USAGE_REST = dict(USAGE, output_tokens=7197)
USAGE_SUMME = dict(USAGE, input_tokens=6, output_tokens=71197)

# Synthetic numbers, as in the probe (local-storage/teile/probe-lang): the cut
# sits inside a number, so any separator at the seam is visible.
TEIL_1 = "\n".join(str(i) for i in range(1, 400)) + "\n4"
TEIL_2 = "00\n" + "\n".join(str(i) for i in range(401, 600)) + "\nENDE"
GANZ = "\n".join(str(i) for i in range(1, 600)) + "\nENDE"


def _zug(msg_id, text, stop_reason, usage):
    return [
        _parsed(msg_id, [{"type": "thinking", "thinking": ""}], stop_reason, usage),
        _parsed(msg_id, [{"type": "text", "text": text}], stop_reason, usage),
    ]


def fortsetzungs_verlauf(*teile, letzter_stop="end_turn"):
    """Part 1 … part n, the CLI's meta turn between each two."""
    out = [sdk.init()]
    for i, text in enumerate(teile):
        if i:
            out.append(UserMessage(content=[sdk.TextBlock(text=META_TEXT)]))
        letzter = i == len(teile) - 1
        out += _zug(f"msg_{i}", text, letzter_stop if letzter else None,
                    USAGE_REST if letzter else USAGE)
    out.append(sdk.result("success", usage=USAGE_SUMME, num_turns=len(teile)))
    return out


async def test_run_completion_liefert_alle_teile(monkeypatch, tmp_path):
    chunks = await _chunks(monkeypatch, tmp_path, fortsetzungs_verlauf(TEIL_1, TEIL_2))
    text, fortsetzungen = antwort_teile(chunks)
    assert text == GANZ
    assert fortsetzungen == 1
    abschluss = lauf_abschluss(chunks, antwort_zusammengesetzt=True)
    assert abschluss.finish_reason == "stop"
    assert abschluss.details() == {"stop_reason": "max_tokens_fortgesetzt",
                                   "vollstaendig": True, "fortsetzungen": 1}
    # Paths that read the LAST message as the answer: unchanged, still cut.
    assert lauf_abschluss(chunks).finish_reason == "length"


async def test_drei_fortsetzungen(monkeypatch, tmp_path):
    chunks = await _chunks(monkeypatch, tmp_path,
                           fortsetzungs_verlauf("a1", "b2", "c3", "d4"))
    assert antwort_teile(chunks) == ("a1b2c3d4", 3)


async def test_fortsetzungen_erschoepft_bleibt_length_ohne_fehlertext(monkeypatch, tmp_path):
    """After the third continuation the CLI gives up: the last part stopped at the
    limit, then its own error message (top-level error max_output_tokens)."""
    verlauf = fortsetzungs_verlauf("a1", "b2", letzter_stop="max_tokens")
    fehler = _parsed("msg_err", [{"type": "text", "text": "API Error: Claude's response "
                                  "exceeded the 64000 output token maximum."}], None, USAGE)
    fehler._bridge_cli_error = "max_output_tokens"
    verlauf.insert(-1, fehler)
    chunks = await _chunks(monkeypatch, tmp_path, verlauf)
    assert antwort_teile(chunks) == ("a1b2", 1)
    abschluss = lauf_abschluss(chunks, antwort_zusammengesetzt=True)
    assert abschluss.finish_reason == "length"
    assert abschluss.fortsetzungen == 0


async def test_teil_1_nur_gedacht_text_beginnt_in_der_fortsetzung(monkeypatch, tmp_path):
    verlauf = fortsetzungs_verlauf("x", "Die ganze Antwort.")
    del verlauf[2]  # part 1's text block: the call was cut while thinking
    chunks = await _chunks(monkeypatch, tmp_path, verlauf)
    assert antwort_teile(chunks) == ("Die ganze Antwort.", 1)
    assert lauf_abschluss(chunks, antwort_zusammengesetzt=True).finish_reason == "stop"


def test_werkzeugaufruf_beendet_die_antwort_wie_bisher():
    """Text before a tool call is a preamble; the answer after it is not
    appended (unchanged behaviour), nor is a continuation behind the tool."""
    chunks = [
        {"content": [{"type": "text", "text": "Ich schaue nach."}], "message_id": "a"},
        {"content": [{"type": "tool_use", "id": "t", "name": "Read", "input": {}}],
         "message_id": "a"},
        {"content": [{"type": "text", "text": "Teil 1"}], "message_id": "b"},
        {"content": [{"type": "text", "text": " Teil 2"}], "message_id": "c",
         FORTSETZUNG_KEY: True},
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3},
    ]
    assert antwort_teile(chunks) == ("Ich schaue nach.", 0)
    # The continuation of the final answer is not in that text → not "stop".
    assert lauf_abschluss(chunks, antwort_zusammengesetzt=True).finish_reason == "length"


def test_fremder_zug_ohne_fortsetzungsmarke_endet_die_antwort():
    chunks = [
        {"content": [{"type": "text", "text": "eins"}], "message_id": "a"},
        {"content": [{"type": "text", "text": "zwei"}], "message_id": "a"},
        {"content": [{"type": "text", "text": "fremd"}], "message_id": "b"},
    ]
    assert antwort_teile(chunks) == ("eins\nzwei", 0)


def test_sync_chat_gibt_die_ganze_antwort(monkeypatch, tmp_path):
    resp = _sync_chat(monkeypatch, tmp_path, fortsetzungs_verlauf(TEIL_1, TEIL_2))
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    choice = body["choices"][0]
    assert choice["message"]["content"] == GANZ
    assert "Output token limit" not in choice["message"]["content"]
    assert choice["finish_reason"] == "stop"
    assert body["x_bridge_finish"]["fortsetzungen"] == 1
    assert body["x_bridge_finish"]["stop_reason"] == "max_tokens_fortgesetzt"
    assert body["usage"]["completion_tokens"] == 71197
    assert [z["output_tokens"] for z in body["x_bridge_usage_zuege"]] == [64000, 7197]


def test_sync_chat_erschoepft_meldet_length(monkeypatch, tmp_path):
    resp = _sync_chat(monkeypatch, tmp_path,
                      fortsetzungs_verlauf("a1", "b2", letzter_stop="max_tokens"))
    assert resp.status_code == 200, resp.text[:300]
    choice = resp.json()["choices"][0]
    assert choice["finish_reason"] == "length"
    assert choice["message"]["content"] == "a1b2"


async def test_streaming_gleiche_naht_und_stop(monkeypatch, tmp_path):
    _, daten = await _stream(monkeypatch, tmp_path, fortsetzungs_verlauf(TEIL_1, TEIL_2))
    text = "".join(d["choices"][0]["delta"].get("content", "") for d in daten)
    assert text == GANZ
    assert daten[-1]["choices"][0]["finish_reason"] == "stop"
    assert daten[-1]["x_bridge_finish"]["fortsetzungen"] == 1

