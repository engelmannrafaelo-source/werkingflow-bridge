"""ASGI-Huelle um ``src.routing.bild_policy``: prueft den Koerper, bevor ein Endpunkt ihn sieht.

Pure ASGI (nicht BaseHTTPMiddleware): streamingsicher, der Koerper wird nur fuer
Aufrufer gepuffert, die unter der Policy stehen, und danach unveraendert an den
Endpunkt weitergereicht. Alle anderen Apps gehen ohne jede Berührung durch.

Aufruferzuordnung: ein Principal, dessen Token genau EINE App benennt, gewinnt vor
der selbst erklaerten ``X-App-ID`` (ein Aufrufer mit Energy-Token kann der Policy
also nicht entgehen, indem er sich anders nennt); sonst gilt die Kopfzeile.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.routing import bild_policy

logger = logging.getLogger(__name__)


#: Kopfzeilen, die genau einmal vorkommen duerfen. Starlette/FastAPI liest bei Doppelten den ERSTEN,
#: ein dict aus der Liste behielte den LETZTEN: wer Policy und Parser verschieden liest, wird umgangen
#: (Codex R4: doppelter Content-Type). Mehrdeutig = 400, bevor irgendetwas geparst wird.
_SINGLETON = frozenset({
    "content-type", "content-length", "content-encoding", "transfer-encoding",
    "x-app-id", "x-client-id", "authorization", "x-bridge-origin",
})


def _headers(scope: Scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def ambiguity(scope: Scope) -> Optional[str]:
    """Grund, wenn der Request mehrdeutig ist; sonst None."""
    seen: dict[str, str] = {}
    for k, v in scope.get("headers", []):
        name, value = k.decode("latin-1").lower(), v.decode("latin-1")
        if name in _SINGLETON and name in seen:
            return f"Kopfzeile {name} mehrfach"
        seen[name] = value
    enc = seen.get("content-encoding", "").strip().lower()
    if enc and enc != "identity":
        return f"Content-Encoding {enc!r} wird nicht angenommen (Koerper nicht pruefbar)"
    if "transfer-encoding" in seen and "content-length" in seen:
        return "Content-Length zugleich mit Transfer-Encoding"
    if "content-length" in seen and not seen["content-length"].strip().isdigit():
        return "Content-Length ist keine Zahl"
    return None


def _app_from_client_id(client_id: Optional[str]) -> Optional[str]:
    if not client_id:
        return None
    parts = [p for p in client_id.strip().split("/") if p]
    if parts and parts[0] == "workflow":
        parts = parts[1:]
    return parts[0] if parts else None


async def _app_id(headers: dict[str, str]) -> Optional[str]:
    declared = headers.get("x-app-id") or _app_from_client_id(headers.get("x-client-id"))
    if os.getenv("BRIDGE_PRINCIPALS_ENABLED", "false").strip().lower() not in ("1", "true", "yes"):
        return declared
    auth = headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return declared
    try:
        from src.principals import resolve_principal_by_token

        principal = await resolve_principal_by_token(auth[7:].strip())
    except Exception as exc:  # noqa: BLE001 — die Auth-Schicht entscheidet ueber den Zugang, nicht diese
        logger.warning("bild-policy: Principal-Aufloesung fehlgeschlagen (%s) — Kopfzeile gilt", exc)
        return declared
    allowed = list(getattr(principal, "allowed_apps", []) or []) if principal is not None else []
    if principal is not None and not getattr(principal, "is_legacy", False) and len(allowed) == 1 and allowed[0] != "*":
        return allowed[0]
    return declared


def _reject(v: bild_policy.Violation) -> tuple[Message, Message]:
    body = json.dumps({"error": {
        "message": v.message, "type": "invalid_request_error", "code": v.code,
    }}).encode()
    return (
        {"type": "http.response.start", "status": v.status, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]},
        {"type": "http.response.body", "body": body},
    )


class ImagePolicyMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "").upper() not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return
        reason = ambiguity(scope)
        if reason is not None:
            logger.warning("⛔ bild-policy: mehrdeutiger Request (%s) %s %s", reason, scope["method"], scope.get("path"))
            start, end = _reject(bild_policy.Violation(400, "ambiguous_request", f"Mehrdeutiger Request abgewiesen: {reason}."))
            await send(start)
            await send(end)
            return
        headers = _headers(scope)
        app_id = await _app_id(headers)
        # Kontext fuer die Ausgangspruefung: "" = ohne Identitaet (gilt als eingeschraenkt).
        token = bild_policy.CURRENT_APP.set(app_id or "")
        try:
            if not bild_policy.is_restricted(app_id):
                await self.app(scope, receive, send)  # ausdruecklich erlaubte App: Koerper unberuehrt
                return
            await self._guarded(scope, receive, send, headers, app_id or "")
        finally:
            bild_policy.CURRENT_APP.reset(token)

    async def _guarded(self, scope: Scope, receive: Receive, send: Send, headers: dict[str, str], app_id: str) -> None:

        limit = bild_policy.max_body_bytes()
        chunks: list[bytes] = []
        size = 0
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                break  # frueh abweisen: ein endloser Sender haelt den Request nicht offen
            chunks.append(chunk)
            more = message.get("more_body", False)
        body = b"".join(chunks)

        verdict = (
            bild_policy.too_large(size) if size > limit
            else bild_policy.evaluate(
                app_id=app_id, method=scope["method"], path=scope.get("path", ""),
                content_type=headers.get("content-type", ""), body=body,
            )
        )
        if verdict is not None:
            logger.warning("⛔ bild-policy app=%s %s %s -> %s %s", app_id, scope["method"], scope.get("path"), verdict.status, verdict.code)
            start, end = _reject(verdict)
            await send(start)
            await send(end)
            return

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
