"""BRRES x CACHE2 (laeuft wie test_cache_boundary_cli.py nur in eigener pytest-Sitzung gruen, siehe dort): ``antwort_im_text`` und ``cache_boundary`` im selben Aufruf.

Mit Textvertrag wird kein OUTPUT_FILE_PATH in den Prompt geschrieben, der Prompt
bleibt unveraendert und die Cache-Grenze gilt. Ohne Textvertrag schreibt die
Dateisuche den Prompt um und die Grenze wird laut verworfen (Gegenprobe).
"""

import pytest

from tests.unit.test_cache_boundary import STABIL
from tests.unit.test_cache_boundary_cli import _sdk_input, cli, mock_session_mgr  # noqa: F401


@pytest.mark.asyncio
async def test_textvertrag_behaelt_cache_grenze(cli, mock_session_mgr, caplog):  # noqa: F811
    prompt = "Human: " + STABIL + "\nAufgabe: fasse zusammen."
    with caplog.at_level("ERROR"):
        got = await _sdk_input(
            cli, prompt=prompt, cache_boundary=10,
            enable_file_discovery=True, allowed_tools=["Write"],
            antwort_im_text=True,
        )
    (msg,) = got["stream"]
    content = msg["message"]["content"]
    assert isinstance(content, list) and len(content) == 2
    assert content[0]["cache_control"] == {"type": "ephemeral"}
    assert content[0]["text"] + content[1]["text"] == prompt
    assert "running WITHOUT cache boundary" not in caplog.text


@pytest.mark.asyncio
async def test_dateivertrag_verwirft_grenze_laut(cli, mock_session_mgr, caplog):  # noqa: F811
    with caplog.at_level("ERROR"):
        got = await _sdk_input(
            cli, prompt="Human: " + STABIL, cache_boundary=10,
            enable_file_discovery=True, allowed_tools=["Write"],
            antwort_im_text=False,
        )
    content = got["stream"][0]["message"]["content"] if "stream" in got else got["argv"]
    assert isinstance(content, str)
    assert "running WITHOUT cache boundary" in caplog.text
