"""Direct Messages API credits, shared by both bridges through ONE coordinator.

Never exported as ANTHROPIC_API_KEY: the CLI subscription path stays separate.
Only non-streaming, tool-free Messages requests are supported here. A lost
response keeps its full reservation; no speculative release or paid fallback.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import httpx
from fastapi import HTTPException

log = logging.getLogger(__name__)
API = "https://api.anthropic.com/v1/messages"
VERSION = "2023-06-01"


def enabled() -> bool:
    return bool(os.getenv("ANTHROPIC_CREDIT_POOL_URL"))


async def coordinator(method: str, path: str, data: dict | None = None) -> dict:
    url = os.getenv("ANTHROPIC_CREDIT_POOL_URL", "").rstrip("/")
    token = os.getenv("ANTHROPIC_CREDIT_POOL_TOKEN", "")
    if not url or not token:
        raise HTTPException(503, "Anthropic credit coordinator is not configured")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.request(method, url + "/v1/internal/api-credit-pool" + path,
                                     headers={"X-Credit-Pool-Token": token}, json=data)
        if r.status_code != 200:
            # Never log the body: /accounts carries API keys.
            raise HTTPException(r.status_code if r.status_code in (409, 429) else 503,
                                "Anthropic credit coordinator rejected request")
        return r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "Anthropic credit coordinator unavailable") from exc


async def _finish(reservation: str, **data: Any) -> None:
    try:
        await coordinator("POST", "/settle", {"id": reservation, **data})
    except Exception:
        # A conservative pending debit survives process/network loss. Never
        # discard a paid successful response just because accounting is down.
        log.error("Credit reservation %s remains charged at its upper bound", reservation)


async def post_messages(body: dict, timeout: float) -> tuple[httpx.Response, dict]:
    if body.get("tools") or body.get("stream") or body.get("speed") or body.get("inference_geo"):
        raise HTTPException(400, "Credit pool supports standard non-streaming text/vision requests")
    accounts = (await coordinator("GET", "/accounts"))["accounts"]
    async with httpx.AsyncClient(timeout=timeout) as client:
        for account in accounts:
            headers = {"x-api-key": account["api_key"], "anthropic-version": VERSION}
            counted = {k: body[k] for k in ("model", "messages", "system", "thinking") if k in body}
            try:
                count = await client.post(API + "/count_tokens", headers=headers, json=counted)
            except httpx.HTTPError as exc:
                raise HTTPException(503, "Anthropic token count unavailable; no credit reserved") from exc
            if count.status_code in (401, 403, 429):
                continue
            if count.status_code != 200:
                return count, {}
            tokens = count.json().get("input_tokens")
            if type(tokens) is not int or tokens < 0:
                raise HTTPException(502, "Anthropic returned an invalid input token count")
            reservation = str(uuid.uuid4())
            try:
                await coordinator("POST", "/reserve", {
                    "id": reservation, "account_id": account["id"], "grant_id": account["grant_id"],
                    "model": body["model"], "input_tokens": tokens, "max_tokens": body["max_tokens"],
                })
            except HTTPException as exc:
                if exc.status_code in (409, 429):
                    continue
                raise
            # No retry after a transport failure or 5xx: it may already have
            # generated a paid answer. Keep the reservation, fail visibly.
            response = await client.post(API, headers=headers, json=body)
            meta = {"credit_pool_account": account["id"], "credit_pool_org": account["org_id"],
                    "credit_pool_grant": account["grant_id"], "credit_pool_reservation": reservation}
            if response.status_code == 200:
                try:
                    usage = response.json()["usage"]
                except (ValueError, KeyError, TypeError):
                    log.error("Credit reservation %s: unreadable paid response; retained", reservation)
                else:
                    await _finish(reservation, usage=usage)
                return response, meta
            if 400 <= response.status_code < 500:
                await _finish(reservation, rejected=True)
                exhausted = response.status_code == 400 and "credit balance" in response.text.lower()
                if exhausted or response.status_code in (401, 403, 429):
                    continue
            return response, meta
    raise HTTPException(429, "No eligible Anthropic API credit budget available; paid fallback disabled")
