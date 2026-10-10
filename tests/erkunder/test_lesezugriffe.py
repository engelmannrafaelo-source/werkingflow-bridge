"""BR19: Eingangsordner pruefwissen/ und Lesezugriffe je Schritt (synthetisch)."""

import sys
import types

import conftest
import pytest
from pydantic import ValidationError

from src.erkunder import kind
from src.erkunder.kind import lesezugriff
from src.erkunder.models import Auftrag, Ergebnis, Schritt

CWD = "/arbeit/bericht-test-123/erkunder-1"
EINGANG = "/arbeit/bericht-test-123/eingang"


def datei(ziel):
    return {
        "ziel": ziel,
        "url": "https://example.test/x",
        "sha256": "a" * 64,
        "bytes": 1,
    }


@pytest.mark.parametrize(
    "ziel",
    [
        "pruefwissen/kw-thema-waermepumpe.md",
        "pruefwissen/a",
        "pruefwissen/" + "a" * 200,
        "messdaten/x.parquet",
        "unterlagen/vertrag.txt",
        "plan/schema.pdf",
    ],
)
def test_gueltige_eingangspfade(auftrag, ziel):
    auftrag["dateien"] = [datei(ziel)]
    assert Auftrag.model_validate(auftrag).dateien[0].ziel == ziel


@pytest.mark.parametrize(
    "ziel",
    [
        "pruefwissen/",
        "pruefwissen",
        "pruefwissen/a/b",
        "pruefwissen/..",
        "pruefwissen/..x",
        "pruefwissen/a\\b",
        "pruefwissen/a\x00",
        "pruefwissen/a\n",
        "messdaten/a\n",
        "pruefwissen/a\tb",
        "unterlagen/a\x7f",
        "pruefwissen/" + "a" * 201,
        "/pruefwissen/a",
        "./pruefwissen/a",
        "Pruefwissen/a",
        "pruefwisse/a",
        "pruefwissenx/a",
        "xpruefwissen/a",
        "prüfwissen/a",
        "bibliothek/a",
    ],
)
def test_ungueltige_eingangspfade(auftrag, ziel):
    auftrag["dateien"] = [datei(ziel)]
    with pytest.raises(ValidationError):
        Auftrag.model_validate(auftrag)


def test_vierzig_dokumente_im_auftrag(auftrag):
    """Der Vertrag selbst setzt keine Anzahl- oder Summengrenze fuer 25-40 Dokumente."""
    auftrag["dateien"] = [datei(f"pruefwissen/kw-thema-{i:02d}.md") for i in range(40)]
    auftrag["dateien"][0]["bytes"] = 650_000
    assert len(Auftrag.model_validate(auftrag).dateien) == 40


def schritt(**changes):
    data = {
        "name": "erkunder-1",
        "versuch": 1,
        "status": "ok",
        "abbruch_grund": None,
        "dauer_s": 1.0,
        "zuege": 2,
        "ram_spitze_mb": 1.0,
        "worker": "w",
        "tokens": {"input": 1, "output": 1, "cache_read": 0, "cache_creation": 0},
    }
    data.update(changes)
    return data


def test_schritt_ohne_erhebung_hat_kein_feld():
    assert "lesezugriffe" not in Schritt.model_validate(schritt()).model_dump()


def test_schritt_erhoben_ohne_zugriff_ist_leere_liste():
    dump = Schritt.model_validate(schritt(lesezugriffe=[])).model_dump()
    assert dump["lesezugriffe"] == []


@pytest.mark.parametrize(
    "eintrag",
    [
        {"werkzeug": "write", "pfad": "a"},
        {"werkzeug": "Read", "pfad": "a"},
        {"werkzeug": "read", "pfad": ""},
        {"werkzeug": "read"},
        {"werkzeug": "read", "pfad": "a", "inhalt": "x"},
    ],
)
def test_ungueltiger_lesezugriff(eintrag):
    with pytest.raises(ValidationError):
        Schritt.model_validate(schritt(lesezugriffe=[eintrag]))


def test_ergebnis_rundweg_mit_und_ohne_erhebung():
    meta = {
        "schema": "erkunder-ergebnis/1",
        "bericht_id": "bericht-test-123",
        "prompt_version": "erkunder-prompts/3",
        "modell": "claude-sonnet-5-5",
        "schritte": [
            schritt(lesezugriffe=[{"werkzeug": "read", "pfad": "pruefwissen/a.md"}]),
            schritt(name="erkunder-2"),
        ],
        "erkunder_ausgefallen": [],
        "korrekturkreis_gelaufen": False,
        "offene_befunde_anzahl": 0,
        "pruefstatus": "widerspruchsfrei",
        "korrekturrunden": 0,
    }
    dump = Ergebnis.model_validate(meta).model_dump(by_alias=True)
    assert dump["schritte"][0]["lesezugriffe"] == [
        {"werkzeug": "read", "pfad": "pruefwissen/a.md"}
    ]
    assert "lesezugriffe" not in dump["schritte"][1]
    assert Ergebnis.model_validate(dump).model_dump(by_alias=True) == dump


