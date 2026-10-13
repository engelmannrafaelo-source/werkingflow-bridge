"""ERKNEUR P2-1: optional output targets (ausgabe.artefakte_optional).

What lies under an optional target comes back with provenance like a declared
target; absence is no antwort_fehlt. Empty means absent everywhere the output
contract is serialised, so every existing wire digest stays the same.
"""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.agentischer_lauf.cli_platz import CliAusgang
from src.agentischer_lauf.runtime import request_digest
from src.models import ResearchRequest

from .test_cli import Platz, cli_payload, cli_runtime, strom
from .test_contract import payload
from .test_runtime import reply, runtime

OPTIONAL = ["skripte", "antwort-pruefer.md"]


def with_output(body, **ausgabe):
    body["agentischer_lauf"]["ausgabe"] = ausgabe
    return body


# ------------------------------------------------------------------ contract
@pytest.mark.parametrize(
    "ausgabe",
    [
        {"artefakte": [], "artefakte_optional": ["/abs"]},
        {"artefakte": [], "artefakte_optional": ["a/../b"]},
        {"artefakte": [], "artefakte_optional": ["skripte", "skripte"]},
        {"artefakte": [], "artefakte_optional": ["skripte", "skripte/a.py"]},
        {"artefakte": [], "artefakte_optional": ["skripte/a.py", "skripte"]},
        {"artefakte": ["skripte"], "artefakte_optional": ["skripte"]},
        {"artefakte": ["skripte"], "artefakte_optional": ["skripte/a.py"]},
        {"artefakte": ["skripte/a.py"], "artefakte_optional": ["skripte"]},
        {"artefakte": [], "artefakte_optional": "skripte"},
    ],
)
def test_invalid_optional_targets_are_refused(ausgabe):
    with pytest.raises(ValidationError, match="auftrag_ungueltig|list"):
        ResearchRequest(**with_output(payload(), modus="text", **ausgabe))


def test_optional_target_must_not_touch_the_answer_file():
    body = with_output(payload(antwort_im_text=False), modus="datei", datei="bericht.md",
                       artefakte=[], artefakte_optional=["bericht.md"])
    with pytest.raises(ValidationError, match="overlaps"):
        ResearchRequest(**body)


def test_optional_target_in_review_output_is_validated_too():
    body = payload()
    body["agentischer_lauf"]["pruefung"] = {
        "auftrag": "Pruefe.", "model": "synthetic", "werkzeuge": [],
        "ausgabe": {"modus": "text", "artefakte": ["n"], "artefakte_optional": ["n"]},
        "max_turns": 1, "max_tokens": 10,
    }
    with pytest.raises(ValidationError, match="overlaps"):
        ResearchRequest(**body)


def test_empty_optional_is_absent_digest_and_model_contract_unchanged():
    implicit = ResearchRequest(**payload())
    explicit = ResearchRequest(**with_output(payload(), modus="text", artefakte=[],
                                             artefakte_optional=[]))
    assert request_digest(implicit) == request_digest(explicit)
    assert "artefakte_optional" not in implicit.agentischer_lauf.ausgabe.model_dump()
    assert "artefakte_optional" not in explicit.agentischer_lauf.ausgabe.model_dump()
    filled = ResearchRequest(**with_output(payload(), modus="text", artefakte=[],
                                           artefakte_optional=["skripte"]))
    assert request_digest(filled) != request_digest(implicit)
    assert filled.agentischer_lauf.ausgabe.model_dump()["artefakte_optional"] == ["skripte"]


def test_optional_wire_digest_vector():
    document = (Path(__file__).parents[2] / "docs/agentischer-lauf.md").read_text()
    contract = json.loads(document.split("```json wire-digest-contract\n", 1)[1]
                          .split("```", 1)[0])
    marker = "```json wire-digest-vector-optional\n"
    assert marker in document
    vector = json.loads(document.split(marker, 1)[1].split("```", 1)[0])
    normalized = vector["normalized"]
    encoded = json.dumps(normalized, **contract["json_options"]).encode(contract["encoding"])
    assert hashlib.sha256(encoded).hexdigest() == vector["sha256"]
    lauf = normalized["agentischer_lauf"]
    assert lauf["ausgabe"]["artefakte_optional"] == OPTIONAL
    assert "artefakte_optional" not in lauf["pruefung"]["ausgabe"]
    assert request_digest(ResearchRequest(**vector["request"])) == vector["sha256"]
    other = copy.deepcopy(vector["request"])
    other["agentischer_lauf"]["ausgabe"]["artefakte_optional"] = list(reversed(OPTIONAL))
    assert request_digest(ResearchRequest(**other)) != vector["sha256"]


