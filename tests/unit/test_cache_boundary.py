"""CACHE2 Fix 1: die mitgeschickte Cache-Grenze kommt bis zur CLI-Schnittstelle.

Die Kette ist dieselbe wie in main.py (Streaming und Nicht-Streaming):
  ChatCompletionRequest → mark_messages → [Anonymisierung] → messages_to_prompt
  → filter_content → split_prompt → run_completion(cache_boundary=…) → query(prompt=…)

Die Uebergabe an ``query`` (claude_code_sdk), also die CLI-Schnittstelle, prueft
test_cache_boundary_cli.py — getrennt, weil ein frueher gesammeltes Testmodul das SDK
in sys.modules stubbt und jeder Modul-Import von src.claude_cli in der Gesamtsuite
daran scheitert (vorbestehend, trifft test_claude_cli.py und test_argv_prompt_fix.py
genauso). Dass die CLI 2.1.285 Bloecke mit
``cache_control`` aus dem Stream-JSON-Eingang unveraendert in die API-Anfrage
uebernimmt, ist gegen eine Attrappe der Messages-API gemessen
(CACHE2-ergebnis.md) — hier nicht wiederholbar ohne die CLI.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from src import cache_boundary
from src.cache_boundary import SENTINEL, cli_user_content, mark_messages, split_prompt
from src.message_adapter import MessageAdapter
from src.models import ChatCompletionRequest, Message

STABIL = "PRUEFWISSEN " + "Register und Dokumente. " * 50
AUFGABE = "Aufgabe: nur dieser Teil wechselt je Aufruf."


def _req(content, system="Du bist ein Pruefer."):
    return ChatCompletionRequest(model="sonnet", messages=[
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ])


def _chain(messages, anonymize=None):
    """Die Schritte aus main.py zwischen process_messages und run_completion."""
    msgs, requested = mark_messages(messages)
    if anonymize:
        msgs = [Message(role=m["role"], content=m["content"])
                for m in anonymize([{"role": m.role, "content": m.content} for m in msgs])]
    prompt, system = MessageAdapter.messages_to_prompt(msgs)
    prompt = MessageAdapter.filter_content(prompt)
    return split_prompt(prompt, requested) + (system,)


def _marked_blocks():
    return [
        {"type": "text", "text": STABIL, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": AUFGABE},
    ]


class TestRequestModel:
    def test_marked_block_keeps_flat_text_and_remembers_boundary(self):
        r = _req(_marked_blocks())
        user = r.messages[1]
        assert user.content == STABIL + "\n" + AUFGABE      # Text wie bisher
        assert user.cache_prefix_chars == len(STABIL)

    def test_last_mark_wins(self):
        r = _req([
            {"type": "text", "text": "A", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "BB", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "rest"},
        ])
        assert r.messages[1].cache_prefix_chars == len("A\nBB")

    def test_no_mark_no_boundary(self):
        r = _req([{"type": "text", "text": STABIL}, {"type": "text", "text": AUFGABE}])
        assert r.messages[1].cache_prefix_chars is None
        assert _req("plain").messages[1].cache_prefix_chars is None

    def test_unknown_cache_control_key_is_rejected_not_dropped(self):
        with pytest.raises(ValidationError):
            _req([{"type": "text", "text": "x", "cache_control": {"type": "ephemeral", "ttl": "1h"}}])

    def test_reserved_marker_chars_in_request_rejected(self):
        with pytest.raises(ValidationError):
            _req("text mit " + SENTINEL)
        with pytest.raises(ValidationError):
            _req([{"type": "text", "text": "xy"}])


class TestChain:
    def test_boundary_survives_to_the_split(self):
        prompt, at, status, system = _chain(_req(_marked_blocks()).messages)
        assert status == "applied"
        assert prompt == "Human: " + STABIL + "\n" + AUFGABE   # identischer Text
        assert prompt[:at] == "Human: " + STABIL
        assert system == "Du bist ein Pruefer."

    def test_without_mark_everything_is_as_before(self):
        plain = _req(STABIL + "\n" + AUFGABE).messages
        prompt, at, status, _ = _chain(plain)
        assert (at, status) == (None, None)
        assert prompt == MessageAdapter.filter_content(MessageAdapter.messages_to_prompt(plain)[0])
        assert cli_user_content(prompt, at) == prompt          # String, keine Bloecke

    def test_boundary_survives_anonymization_that_changes_lengths(self):
        def anonymize(msgs):  # Nachbildung: ersetzt Namen VOR und NACH der Grenze
            return [{"role": m["role"],
                     "content": m["content"].replace("Register", "<ORG_1>").replace("Aufgabe", "<X>")}
                    for m in msgs]
        prompt, at, status, _ = _chain(_req(_marked_blocks()).messages, anonymize)
        assert status == "applied"
        assert prompt[:at] == "Human: " + STABIL.replace("Register", "<ORG_1>")
        assert prompt[at:] == "\n" + AUFGABE.replace("Aufgabe", "<X>")

    def test_lost_marker_is_loud_and_leaves_no_trace(self, caplog):
        def drop_marker(msgs):
            return [{"role": m["role"], "content": m["content"].replace("", "")} for m in msgs]
        with caplog.at_level("ERROR"):
            prompt, at, status, _ = _chain(_req(_marked_blocks()).messages, drop_marker)
        assert at is None and status == cache_boundary.LOST_SENTINEL_MISSING
        assert not cache_boundary.contains_sentinel_chars(prompt)
        assert "OHNE Cache-Grenze" in caplog.text

    def test_session_history_is_not_marked(self):
        msgs = _req(_marked_blocks()).messages
        mark_messages(msgs)
        assert SENTINEL not in msgs[1].content   # Eingabe unveraendert (Session speichert sie)

    def test_last_marked_user_message_of_a_conversation_counts(self):
        msgs = [
            Message(role="user", content=[{"type": "text", "text": "frueh", "cache_control": {"type": "ephemeral"}}]),
            Message(role="assistant", content="ok"),
            Message(role="user", content=_marked_blocks()),
        ]
        prompt, at, status, _ = _chain(msgs)
        assert status == "applied"
        assert prompt[:at].endswith(STABIL) and prompt[at:] == "\n" + AUFGABE


class TestCliContent:
    def test_two_blocks_join_to_the_prompt(self):
        p = "Human: stabil\nrest"
        blocks = cli_user_content(p, len("Human: stabil"))
        assert blocks == [
            {"type": "text", "text": "Human: stabil", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "\nrest"},
        ]
        assert "".join(b["text"] for b in blocks) == p

    def test_boundary_at_end_gives_one_marked_block(self):
        assert cli_user_content("abc", 3) == [
            {"type": "text", "text": "abc", "cache_control": {"type": "ephemeral"}}]

    def test_exactly_one_cache_mark(self):
        # API limit 4: CLI already sets 3 (2 system, 1 environment block)
        blocks = cli_user_content("x" * 100, 40)
        assert sum(1 for b in blocks if "cache_control" in b) == 1


def test_main_passes_boundary_on_every_chat_cli_call():
    main = (Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()
    assert main.count("cache_boundary.mark_messages(all_messages)") == 2      # stream + non-stream
    assert main.count("cache_boundary.split_prompt(") == 2
    assert main.count("cache_boundary=cache_boundary_at,") == 3             # + tool-leak retry
