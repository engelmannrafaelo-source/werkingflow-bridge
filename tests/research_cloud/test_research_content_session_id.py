"""BR3 hotfix (10.10.2026): GET /v1/research/{session_id}/content takes only
research session uuids.

The id went unchecked into ``INSTANCES_DIR.glob(f"*_{session_id}")``. With glob
syntax (``*``, ``?``, ``[0-7]*``, prefixes) any holder of a valid bridge key
could enumerate and read every research session on a worker's volume. Every
id that is not a uuid is now refused with 400 before the filesystem is
touched; uuids behave as before. Neutral fixtures.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock as _MagicMock

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
    "src.identity.routes",
    "src.db.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

from pathlib import Path  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import src.main  # noqa: E402

EIGENE = "3aeda720-8443-4379-951d-b35b8246a7c3"
FREMDE = "0b1c2d3e-4f50-4617-8899-aabbccddeeff"
FREMDER_BERICHT = "# Bericht einer anderen Sitzung\n"


def _sitzung(root: Path, session_id: str, text: str) -> None:
    docs = root / f"2026-10-10-0100_{session_id}" / "claudedocs"
    docs.mkdir(parents=True)
    (docs / "output.md").write_text(text, encoding="utf-8")


async def _content(session_id: str, root: Path, monkeypatch):
    monkeypatch.setenv("INSTANCES_DIR", str(root))
    with patch.object(src.main, "verify_api_key", new=AsyncMock()):
        return await src.main.get_research_content(session_id, MagicMock(), None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_id",
    [
        "*",
        "?" * 36,
        "[0-9a-f]*",
        "0*",
        "0b1c2d3e-*",
        "0b1c2d3e-4f50-4617-8899-aabbccddeef?",
        "0b1c2d3e-4f50-4617-8899-aabbccddeef[e]",
        f"{FREMDE}\n",
        "..",
        "",
    ],
)
async def test_keine_uuid_wird_abgewiesen_und_liest_nichts(
    session_id, tmp_path, monkeypatch
):
    _sitzung(tmp_path, FREMDE, FREMDER_BERICHT)
    with pytest.raises(HTTPException) as exc:
        await _content(session_id, tmp_path, monkeypatch)
    assert exc.value.status_code == 400
    assert exc.value.detail["reason"] == "research_session_id_malformed"
    assert FREMDER_BERICHT not in str(exc.value.detail)


@pytest.mark.asyncio
async def test_job_id_wird_abgewiesen_und_liest_nichts(tmp_path, monkeypatch):
    # Not a session uuid either; develop (BR2) answers it with its own 400
    # pointing at GET /v1/jobs/{id}, so only the status is pinned here.
    _sitzung(tmp_path, FREMDE, FREMDER_BERICHT)
    with pytest.raises(HTTPException) as exc:
        await _content("job_prod_c3e635a5", tmp_path, monkeypatch)
    assert exc.value.status_code == 400
    assert FREMDER_BERICHT not in str(exc.value.detail)


@pytest.mark.asyncio
async def test_glob_wird_gar_nicht_erst_ausgefuehrt(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTANCES_DIR", str(tmp_path))
    glob_erreicht = AssertionError("glob reached")
    with patch.object(src.main, "verify_api_key", new=AsyncMock()), \
            patch.object(src.main.Path, "glob", side_effect=glob_erreicht):
        with pytest.raises(HTTPException) as exc:
            await src.main.get_research_content("*", MagicMock(), None)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_eigene_uuid_liefert_eigenen_bericht(tmp_path, monkeypatch):
    _sitzung(tmp_path, FREMDE, FREMDER_BERICHT)
    _sitzung(tmp_path, EIGENE, "# Eigener Bericht\n")
    resp = await _content(EIGENE, tmp_path, monkeypatch)
    assert resp.status_code == 200
    assert resp.body.decode("utf-8") == "# Eigener Bericht\n"


@pytest.mark.asyncio
async def test_unbekannte_uuid_bleibt_404(tmp_path, monkeypatch):
    _sitzung(tmp_path, FREMDE, FREMDER_BERICHT)
    with pytest.raises(HTTPException) as exc:
        await _content(EIGENE, tmp_path, monkeypatch)
    assert exc.value.status_code == 404
