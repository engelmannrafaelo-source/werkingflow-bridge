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


class PinnedAccountLost(HTTPException):
    """The account a run is pinned to can no longer serve it (409). Raised only
    with ``pin``; never for a coordinator 409 or any other pool failure, so a
    caller can tell "pinned account gone" apart from every other refusal."""

    def __init__(self, detail: str):
        super().__init__(409, detail)


def _account_bound_400(response: httpx.Response) -> bool:
    """A 400 that depends on the ACCOUNT, not on the request: blocks carrying
    encrypted_content of another organisation, or an empty credit balance.
    Every other 400 is the request itself and is the same on every account."""
    if response.status_code != 400:
        return False
    text = response.text.lower()
    return "encrypted_content" in text or "credit balance" in text


async def post_messages(body: dict, timeout: float, pin: dict | None = None) -> tuple[httpx.Response, dict]:
    """``pin``: Zustand EINES Laufs (leeres dict). Der erste erfolgreiche Aufruf
    heftet das Konto an; jeder Folgeaufruf darf nur dieses Konto nehmen, weil
    encrypted_content (web_search_tool_result) an Organisation/Schluessel des
    erzeugenden Aufrufs gebunden ist. Ist das Konto nicht mehr verfuegbar oder
    lehnt es ab: PinnedAccountLost (409), laut, kein Wechsel auf ein anderes Konto.

    count_tokens mit ``pin``: Vor der Anheftung darf ein kontogebundener 400
    (_account_bound_400) aufs naechste Konto gehen; es gibt noch keinen Block,
    der an ein Konto gebunden waere. Nach der Anheftung ist jeder kontogebundene
    400 des angehefteten Kontos ein laut gemeldeter PinnedAccountLost. Ohne
    ``pin`` (Vision, Lueckenpruefung) bleibt alles wie bisher: der 400 geht
    unveraendert zurueck."""
    if body.get("stream") or body.get("speed") or body.get("inference_geo") or body.get("container"):
        raise HTTPException(400, "Credit pool supports standard non-streaming text/vision requests")
    server_steps = 0
    for tool in body.get("tools", []):
        if "type" not in tool:
            continue  # Client schemas execute no provider-side tool.
        if tool["type"] not in ("web_search_20250305", "web_fetch_20250910"):
            raise HTTPException(400, "Unpriced server tool; promotional_only stopped")
        uses = tool.get("max_uses")
        if type(uses) is not int or not 1 <= uses <= 3:
            raise HTTPException(400, "Server tools require max_uses between 1 and 3")
        server_steps += uses
    accounts = (await coordinator("GET", "/accounts"))["accounts"]
    pinned = (pin or {}).get("account_id")
    if pinned:
        accounts = [a for a in accounts if a["id"] == pinned]
        if not accounts:
            raise PinnedAccountLost(f"pinned credit account {pinned} unavailable; encrypted_content of this run "
                                    "is bound to it, no switch to another account")
    async with httpx.AsyncClient(timeout=timeout) as client:
        for account in accounts:
            headers = {"x-api-key": account["api_key"], "anthropic-version": VERSION}
            counted = {k: body[k] for k in ("model", "messages", "system", "thinking", "tools", "tool_choice") if k in body}
            try:
                count = await client.post(API + "/count_tokens", headers=headers, json=counted)
            except httpx.HTTPError as exc:
                raise HTTPException(503, "Anthropic token count unavailable; no credit reserved") from exc
            if count.status_code in (401, 403, 429):
                continue
            if count.status_code != 200:
                if pin is not None and _account_bound_400(count):
                    if pinned:
                        raise PinnedAccountLost(
                            f"pinned credit account {pinned} rejected count_tokens (HTTP 400, account-bound: "
                            "encrypted_content/credit balance); no switch to another account")
                    continue
                return count, {}
            tokens = count.json().get("input_tokens")
            if type(tokens) is not int or tokens < 0:
                raise HTTPException(502, "Anthropic returned an invalid input token count")
            reservation = str(uuid.uuid4())
            try:
                await coordinator("POST", "/reserve", {
                    "id": reservation, "account_id": account["id"], "grant_id": account["grant_id"],
                    "model": body["model"],
                    # Reserve whole context windows for internal tool turns,
                    # plus output ceilings and search fees conservatively as
                    # token budget. No extra fee column / idempotency gap.
                    "input_tokens": max(tokens, 2_000_000 * (server_steps + 1)) if server_steps else tokens,
                    "max_tokens": body["max_tokens"] * (server_steps + 1),
                })
            except HTTPException as exc:
                if exc.status_code in (409, 429):
                    continue
                raise
            # No retry after a transport failure or 5xx: it may already have
            # generated a paid answer. Keep the reservation, fail visibly.
            response = await client.post(API, headers=headers, json=body)
            meta = {"credit_pool_account": account["id"], "credit_pool_org": account["org_id"],
                    "credit_pool_grant": account["grant_id"], "credit_pool_reservation": reservation,
                    "funding_policy": "promotional_only", "automatic_paid_fallback": False}
            if response.status_code == 200:
                if pin is not None:
                    pin.setdefault("account_id", account["id"])
                try:
                    usage = response.json()["usage"]
                except (ValueError, KeyError, TypeError):
                    log.error("Credit reservation %s: unreadable paid response; retained", reservation)
                else:
                    await _finish(reservation, usage=usage)
                return response, meta
            if 400 <= response.status_code < 500:
                await _finish(reservation, rejected=True)
                if pinned and response.status_code == 400 and "encrypted_content" in response.text.lower():
                    raise PinnedAccountLost(
                        f"pinned credit account {pinned} rejected encrypted_content of its own run (HTTP 400); "
                        "no switch to another account")
                exhausted = response.status_code == 400 and "credit balance" in response.text.lower()
                if exhausted or response.status_code in (401, 403, 429):
                    continue
            return response, meta
    if pinned:
        raise PinnedAccountLost(f"pinned credit account {pinned} refused (count/reserve/limit); no switch to "
                                "another account because of encrypted_content")
    raise HTTPException(429, "No eligible Anthropic API credit budget available; paid fallback disabled")