# -------------------------------------------------------------- runtime path
def runtime_body():
    body = payload()
    body["agentischer_lauf"]["werkzeuge"] = [{"name": "Write", "parameter": {}}]
    body["agentischer_lauf"]["ausgabe"] = {"modus": "text", "artefakte": ["ergebnis.txt"],
                                           "artefakte_optional": OPTIONAL}
    return body


def writes(*paths):
    return [{"id": f"c{i}", "name": "Write", "input": {"path": p, "content": "x" * (i + 1)}}
            for i, p in enumerate(paths)]


async def test_runtime_returns_present_optional_with_provenance(tmp_path):
    rt, _ = runtime(tmp_path, [reply("d", "tool_use", writes("ergebnis.txt", "skripte/a.py")),
                               reply()])
    result = await rt.execute(ResearchRequest(**runtime_body()), "a")
    assert result["status"] == "success", result.get("fehler_code")
    item = next(a for a in result["artefakte"] if a["pfad"] == "skripte/a.py")
    assert item["sha256"] == hashlib.sha256(b"xx").hexdigest() and item["bytes"] == 2
    assert rt.storage.artifact("a", "run-1", item["ref"].rsplit("=", 1)[-1]) == b"xx"
    assert "skripte/a.py" in [a["pfad"] for a in result["herkunft"]["artefakte"]]
    assert "antwort-pruefer.md" not in [a["pfad"] for a in result["artefakte"]]


async def test_runtime_absent_optional_is_no_antwort_fehlt(tmp_path):
    rt, _ = runtime(tmp_path, [reply("d", "tool_use", writes("ergebnis.txt")), reply()])
    result = await rt.execute(ResearchRequest(**runtime_body()), "a")
    assert result["status"] == "success"
    assert [a["pfad"] for a in result["artefakte"]] == ["ergebnis.txt"]


async def test_runtime_absent_declared_target_still_fails(tmp_path):
    rt, _ = runtime(tmp_path, [reply("d", "tool_use", writes("skripte/a.py")), reply()])
    result = await rt.execute(ResearchRequest(**runtime_body()), "a")
    assert result["fehler_code"] == "antwort_fehlt"


async def test_runtime_write_outside_every_target_fails(tmp_path):
    rt, _ = runtime(tmp_path, [reply("d", "tool_use", writes("ergebnis.txt", "anderswo.py")),
                               reply()])
    result = await rt.execute(ResearchRequest(**runtime_body()), "a")
    assert result["fehler_code"] == "werkzeug_fehler"


async def test_runtime_review_writes_its_own_optional_target(tmp_path):
    body = runtime_body()
    body["agentischer_lauf"]["pruefung"] = {
        "auftrag": "Pruefe.", "model": "synthetic",
        "werkzeuge": [{"name": "Write", "parameter": {}}],
        "ausgabe": {"modus": "text", "artefakte": [], "artefakte_optional": ["notizen"]},
        "max_turns": 2, "max_tokens": 40,
    }
    rt, _ = runtime(tmp_path, [reply("d", "tool_use", writes("ergebnis.txt")), reply(),
                               reply("p", "tool_use", writes("notizen/n.md")), reply("urteil")])
    result = await rt.execute(ResearchRequest(**body), "a")
    assert result["status"] == "success", result.get("fehler_code")
    assert "notizen/n.md" in [a["pfad"] for a in result["pruefung"]["artefakte"]]


# ------------------------------------------------------------------ CLI path
class DeklarierterPlatz(Platz):
    """Like CliPlatz.ausgaben: only files under the order's declared outputs
    come back from the place."""

    async def lauf(self, auftrag, beobachter):
        ausgang = await super().lauf(auftrag, beobachter)
        dateien = {p: b for p, b in ausgang.dateien.items()
                   if any(p == t or p.startswith(t + "/") for t in auftrag.ausgaben)}
        return CliAusgang(ausgang.zeilen, ausgang.returncode, dateien)


