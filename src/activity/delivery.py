"""Did the answer actually reach the caller? — the delivery question, in one place.

Why this module exists (Befund 03.09.2026, 2,16 USD):
    A gateway error AFTER the model run produced no error row. The worker
    finished the call, wrote ``status=success`` into ``usage_events`` and only
    THEN returned the response — into a connection the gateway had already
    torn down. The caller saw a 504 and no answer; the ledger showed a
    successful, billed call. Every cost readout built on that ledger therefore
    counted *billed* calls as *delivered* ones, with no way to tell them apart
    (KOSTEN-VISION-SEPTEMBER-20260904.md, Abschnitt 8g).

    The money is genuinely spent either way — the model ran, we owe the
    provider. What was wrong was the claim that an answer was delivered.

Measured, not assumed (05.09.2026, uvicorn 0.32.1 / starlette 0.46.2 —
the versions inside wt-wrapper-worker*, see /root/projekte/local-storage/
ledger-delivery-repro):
    • client aborts after 2s, handler runs 8s  → probe says gone   (True)
    • nginx proxy_read_timeout 3s, handler 10s → probe says gone   (True),
      caller got 504 at 3s
    • normal completion                        → probe says here   (False)
    Both gateway shapes end the same way at the worker's socket: nginx closes
    the upstream connection (``proxy_ignore_client_abort`` is off by default,
    and a read timeout closes it too), so one probe covers both.

What it deliberately does NOT do:
    It does not prove delivery — only non-delivery. A caller that vanishes in
    the milliseconds between the probe and the last byte leaving the socket is
    still booked as success; the send-failure latch below makes that case
    LOUD (ERROR log naming the affected ledger rows) but does not rewrite the
    row. Closing that last window needs a settle-after-send protocol on top of
    the write-ahead spool — a much deeper change to the money path, deliberately
    not made here.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Optional

import anyio
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# usage_events.status vocabulary (docker/migrations/060). Distinct from
# 'error' on purpose: an 'error' call cost nothing (the model never produced
# anything), an 'undelivered' call cost the full amount and produced an answer
# that nobody received. Collapsing the two would either hide the spend or
# invent a failed model run.
STATUS_UNDELIVERED = "undelivered"

# usage_events.error_code for that status — what exactly was wrong.
ERROR_CODE_CALLER_GONE = "caller_gone"

UNDELIVERED_MESSAGE = (
    "Modelllauf abgeschlossen und abgerechnet, aber der Aufrufer war beim "
    "Buchen nicht mehr verbunden (Gateway-Timeout oder Client-Abbruch) — "
    "die Antwort wurde nie zugestellt."
)


class DeliveryProbe:
    """Per-request answer to "is the caller still there?".

    Holds the raw ASGI ``receive`` rather than a Starlette ``Request`` so the
    ledger writer can ask the question without any call-site threading a
    request object through five layers of helper.
    """

    __slots__ = (
        "_receive", "_gone", "_booked_call_uids", "_send_failure",
        "_response_completed",
    )

    def __init__(self, receive: Receive) -> None:
        self._receive = receive
        self._gone = False
        self._booked_call_uids: list = []
        self._send_failure: Optional[str] = None
        self._response_completed = False

    def mark_response_completed(self) -> None:
        self._response_completed = True

    async def caller_gone(self) -> bool:
        """True once the client/gateway connection is known to be gone.

        Non-blocking and latching: the ASGI ``http.disconnect`` message is
        consumed at most once, so a second caller (e.g. the streaming
        disconnect monitor) cannot make this answer flap back to False.
        """
        if self._response_completed:
            # Our answer is already out of the door. uvicorn returns
            # ``http.disconnect`` for every receive() after that point, which
            # would otherwise read as "the caller left" — and mislabel work that
            # merely OUTLIVES the request. The async research dispatch is
            # exactly that shape: it answers 202 immediately and books its
            # ledger row minutes later, from a task that inherited this
            # context. Whatever such a task delivers, it does not deliver it
            # through this response.
            return False
        if self._gone:
            return True
        try:
            message: Message = {}
            # Already-cancelled scope = "take the message if one is waiting,
            # otherwise move on" — the same trick starlette's
            # Request.is_disconnected uses.
            with anyio.CancelScope() as cs:
                cs.cancel()
                message = await self._receive()
            if message.get("type") == "http.disconnect":
                self._gone = True
        except Exception as e:  # noqa: BLE001 — a probe must never break a call
            logger.debug("delivery probe unavailable (treating caller as present): %s", e)
        return self._gone

    def note_booked(self, call_uid: str) -> None:
        """Remember which ledger rows this request booked, so a send failure
        after the fact can name them instead of reporting an anonymous loss."""
        self._booked_call_uids.append(call_uid)

    def mark_send_failure(self, reason: str) -> None:
        self._send_failure = reason

    @property
    def send_failure(self) -> Optional[str]:
        return self._send_failure

    @property
    def booked_call_uids(self) -> list:
        return list(self._booked_call_uids)


_probe: ContextVar[Optional[DeliveryProbe]] = ContextVar(
    "bridge_delivery_probe", default=None
)


def get_delivery_probe() -> Optional[DeliveryProbe]:
    return _probe.get()


async def caller_gone() -> bool:
    """Module-level convenience for the ledger writer.

    No probe (spool replay, background job, unit test) → False. That is the
    honest default: outside a request there is no caller who could be gone,
    and guessing "gone" would invent undelivered calls.
    """
    probe = _probe.get()
    if probe is None:
        return False
    return await probe.caller_gone()


def detach() -> None:
    """Drop the inherited probe in work that OUTLIVES its request.

    ``asyncio.create_task`` copies the current context, so a background task
    spawned inside a handler inherits that request's probe — and would then ask
    a finished exchange whether its caller is still there. ``caller_gone()``
    already refuses to answer once the response completed; calling this at the
    top of a detached runner says the same thing out loud, at the place where
    the detachment happens.
    """
    _probe.set(None)


class DeliveryProbeMiddleware:
    """Pure-ASGI: install one DeliveryProbe per HTTP request and watch the send
    side of it.

    Pure ASGI (not BaseHTTPMiddleware) for the same reason as
    PerformanceMonitorMiddleware: it must not buffer or break streaming.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        probe = DeliveryProbe(receive)
        _probe.set(probe)

        async def watched_send(message: Message) -> None:
            try:
                await send(message)
            except Exception as e:  # noqa: BLE001 — re-raised below, just observed
                probe.mark_send_failure(f"{type(e).__name__}: {e}")
                raise
            if (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
            ):
                probe.mark_response_completed()

        try:
            await self.app(scope, receive, watched_send)
        finally:
            failure = probe.send_failure
            if failure:
                booked = probe.booked_call_uids
                # ERROR, not WARNING: rows exist that claim an answer was
                # delivered, and this is the only place that knows better.
                # Named uids make the correction a query, not an archaeology
                # project.
                logger.error(
                    "delivery: response to %s %s could not be sent (%s) — "
                    "%d ledger row(s) booked during this request claim a "
                    "delivered answer%s",
                    scope.get("method"), scope.get("path"), failure,
                    len(booked),
                    f": {', '.join(booked)}" if booked else "",
                )