@pytest.mark.parametrize(
    ("name", "eingabe", "erwartet"),
    [
        (
            "Read",
            {"file_path": f"{EINGANG}/pruefwissen/kw-a.md"},
            [("read", "pruefwissen/kw-a.md")],
        ),
        (
            "Read",
            {"file_path": "../eingang/unterlagen/u.txt"},
            [("read", "unterlagen/u.txt")],
        ),
        (
            "Read",
            {"file_path": f"{EINGANG}/../eingang/vorwissen.md"},
            [("read", "vorwissen.md")],
        ),
        ("Read", {"file_path": "ergebnis.md"}, []),
        ("Read", {"file_path": f"{EINGANG}x/a"}, []),
        ("Read", {"file_path": "/arbeit/anderer-bericht/eingang/a"}, []),
        ("Read", {"file_path": 7}, []),
        (
            "Grep",
            {"pattern": "Takt", "path": f"{EINGANG}/pruefwissen"},
            [("grep", "pruefwissen")],
        ),
        ("Grep", {"pattern": "Takt", "path": "../eingang"}, [("grep", ".")]),
        ("Grep", {"pattern": "Takt"}, []),
        (
            "Glob",
            {"pattern": "pruefwissen/*.md", "path": EINGANG},
            [("glob", "pruefwissen/*.md")],
        ),
        ("Glob", {"pattern": "../eingang/**/*.md"}, [("glob", "**/*.md")]),
        ("Glob", {"pattern": f"{EINGANG}/plan/*"}, [("glob", "plan/*")]),
        ("Write", {"file_path": f"{EINGANG}/x"}, []),
        ("Edit", {"file_path": f"{EINGANG}/x"}, []),
        ("LS", {"path": EINGANG}, []),
        ("Read", "kein dict", []),
        (
            "Bash",
            {
                "command": "head -5 ../eingang/unterlagen/u.txt && "
                f"python -c \"open('{EINGANG}/pruefwissen/kw-a.md')\"; "
                "cat ../eingang/unterlagen/u.txt"
            },
            [("bash", "unterlagen/u.txt"), ("bash", "pruefwissen/kw-a.md")],
        ),
        ("Bash", {"command": "python skripte/rechnung.py"}, []),
        ("Bash", {"command": "ls eingang"}, []),
    ],
)
def test_lesezugriff(name, eingabe, erwartet):
    assert lesezugriff(name, eingabe, CWD) == [
        {"werkzeug": w, "pfad": p} for w, p in erwartet
    ]


def test_bash_eintrag_traegt_keinen_befehlstext():
    eintraege = lesezugriff(
        "Bash",
        {"command": "grep -i 'Kunde Mustermann' ../eingang/unterlagen/u.txt"},
        CWD,
    )
    assert eintraege == [{"werkzeug": "bash", "pfad": "unterlagen/u.txt"}]


@pytest.mark.asyncio
async def test_run_sammelt_aus_dem_query_strom(monkeypatch):
    sdk = conftest.real_sdk
    tool = sdk.ToolUseBlock

    class ResultMessage:
        is_error = False
        usage = {"input_tokens": 2, "output_tokens": 3}
        num_turns = 4

    class ClaudeCodeOptions:
        def __init__(self, **_kwargs):
            pass

    async def query(**_kwargs):
        yield sdk.SystemMessage(subtype="init", data={})
        yield sdk.AssistantMessage(
            content=[
                sdk.TextBlock(text="Ich lese das Prüfwissen."),
                tool(
                    id="t1",
                    name="Glob",
                    input={"pattern": "pruefwissen/*", "path": EINGANG},
                ),
                tool(
                    id="t2",
                    name="Read",
                    input={"file_path": f"{EINGANG}/pruefwissen/kw-a.md"},
                ),
            ],
            model="m",
        )
        yield sdk.UserMessage(
            content=[sdk.ToolResultBlock(tool_use_id="t2", content="x")]
        )
        yield None  # vom toleranten Parser uebersprungen
        yield sdk.AssistantMessage(
            content=[
                tool(
                    id="t3",
                    name="Write",
                    input={"file_path": "ergebnis.md", "content": "x"},
                ),
                tool(
                    id="t4",
                    name="Grep",
                    input={"pattern": "Takt", "path": "../eingang"},
                ),
            ],
            model="m",
        )
        yield ResultMessage()

    monkeypatch.setattr(kind, "install_resilient_parser", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "claude_code_sdk",
        types.SimpleNamespace(
            AssistantMessage=sdk.AssistantMessage,
            ToolUseBlock=sdk.ToolUseBlock,
            ClaudeCodeOptions=ClaudeCodeOptions,
            ResultMessage=ResultMessage,
            query=query,
        ),
    )
    monkeypatch.setattr(kind, "sdk_options", lambda body: None)
    output = await kind.run(
        {"ordner": CWD, "prompt": "synthetisch", "max_turns": 5, "claude_token": "x"}
    )
    assert output["zuege"] == 4
    assert output["lesezugriffe"] == [
        {"werkzeug": "glob", "pfad": "pruefwissen/*"},
        {"werkzeug": "read", "pfad": "pruefwissen/kw-a.md"},
        {"werkzeug": "grep", "pfad": "."},
    ]


@pytest.mark.asyncio
async def test_run_ohne_lesen_meldet_leere_liste(monkeypatch):
    """Erhoben, nichts gelesen: [] ist hier die wahre Antwort, kein fehlendes Feld."""

    class ResultMessage:
        is_error = False
        usage = {}
        num_turns = 1

    async def query(**_kwargs):
        yield ResultMessage()

    monkeypatch.setattr(kind, "install_resilient_parser", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "claude_code_sdk",
        types.SimpleNamespace(
            AssistantMessage=conftest.real_sdk.AssistantMessage,
            ToolUseBlock=conftest.real_sdk.ToolUseBlock,
            ResultMessage=ResultMessage,
            query=query,
        ),
    )
    monkeypatch.setattr(kind, "sdk_options", lambda body: None)
    output = await kind.run(
        {"ordner": CWD, "prompt": "p", "max_turns": 1, "claude_token": "x"}
    )
    assert output["lesezugriffe"] == []
