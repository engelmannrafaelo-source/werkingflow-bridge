"""/v1/convert-pdf surfaces the privacy service's status and reason.

It used to call ``raise_for_status()`` and answer HTTP 200
``{"status":"error","error":"PDF conversion failed: Client error '413 …'"}`` —
status and the service's reason (page cap, render budget, misconfiguration)
were lost. /v1/document/* already passed them through 1:1.
"""

import sys
from unittest.mock import MagicMock as _MM

for _mod in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
    "src.identity.routes",
    "src.db.client",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = _MM()

import json  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

import src.main  # noqa: E402
from src.privacy_client import PrivacyServiceClient  # noqa: E402


def _request():
    req = AsyncMock()
    req.form = AsyncMock(return_value=MagicMock(
        get=MagicMock(return_value=MagicMock(filename="a.pdf", read=AsyncMock(return_value=b"%PDF-1"))),
    ))
    req.headers = {}
    return req


async def _call(downstream: httpx.Response):
    client = PrivacyServiceClient()
    inner = AsyncMock()
    inner.request = AsyncMock(return_value=downstream)
    inner.is_closed = False
    client._client = inner
    with patch("src.main.get_privacy_client", return_value=client), \
         patch("src.main.verify_api_key", new=AsyncMock()), \
         patch("src.main.extract_attribution_context", return_value={
             "app_id": "werking-report", "user_id": None, "workflow_id": None,
             "session_id": None, "job_id": None,
         }):
        return await src.main.convert_pdf_endpoint(request=_request(), credentials=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,error",
    [
        (413, "PDF hat 700 Seiten, die Obergrenze dieses Dienstes ist 600 Seiten (DOCLING_MAX_PAGES)."),
        (500, "Konfigurationsfehler des Dienstes: DOCLING_BATCH_PAGES=0 muss mindestens 1 sein"),
    ],
)
async def test_service_error_status_and_reason_pass_through(status, error):
    downstream = httpx.Response(
        status, json={"status": "error", "error": error},
        request=httpx.Request("POST", "http://privacy/convert-pdf"),
    )
    resp = await _call(downstream)
    assert resp.status_code == status
    assert json.loads(resp.body) == {"status": "error", "error": error}


@pytest.mark.asyncio
async def test_success_is_unchanged():
    downstream = httpx.Response(
        200, json={"status": "success", "markdown": "# x", "pages": 1},
        request=httpx.Request("POST", "http://privacy/convert-pdf"),
    )
    resp = await _call(downstream)
    assert resp.status == "success" and resp.markdown == "# x" and resp.pages == 1
