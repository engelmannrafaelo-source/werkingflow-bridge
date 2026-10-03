"""fetch_document (src/research_cloud/fetch_document.py) and its wiring into
the cloud executor and the pool path.

Download is mocked with httpx.MockTransport, the conversion (privacy service
/document/convert) with an injected converter. Fixtures are neutral on purpose:
no manufacturer, product, norm or customer names from any real case.
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

for _mod_name in ["claude_code_sdk", "claude_code_sdk._errors", "claude_code_sdk._internal",
                  "claude_code_sdk._internal.client"]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()

import src.research_pool_perplexity as rpp  # noqa: E402
from src.research_cloud.executor import _build_tools, run_research_cloud  # noqa: E402
from src.research_cloud.fetch_document import (  # noqa: E402
    DocumentCounters,
    DocumentFetchError,
    _check_public_url,
    fetch_document,
    parse_page_range,
)
from src.research_cloud.library import LibraryConfig  # noqa: E402
from src.research_cloud.models import ResearchCloudConfig  # noqa: E402
from src.research_cloud.perplexity import PerplexityConfig  # noqa: E402
from src.research_cloud.prompt import build_system_prompt  # noqa: E402

PDF = b"%PDF-1.4\n...binary..."
URL = "https://hersteller.example/datenblatt.pdf"


def _paged(*pages, count=None):
    return {
        "success": True,
        "markdown": "\n\n".join(pages),
        "metadata": {
            "pages": count or len(pages),
            "page_markdowns": [{"page_no": i + 1, "markdown": p} for i, p in enumerate(pages)],
        },
    }


def _flat(markdown, count=3):
    return {"success": True, "markdown": markdown, "metadata": {"pages": count}}


_PAGES = (
    "## Produktreihe X\n\n![Image](image_000000_abc.png)\n\nÜbersicht",
    "| Typ | Heizleistung kW |\n|---|---|\n| A-10 | 41,0 |\n| B-20 | 82,5 |",
    "## Schall\n\nSchallleistung 77 dB(A)",
)


def _transport(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        r = routes[str(request.url)]
        return r() if callable(r) else r
    return httpx.MockTransport(handler)


async def _ok(url):  # public-address check that needs no DNS
    return None


async def _call(args, body=None, routes=None, counters=None, convert=None):
    routes = routes or {URL: httpx.Response(200, content=PDF, headers={"content-type": "application/octet-stream"})}
    counters = counters or DocumentCounters(max_uses=5)
    async with httpx.AsyncClient(transport=_transport(routes)) as client:
        text = await fetch_document(
            args, counters, client=client,
            convert=convert or AsyncMock(return_value=body or _paged(*_PAGES)), check_url=_ok,
        )
    return text, counters


# --- reading ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pdf_by_magic_bytes_is_converted_with_page_numbers():
    convert = AsyncMock(return_value=_paged(*_PAGES))
    text, counters = await _call({"url": URL}, convert=convert)
    content, filename, mime = convert.await_args.args
    assert content == PDF and mime == "application/pdf" and filename.endswith(".pdf")
    assert "--- Seite 2 ---" in text and "| B-20 | 82,5 |" in text
    assert "Seiten gesamt: 3" in text and URL in text
    assert "image_000000" not in text  # converter-internal image refs are dropped
    assert counters.calls == 1 and counters.errors == 0


@pytest.mark.asyncio
async def test_search_term_keeps_only_matching_pages():
    text, _ = await _call({"url": URL, "suchbegriff": "b-20"})
    assert "Seiten mit „b-20“: 2" in text
    assert "--- Seite 2 ---" in text and "Seite 3" not in text


@pytest.mark.asyncio
async def test_search_term_missing_is_a_named_error_pointing_to_a_second_source():
    with pytest.raises(DocumentFetchError, match="zweite Fundstelle"):
        await _call({"url": URL, "suchbegriff": "Z-99"})


@pytest.mark.asyncio
async def test_page_range_selects_pages():
    text, _ = await _call({"url": URL, "seiten": "1,3"})
    assert "--- Seite 1 ---" in text and "--- Seite 3 ---" in text and "Seite 2" not in text


def test_page_range_parser():
    assert parse_page_range("3-5, 2") == [2, 3, 4, 5]
    for bad in ("drei", "5-3", "0"):
        with pytest.raises(DocumentFetchError, match="Seitenangabe"):
            parse_page_range(bad)


@pytest.mark.asyncio
async def test_without_page_split_says_so_and_quotes_hits_in_place():
    body = _flat("\n".join(_PAGES))
    text, _ = await _call({"url": URL, "suchbegriff": "Schallleistung", "seiten": "2"}, body=body)
    assert "Seitenzuordnung: nicht verfügbar" in text
    assert "nicht anwendbar" in text
    assert "--- Fundstelle 1 ---" in text and "77 dB(A)" in text


@pytest.mark.asyncio
async def test_long_document_is_cut_with_a_visible_marker():
    pages = [f"Seite {i} " + "x" * 15_000 for i in range(1, 6)]
    text, _ = await _call({"url": URL}, body=_paged(*pages))
    assert "[GEKÜRZT:" in text and "`seiten` oder `suchbegriff`" in text
    assert len(text) < 41_000


# --- named refusals (fail-loud in the result) --------------------------------

@pytest.mark.asyncio
async def test_scanned_pdf_without_text_names_the_reason():
    counters = DocumentCounters(max_uses=5)
    with pytest.raises(DocumentFetchError, match="keinen lesbaren Text"):
        await _call({"url": URL}, body=_paged("![Image](a.png)", "  "), counters=counters)
    assert counters.calls == 1 and counters.errors == 1


@pytest.mark.asyncio
async def test_web_page_is_refused_with_a_pointer_to_web_fetch():
    routes = {URL: httpx.Response(200, content=b"<!DOCTYPE html><html>..</html>", headers={"content-type": "text/html"})}
    with pytest.raises(DocumentFetchError, match="Webseite"):
        await _call({"url": URL}, routes=routes)


@pytest.mark.asyncio
async def test_http_error_is_named():
    with pytest.raises(DocumentFetchError, match="HTTP 404"):
        await _call({"url": URL}, routes={URL: httpx.Response(404)})


@pytest.mark.asyncio
async def test_declared_size_over_limit_is_refused_before_reading():
    big = httpx.Response(200, content=PDF, headers={"content-length": str(200 * 1024 * 1024)})
    with pytest.raises(DocumentFetchError, match="zu groß"):
        await _call({"url": URL}, routes={URL: big})


@pytest.mark.asyncio
async def test_converter_failure_is_named_and_counted():
    counters = DocumentCounters(max_uses=5)
    convert = AsyncMock(side_effect=DocumentFetchError("Dokumentkonvertierung HTTP 500: kaputt"))
    with pytest.raises(DocumentFetchError, match="HTTP 500"):
        await _call({"url": URL}, convert=convert, counters=counters)
    assert counters.errors == 1


@pytest.mark.asyncio
async def test_budget_refuses_without_downloading():
    counters = DocumentCounters(max_uses=1, calls=1)
    routes = {URL: lambda: pytest.fail("must not download")}
    with pytest.raises(DocumentFetchError, match="Budget"):
        await _call({"url": URL}, routes=routes, counters=counters)
    assert counters.calls == 1


@pytest.mark.asyncio
async def test_redirect_hops_are_each_checked():
    final = "https://cdn.example/datei.pdf"
    routes = {
        URL: httpx.Response(302, headers={"location": final}),
        final: httpx.Response(200, content=PDF),
    }
    checked = []

    async def check(u):
        checked.append(u)

    async with httpx.AsyncClient(transport=_transport(routes)) as client:
        text = await fetch_document({"url": URL}, DocumentCounters(max_uses=2), client=client,
                                    convert=AsyncMock(return_value=_paged(*_PAGES)), check_url=check)
    assert checked == [URL, final] and final in text


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x.pdf", "http://10.0.0.5/x.pdf", "http://169.254.169.254/latest",
    "http://100.65.1.1:8100/document/convert", "http://[::1]/x.pdf", "file:///etc/hosts",
])
async def test_internal_addresses_are_refused(url):
    with pytest.raises(DocumentFetchError):
        await _check_public_url(url)


# --- cloud path ---------------------------------------------------------------

_PPLX_ON = PerplexityConfig(enabled=True, api_key="pplx-test")


def _resp(body):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"model": "claude-sonnet-5", "usage": {"input_tokens": 1, "output_tokens": 1}, **body}
    return r


def test_cloud_tool_offered_only_with_perplexity():
    on = {t["name"] for t in _build_tools(ResearchCloudConfig(), LibraryConfig(), _PPLX_ON)}
    off = {t["name"] for t in _build_tools(ResearchCloudConfig(), LibraryConfig(), PerplexityConfig())}
    assert "fetch_document" in on and "fetch_document" not in off


@pytest.mark.asyncio
async def test_cloud_executor_answers_fetch_document_and_counts(monkeypatch):
    monkeypatch.setenv("BRIDGE_ANONYMIZE_ENABLED", "true")
    client = MagicMock()
    client.post = AsyncMock(side_effect=[
        _resp({"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "a", "name": "fetch_document", "input": {"url": URL}},
            {"type": "tool_use", "id": "b", "name": "fetch_document", "input": {"url": URL, "suchbegriff": "Z-99"}},
        ]}),
        _resp({"stop_reason": "end_turn", "content": [{"type": "text", "text": "bericht"}]}),
    ])
    routes = {URL: httpx.Response(200, content=PDF)}
    async with httpx.AsyncClient(transport=_transport(routes)) as dl:
        result = await run_research_cloud(
            "q", "s", api_key="sk-test", client=client, library_config=LibraryConfig(), library_index={},
            perplexity_config=_PPLX_ON, anonymize=AsyncMock(return_value="x"), perplexity_client=MagicMock(),
            fetch_document_kwargs={"client": dl, "convert": AsyncMock(return_value=_paged(*_PAGES)), "check_url": _ok},
        )
    assert result.fetch_document_calls == 2 and result.fetch_document_errors == 1
    sent = client.post.call_args_list[1].kwargs["json"]["messages"][-1]["content"]
    assert not sent[0].get("is_error") and "--- Seite 2 ---" in sent[0]["content"][0]["text"]
    assert sent[1]["is_error"] and "zweite Fundstelle" in sent[1]["content"][0]["text"]


def test_cloud_prompt_has_second_source_and_norm_gap_rules():
    p = build_system_prompt("deep", perplexity=True)
    assert "fetch_document" in p
    assert "Zweite Fundstelle statt Abbruch" in p
    gap = p.split("Normlücken zuerst über Perplexity", 1)[1]
    assert "perplexity_search" in gap and "Ausgabedatum" in gap and "Lücke bleibt erlaubt" in gap


# --- pool path ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_pool_handler_returns_text_and_raises_named_errors():
    tool = rpp.PoolDocumentTool(3, convert=AsyncMock(return_value=_paged(*_PAGES)), check_url=_ok,
                                client=httpx.AsyncClient(transport=_transport({URL: httpx.Response(200, content=PDF)})))
    out = await tool.handle({"url": URL})
    assert "--- Seite 1 ---" in out["content"][0]["text"]
    with pytest.raises(rpp.PoolToolError, match="fetch_document: .*Parameter 'url'"):
        await tool.handle({})
    assert tool.counters.as_meta() == {"fetch_document_calls": 1, "fetch_document_errors": 0}


def test_pool_server_is_always_loaded_under_its_own_name(monkeypatch):
    sdk = sys.modules["claude_code_sdk"]
    monkeypatch.setattr(sdk, "create_sdk_mcp_server",
                        MagicMock(return_value={"type": "sdk", "name": "dokument"}), raising=False)
    monkeypatch.setattr(sdk, "tool", MagicMock(return_value=lambda fn: fn), raising=False)
    cfg = rpp.PoolDocumentTool(3).server()
    assert cfg["alwaysLoad"] is True
    assert sdk.create_sdk_mcp_server.call_args.kwargs["name"] == rpp.DOC_MCP_SERVER_NAME
    desc = sdk.tool.call_args.args[1]
    assert "WebFetch" in desc and "web_fetch" not in desc


def test_pool_prompt_has_document_tool_second_source_and_norm_gap_rules():
    s = rpp.POOL_PROMPT_SECTION
    assert rpp.DOC_TOOL_NAME in s and "Seitenzahl" in s
    assert "Zweite Fundstelle statt Abbruch" in s
    gap = s.split("Normlücken zuerst über Perplexity", 1)[1]
    assert rpp.MCP_TOOL_NAME in gap and "Ausgabedatum" in gap

