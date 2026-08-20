"""
SSRF guard for the headless-Chromium HTML renderers (/convert-html-to-pdf,
/convert-html-to-screenshot).

Both endpoints hand attacker-reachable HTML straight to Playwright/Chromium
(`page.set_content(html, ...)`) with no restriction on what the page may then
fetch. Chromium happily follows <img>/<link>/<script>/fetch()/XHR/iframe
requests embedded in that HTML, so a caller (any app/attacker holding a
Bridge credential — this endpoint is user-facing, see
security-audit-live-findings-20260818.md L10c/B.4) can make the Bridge's own
render container fetch arbitrary internal URLs: the cloud metadata endpoint,
other containers, admin surfaces reachable only from the Tailnet the Bridge
sits on (see L13 — "flaches Member-Tailnet"). The rendered PNG/PDF is
returned to the caller, so this is not just probing — it can exfiltrate the
response of whatever internal resource it reached.

Fix: intercept every request Chromium makes for the page (`page.route`) and
allow only http(s) requests whose resolved IP is public (not
private/loopback/link-local/reserved/multicast), plus `data:`/`blob:` URIs
(inline images/fonts — common in generated report HTML, never a network
fetch). Everything else is aborted. DNS is resolved here in Python BEFORE
the decision — this closes the DNS-rebinding gap a naive scheme/hostname
allowlist would leave open (a public-looking hostname that resolves to
169.254.169.254 is exactly the attack this guard exists for).

Fail-closed: any error while resolving/classifying a URL is treated as
unsafe (abort) — a bug in this guard must never silently reopen the SSRF
hole it exists to close.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Iterable, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# No network fetch is involved for these — Chromium decodes them locally.
_ALWAYS_SAFE_SCHEMES = frozenset({"data", "blob", "about", "javascript"})

# Only these ever leave the container.
_NETWORK_SCHEMES = frozenset({"http", "https"})


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for a globally-routable address.

    Covers loopback (127.0.0.0/8, ::1), RFC1918 (10/8, 172.16/12, 192.168/16),
    link-local INCLUDING the cloud metadata address 169.254.169.254
    (169.254.0.0/16), unique-local IPv6 (fc00::/7), reserved, and multicast.
    IPv4-mapped IPv6 (::ffff:10.0.0.0 etc.) is unwrapped first so it cannot
    smuggle a private IPv4 target past an IPv6-only check.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_all(hostname: str) -> Iterable[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """All addresses a hostname resolves to (v4 + v6). Raises on resolution failure."""
    infos = socket.getaddrinfo(hostname, None)
    for family, _, _, _, sockaddr in infos:
        yield ipaddress.ip_address(sockaddr[0])


def is_ssrf_safe_url(url: str) -> bool:
    """True iff Chromium may fetch this URL.

    Public interface used both by the live page.route handler and by tests.
    """
    try:
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()

        if scheme in _ALWAYS_SAFE_SCHEMES:
            return True

        if scheme not in _NETWORK_SCHEMES:
            # file://, ftp://, chrome://, etc. — no legitimate report ever
            # needs the renderer to touch these.
            return False

        hostname = parts.hostname
        if not hostname:
            return False

        # A literal IP in the URL: classify it directly, no DNS involved.
        try:
            literal_ip = ipaddress.ip_address(hostname)
        except ValueError:
            literal_ip = None

        resolved = [literal_ip] if literal_ip is not None else list(_resolve_all(hostname))
        if not resolved:
            return False

        # ALL resolved addresses must be public — DNS can return multiple
        # records, and picking the "nicest" one to check would let an
        # attacker hide a private address among public-looking ones.
        return all(_is_public_ip(ip) for ip in resolved)
    except Exception as e:  # noqa: BLE001 — fail-closed: never let a guard bug become an open SSRF hole
        logger.warning("ssrf_guard: could not classify url %r (%s) — blocking", url, e)
        return False


def make_route_handler():
    """Playwright route handler: abort unsafe requests, let safe ones through.

    Returns an async callable suitable for `page.route("**/*", handler)`.
    Kept as a factory (not a bare module-level function) so tests can invoke
    the classification logic (`is_ssrf_safe_url`) without needing a live
    Playwright `route` object.
    """
    async def _handler(route) -> None:  # route: playwright.async_api.Route
        url = route.request.url
        if is_ssrf_safe_url(url):
            await route.continue_()
        else:
            logger.warning("ssrf_guard: blocked request to %r", url)
            await route.abort()

    return _handler
