"""tests/sdk_strom.py stands in for claude_code_sdk's message dataclasses.

The SDK only lives in the worker image; where it is installed, the stand-ins
must have the same fields, or every fake built on them tests a stream shape
the SDK never sends (BR2R2 M1': SystemMessage converted to {'subtype', 'data'}
was read as an unfinished result). The SDK's types.py is loaded by path under
its own name: test modules replace claude_code_sdk in sys.modules with a
MagicMock, and the package __init__ needs mcp.
"""
from __future__ import annotations

import dataclasses
import importlib.machinery
import importlib.metadata
import importlib.util
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock as _MagicMock

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

import pytest  # noqa: E402

from tests import sdk_strom  # noqa: E402

KLASSEN = ("TextBlock", "AssistantMessage", "SystemMessage", "ResultMessage")


def _gelockte_sdk_version() -> str:
    lock = (Path(__file__).resolve().parents[2] / "poetry.lock").read_text()
    treffer = re.search(
        r'name = "claude-code-sdk"\nversion = "([^"]+)"', lock)
    assert treffer, "claude-code-sdk not found in poetry.lock"
    return treffer.group(1)


def test_stand_ins_haben_die_felder_des_sdk():
    """Against the locked version (the one the worker image runs) the fields
    must be identical. Another installed version may only ADD fields with a
    default (0.0.25: AssistantMessage.parent_tool_use_id) — the stand-ins
    then still build what that SDK builds, minus keys the bridge never reads."""
    paket = importlib.machinery.PathFinder.find_spec("claude_code_sdk")
    if paket is None:
        pytest.skip("claude_code_sdk not installed here (worker image only)")
    spec = importlib.util.spec_from_file_location(
        "_claude_code_sdk_types_echt", Path(paket.origin).parent / "types.py")
    echt = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = echt  # dataclasses resolve their module while being built
    try:
        spec.loader.exec_module(echt)
    finally:
        del sys.modules[spec.name]
    installiert = importlib.metadata.version("claude-code-sdk")
    gelockt = _gelockte_sdk_version()
    for k in KLASSEN:
        stand_in = [f.name for f in dataclasses.fields(getattr(sdk_strom, k))]
        felder = dataclasses.fields(getattr(echt, k))
        if installiert == gelockt:
            assert stand_in == [f.name for f in felder], (k, gelockt)
            continue
        assert stand_in == [f.name for f in felder][:len(stand_in)], (
            k, installiert, gelockt)
        zusaetzlich = felder[len(stand_in):]
        ohne_default = [f.name for f in zusaetzlich
                        if f.default is dataclasses.MISSING
                        and f.default_factory is dataclasses.MISSING]
        assert not ohne_default, (k, installiert, gelockt, ohne_default)


def test_umgewandelte_formen():
    init = sdk_strom.chunk(sdk_strom.init())
    assert set(init) == {"subtype", "data"}
    ende = sdk_strom.chunk(sdk_strom.result())
    assert "type" not in ende and {"is_error", "num_turns", "subtype"} <= set(ende)