def cli_body():
    body = cli_payload()
    body["agentischer_lauf"]["ausgabe"]["artefakte_optional"] = OPTIONAL
    return body


async def test_cli_returns_present_optional_with_provenance(tmp_path):
    platz = DeklarierterPlatz(strom("werkzeug"), {"ergebnis.txt": b"E\n",
                                                  "skripte/rechne.py": b"print(1)\n"})
    rt, _ = cli_runtime(tmp_path, platz)
    result = await rt.execute(ResearchRequest(**cli_body()), "tenant-a")
    assert result["status"] == "success", result.get("fehler_code")
    assert set(platz.auftraege[0].ausgaben) == {"ergebnis.txt", *OPTIONAL}
    item = next(a for a in result["artefakte"] if a["pfad"] == "skripte/rechne.py")
    assert item["sha256"] == hashlib.sha256(b"print(1)\n").hexdigest() and item["bytes"] == 9
    assert rt.storage.artifact("tenant-a", "run-1", item["ref"].rsplit("=", 1)[-1]) == b"print(1)\n"
    assert "skripte/rechne.py" in [a["pfad"] for a in result["herkunft"]["artefakte"]]


async def test_cli_absent_optional_is_no_antwort_fehlt(tmp_path):
    platz = DeklarierterPlatz(strom("werkzeug"), {"ergebnis.txt": b"E\n"})
    rt, _ = cli_runtime(tmp_path, platz)
    result = await rt.execute(ResearchRequest(**cli_body()), "tenant-a")
    assert result["status"] == "success", result.get("fehler_code")
    assert [a["pfad"] for a in result["artefakte"]] == ["ergebnis.txt"]


async def test_cli_absent_declared_target_still_fails(tmp_path):
    platz = DeklarierterPlatz(strom("werkzeug"), {"skripte/rechne.py": b"x"})
    rt, _ = cli_runtime(tmp_path, platz)
    result = await rt.execute(ResearchRequest(**cli_body()), "tenant-a")
    assert result["fehler_code"] == "antwort_fehlt"


async def test_cli_review_order_carries_its_optional_targets(tmp_path):
    platz = DeklarierterPlatz(strom("werkzeug"), {"ergebnis.txt": b"E\n", "notizen/n.md": b"N"})
    rt, _ = cli_runtime(tmp_path, platz)
    body = cli_body()
    body["agentischer_lauf"]["pruefung"] = {
        "auftrag": "Pruefe das Ergebnis.", "model": "cli",
        "werkzeuge": [{"name": "Read", "parameter": {}}, {"name": "Write", "parameter": {}}],
        "ausgabe": {"modus": "text", "artefakte": [], "artefakte_optional": ["notizen"]},
        "max_turns": 4, "max_tokens": 50,
    }
    result = await rt.execute(ResearchRequest(**body), "tenant-a")
    assert result["status"] == "success", result.get("fehler_code")
    assert platz.auftraege[1].ausgaben == ["notizen"]
    assert [a["pfad"] for a in result["pruefung"]["artefakte"]] == ["notizen/n.md"]


async def test_cli_file_mode_collects_the_answer_file_through_the_place(tmp_path):
    """AOPTR P3-2: the answer file reaches the place's output list (ziele), so a
    place that returns only declared outputs still delivers it."""
    platz = DeklarierterPlatz(strom("werkzeug"), {"bericht.md": b"Bericht\n",
                                                  "skripte/rechne.py": b"x"})
    rt, _ = cli_runtime(tmp_path, platz)
    body = cli_body()
    body["antwort_im_text"] = False
    body["agentischer_lauf"]["ausgabe"] = {"modus": "datei", "datei": "bericht.md",
                                           "artefakte": [], "artefakte_optional": ["skripte"]}
    result = await rt.execute(ResearchRequest(**body), "tenant-a")
    assert result["status"] == "success", result.get("fehler_code")
    assert set(platz.auftraege[0].ausgaben) == {"bericht.md", "skripte"}
    assert result["herkunft"]["antwort"]["pfad"] == "bericht.md"
    assert {a["pfad"] for a in result["artefakte"]} == {"bericht.md", "skripte/rechne.py"}
