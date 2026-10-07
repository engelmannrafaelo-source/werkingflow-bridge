"""Synthetic reports only; never copy customer data into tests."""

import importlib.metadata
import importlib.util
import sys

import pytest

# Other test modules may already have installed SDK stubs during collection.
# Load the installed package under a private name, preserving their module cache.
_sdk_path = importlib.metadata.distribution("claude-code-sdk").locate_file(
    "claude_code_sdk/__init__.py"
)
_spec = importlib.util.spec_from_file_location("_erkunder_real_sdk", _sdk_path)
assert _spec is not None and _spec.loader is not None
real_sdk = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = real_sdk
_spec.loader.exec_module(real_sdk)


@pytest.fixture
def auftrag():
    return {
        "schema": "erkunder-auftrag/1",
        "bericht_id": "bericht-test-123",
        "gegenstand": "Testanlage",
        "datenstand": {
            "von": "2026-01-01",
            "bis": "2026-01-02",
            "heute": "2026-01-03",
        },
        "auftrag": None,
        "zweck": "Betriebsprüfung",
        "vorwissen_md": "Testwissen",
        "vertiefung_md": None,
        "dateien": [],
        "korrekturkreis": 1,
    }


@pytest.fixture(autouse=True)
def isolate_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_code_sdk", real_sdk)
