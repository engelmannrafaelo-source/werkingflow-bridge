"""
SSRF guard for the headless-Chromium HTML renderers.

Guards security-audit-live-findings-20260818.md L10c/B.4: caller-controlled
HTML rendered by /convert-html-to-pdf and /convert-html-to-screenshot could
make the render container fetch internal URLs (cloud metadata, other
containers on the Tailnet) and leak the response back in the PNG/PDF.

These tests exercise the pure classification logic (`is_ssrf_safe_url`) and
the Playwright route-handler wrapper (`make_route_handler`) against a fake
Route object, so no real Chromium/Playwright install is required.
"""
from __future__ import annotations

import asyncio
import socket
from unittest.mock import patch

import pytest

from src.privacy_service.ssrf_guard import is_ssrf_safe_url, make_route_handler


def _addrinfo(*ips: str):
    """Build a socket.getaddrinfo()-shaped result list for the given IPs."""
    out = []
    for ip in ips:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        out.append((family, socket.SOCK_STREAM, 6, "", (ip, 0) if family == socket.AF_INET else (ip, 0, 0, 0)))
    return out


class TestNonNetworkSchemes:
    """Legitimate report HTML routinely inlines images/fonts as data: URIs."""

    def test_data_uri_is_safe(self):
        assert is_ssrf_safe_url("data:image/png;base64,iVBORw0KGgo=") is True

    def test_blob_uri_is_safe(self):
        assert is_ssrf_safe_url("blob:https://example.com/uuid") is True

    def test_file_scheme_is_unsafe(self):
        """Chromium can read local files via file:// — never legitimate for report HTML."""
        assert is_ssrf_safe_url("file:///etc/passwd") is False

    def test_ftp_scheme_is_unsafe(self):
        assert is_ssrf_safe_url("ftp://example.com/x") is False

    def test_no_hostname_is_unsafe(self):
        assert is_ssrf_safe_url("http:///no-host") is False

    def test_malformed_url_is_unsafe(self):
        assert is_ssrf_safe_url("not a url at all \x00") is False


class TestLiteralIpAddresses:
    """URLs that name an IP directly — no DNS resolution involved."""

    def test_public_ipv4_is_safe(self):
        assert is_ssrf_safe_url("https://8.8.8.8/x") is True

    def test_loopback_ipv4_is_unsafe(self):
        assert is_ssrf_safe_url("http://127.0.0.1:8000/admin") is False

    def test_rfc1918_10_is_unsafe(self):
        assert is_ssrf_safe_url("http://10.0.0.5/") is False

    def test_rfc1918_172_is_unsafe(self):
        assert is_ssrf_safe_url("http://172.16.5.5/") is False

    def test_rfc1918_192_168_is_unsafe(self):
        assert is_ssrf_safe_url("http://192.168.1.1/") is False

    def test_cloud_metadata_link_local_is_unsafe(self):
        """169.254.169.254 — the AWS/GCP/Azure instance-metadata address."""
        assert is_ssrf_safe_url("http://169.254.169.254/latest/meta-data/") is False

    def test_ipv6_loopback_is_unsafe(self):
        assert is_ssrf_safe_url("http://[::1]/") is False

    def test_ipv6_unique_local_is_unsafe(self):
        assert is_ssrf_safe_url("http://[fc00::1]/") is False

    def test_ipv4_mapped_ipv6_private_is_unsafe(self):
        """::ffff:10.0.0.1 must not smuggle a private IPv4 past an IPv6-only check."""
        assert is_ssrf_safe_url("http://[::ffff:10.0.0.1]/") is False

    def test_ipv4_mapped_ipv6_public_is_safe(self):
        assert is_ssrf_safe_url("http://[::ffff:8.8.8.8]/") is True


class TestHostnameResolution:
    """DNS-backed hostnames — resolution is mocked for determinism."""

    def test_hostname_resolving_public_is_safe(self):
        with patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            assert is_ssrf_safe_url("https://example.com/report.png") is True

    def test_hostname_resolving_private_is_unsafe(self):
        """The DNS-rebinding case this guard exists to close: a public-looking
        hostname that resolves to an internal address."""
        with patch("socket.getaddrinfo", return_value=_addrinfo("10.1.2.3")):
            assert is_ssrf_safe_url("https://attacker-controlled.example/x") is False

    def test_hostname_resolving_mixed_is_unsafe(self):
        """One private record among public ones must still block — picking
        the 'nicest' resolved address would defeat the guard."""
        with patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34", "127.0.0.1")):
            assert is_ssrf_safe_url("https://mixed.example/x") is False

    def test_dns_resolution_failure_is_unsafe(self):
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("no such host")):
            assert is_ssrf_safe_url("https://nonexistent.invalid/x") is False


class TestLegitimateReportAssets:
    """The behaviour report/energy screenshot rendering actually depends on:
    public HTTPS images/fonts/CSS must keep working unmodified."""

    def test_https_cdn_image_is_safe(self):
        with patch("socket.getaddrinfo", return_value=_addrinfo("151.101.1.1")):
            assert is_ssrf_safe_url("https://cdn.example.com/logo.png") is True

    def test_https_s3_asset_is_safe(self):
        with patch("socket.getaddrinfo", return_value=_addrinfo("52.1.2.3")):
            assert is_ssrf_safe_url("https://fsn1.your-objectstorage.com/bucket/key.png") is True

    def test_google_fonts_is_safe(self):
        with patch("socket.getaddrinfo", return_value=_addrinfo("142.250.1.1")):
            assert is_ssrf_safe_url("https://fonts.gstatic.com/s/font.woff2") is True


# ---------------------------------------------------------------------------
# Route-handler wrapper — fake Playwright Route/Request, no real browser
# ---------------------------------------------------------------------------

class _FakeRequest:
    def __init__(self, url: str):
        self.url = url


class _FakeRoute:
    def __init__(self, url: str):
        self.request = _FakeRequest(url)
        self.continued = False
        self.aborted = False

    async def continue_(self):
        self.continued = True

    async def abort(self):
        self.aborted = True


class TestRouteHandler:
    def test_safe_request_is_continued(self):
        handler = make_route_handler()
        route = _FakeRoute("data:image/png;base64,abc")
        asyncio.run(handler(route))
        assert route.continued is True
        assert route.aborted is False

    def test_unsafe_request_is_aborted(self):
        handler = make_route_handler()
        route = _FakeRoute("http://169.254.169.254/latest/meta-data/")
        asyncio.run(handler(route))
        assert route.aborted is True
        assert route.continued is False

    def test_unsafe_hostname_request_is_aborted(self):
        handler = make_route_handler()
        with patch("socket.getaddrinfo", return_value=_addrinfo("10.0.0.1")):
            route = _FakeRoute("http://internal-service.local/admin")
            asyncio.run(handler(route))
        assert route.aborted is True
        assert route.continued is False
