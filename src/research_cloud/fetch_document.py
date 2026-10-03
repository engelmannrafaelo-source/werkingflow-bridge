"""fetch_document: public documents (PDF & Co.) as page-numbered text.

Rafael 03.10.2026 (Bühne b-perplexity-pool-wirkt-teilweise-20261002, answer a).
Measured on the pool path (Dev, 02.10.2026 19:46Z): Perplexity named a
manufacturer datasheet PDF, the CLI's WebFetch returned nothing readable from
it, the model wrote "type not confirmed" and moved on. The same PDF carries a
real text layer — the bridge's own Docling conversion (privacy service,
``/document/convert``) reads every characteristics table from it in ~8 s.
So this tool does not bring a new converter: it downloads the document and
hands it to that existing conversion path.

Rules, same as perplexity_search:
- fail-loud in the RESULT: every refusal names its reason (too big, not a
  document, no text layer, converter down, ...) — never an empty text;
- fail-soft for the run: a failing call is an error tool_result, the model
  continues (second source);
- a budget caps the calls; calls/errors are counted for the ledger.

Only public http(s) URLs: hosts that resolve to private, loopback, link-local
or shared (CGNAT/Tailscale) addresses are refused, on every redirect hop —
the worker sits inside the bridge network next to the privacy service.

Offered together with perplexity_search (RESEARCH_PERPLEXITY_ENABLED): the
two belong together — Perplexity finds the datasheet, this tool reads it.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

FETCH_DOCUMENT_TOOL_NAME = "fetch_document"

MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 60.0
CONVERT_TIMEOUT_SECONDS = 300.0
MAX_REDIRECTS = 5
# Text handed back to the model per call. A 12-page datasheet converts to
# ~20k characters; whole catalogues need a page range or a search term.
MAX_RESULT_CHARS = 40_000
# Lines of context around each search hit when the converter delivered no
# page split (the hit is then quoted in place, not by page).
HIT_CONTEXT_LINES = 6

# Document types the privacy service's /document/convert understands.
_DOCUMENT_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}
_IMAGE_MARKDOWN = re.compile(r"^!\[[^\]]*\]\([^)]*\)\s*$", re.MULTILINE)

FETCH_DOCUMENT_TOOL: Dict[str, Any] = {
    "name": FETCH_DOCUMENT_TOOL_NAME,
    "description": (
        "Lädt ein öffentliches Dokument (PDF, DOCX, PPTX, XLSX) von einer URL und liefert seinen Text "
        "mit Seitenzahlen — für Datenblätter, Broschüren, Kataloge, Normauszüge und Merkblätter, die "
        "web_fetch nicht lesbar liefert. Optional `seiten` (z. B. \"3-5\" oder \"2,7\") oder `suchbegriff` "
        "(z. B. eine Typbezeichnung), um bei langen Dokumenten nur die passenden Seiten zu bekommen.\n"
        "Zitiere Kennwerte mit Dokument-URL und Seite. Liefert das Werkzeug einen Fehler (kein Textinhalt, "
        "zu groß, nicht erreichbar) oder steht der gesuchte Typ nicht im Dokument, suchst du eine zweite "
        "Fundstelle desselben Dokuments oder derselben Angabe (Herstellerseite, andere Händler- oder "
        "Katalogfassung), bevor du einen Wert als „nicht bestätigt“ führst."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Öffentliche http(s)-URL des Dokuments."},
            "seiten": {
                "type": "string",
                "description": "Optional: Seitenbereich, z. B. \"3-5\" oder \"2,7,9-10\".",
            },
            "suchbegriff": {
                "type": "string",
                "description": "Optional: nur Seiten bzw. Stellen, die diesen Begriff enthalten.",
            },
        },
        "required": ["url"],
    },
}


class DocumentFetchError(Exception):
    """One call failed for a NAMED reason. Becomes an error tool_result —
    the run continues, the model sees why."""


Converter = Callable[[bytes, str, str], Awaitable[Dict[str, Any]]]


@dataclass
class DocumentCounters:
    """Per-run budget and counters, shared by the pool and the cloud path."""

    max_uses: int
    calls: int = 0
    errors: int = 0

    def as_meta(self) -> Dict[str, Any]:
        return {
            "fetch_document_calls": self.calls,
            "fetch_document_errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _is_public_address(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def _check_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise DocumentFetchError(f"keine öffentliche http(s)-URL: {url!r}")
    host = parsed.hostname
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise DocumentFetchError(f"Host {host!r} nicht auflösbar: {e}") from e
    addrs = {info[4][0] for info in infos}
    blocked = sorted(a for a in addrs if not _is_public_address(a))
    if not addrs or blocked:
        raise DocumentFetchError(f"Host {host!r} zeigt auf keine öffentliche Adresse ({blocked or 'keine'}) — abgelehnt")


async def download_document(
    url: str,
    client: httpx.AsyncClient,
    *,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
    check_url: Callable[[str], Awaitable[None]] = _check_public_url,
) -> Tuple[bytes, str, str]:
    """Return (content, content_type, final_url). Redirects are followed by
    hand so every hop passes the public-address check."""
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        await check_url(current)
        try:
            async with client.stream(
                "GET", current, timeout=timeout, follow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0 (compatible; werkingflow-research/1.0)", "Accept": "*/*"},
            ) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        raise DocumentFetchError(f"HTTP {resp.status_code} ohne Location-Header")
                    current = urljoin(current, location)
                    continue
                if resp.status_code != 200:
                    raise DocumentFetchError(f"HTTP {resp.status_code} beim Abruf von {current}")
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise DocumentFetchError(
                        f"Dokument zu groß ({int(declared) // 1024 // 1024} MB, Grenze {max_bytes // 1024 // 1024} MB)"
                    )
                chunks: List[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise DocumentFetchError(
                            f"Dokument zu groß (über {max_bytes // 1024 // 1024} MB) — Abruf abgebrochen"
                        )
                    chunks.append(chunk)
                content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                return b"".join(chunks), content_type, current
        except httpx.TimeoutException as e:
            raise DocumentFetchError(f"Zeitgrenze {timeout:.0f} s beim Abruf überschritten") from e
        except httpx.HTTPError as e:
            raise DocumentFetchError(f"Abruf fehlgeschlagen: {type(e).__name__}: {e}") from e
    raise DocumentFetchError(f"mehr als {MAX_REDIRECTS} Weiterleitungen")


def detect_document_type(content: bytes, content_type: str, url: str) -> Tuple[str, str]:
    """Return (mime, extension). Magic bytes first — many servers send PDFs as
    application/octet-stream; a web page is refused with its reason."""
    if content.startswith(b"%PDF-"):
        return "application/pdf", "pdf"
    if content_type in _DOCUMENT_TYPES:
        return content_type, _DOCUMENT_TYPES[content_type]
    path = urlparse(url).path.lower()
    if content.startswith(b"PK"):
        for mime, ext in _DOCUMENT_TYPES.items():
            if path.endswith("." + ext):
                return mime, ext
    head = content[:512].lstrip().lower()
    if content_type.startswith("text/html") or head.startswith((b"<!doctype html", b"<html")):
        raise DocumentFetchError(
            "die URL liefert eine Webseite, kein Dokument — Webseiten mit web_fetch lesen; "
            "steht dort ein Link auf das PDF, diesen Link hier übergeben"
        )
    raise DocumentFetchError(f"kein unterstütztes Dokument (Content-Type {content_type or 'unbekannt'!r})")


# ---------------------------------------------------------------------------
# Conversion (existing path: privacy service /document/convert, Docling)
# ---------------------------------------------------------------------------

async def convert_via_privacy_service(content: bytes, filename: str, mime: str) -> Dict[str, Any]:
    from src.privacy_client import PrivacyServiceUnavailable, get_privacy_client, privacy_timeout

    client = get_privacy_client()
    try:
        resp = await client.post(
            "/document/convert",
            files={"file": (filename, content, mime)},
            timeout=privacy_timeout(CONVERT_TIMEOUT_SECONDS),
        )
    except PrivacyServiceUnavailable as e:
        raise DocumentFetchError(f"Dokumentkonvertierung nicht erreichbar: {e}") from e
    except httpx.TimeoutException as e:
        raise DocumentFetchError(f"Dokumentkonvertierung: Zeitgrenze {CONVERT_TIMEOUT_SECONDS:.0f} s überschritten") from e
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail") or resp.json().get("error")
        except Exception:
            detail = resp.text[:300]  # silent-ok: Fehlertext ist nur Beiwerk der benannten Meldung
        raise DocumentFetchError(f"Dokumentkonvertierung HTTP {resp.status_code}: {detail}")
    return resp.json()


@dataclass
class ConvertedDocument:
    pages: Optional[List[Tuple[int, str]]]  # None = converter gave no page split
    full_text: str
    page_count: Optional[int]


def _clean(markdown: str) -> str:
    # Image references are file names inside the converter's container —
    # useless to the model, and they eat the character budget.
    text = _IMAGE_MARKDOWN.sub("", markdown or "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_converted(body: Dict[str, Any]) -> ConvertedDocument:
    metadata = body.get("metadata") or {}
    page_count = metadata.get("pages") if isinstance(metadata.get("pages"), int) else None
    pages = None
    raw_pages = metadata.get("page_markdowns")
    if isinstance(raw_pages, list) and raw_pages:
        pages = [
            (int(p["page_no"]), _clean(p.get("markdown") or ""))
            for p in raw_pages
            if isinstance(p, dict) and isinstance(p.get("page_no"), int)
        ]
    full_text = _clean(body.get("markdown") or "")
    if not full_text and not any(t for _, t in pages or []):
        raise DocumentFetchError(
            "das Dokument enthält keinen lesbaren Text (vermutlich ein gescanntes Bild-PDF) — "
            "zweite Fundstelle suchen"
        )
    return ConvertedDocument(pages=pages, full_text=full_text, page_count=page_count)


# ---------------------------------------------------------------------------
# Selection + formatting
# ---------------------------------------------------------------------------

def parse_page_range(spec: str) -> List[int]:
    pages: List[int] = []
    for part in (spec or "").replace(" ", "").split(","):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise DocumentFetchError(f"Seitenangabe {spec!r} nicht lesbar — Form \"3-5\" oder \"2,7\"")
        start, end = int(m.group(1)), int(m.group(2) or m.group(1))
        if start < 1 or end < start:
            raise DocumentFetchError(f"Seitenangabe {spec!r} nicht lesbar — Form \"3-5\" oder \"2,7\"")
        pages.extend(range(start, end + 1))
    return sorted(set(pages))


def _hits_in_place(text: str, term: str) -> List[str]:
    lines = text.splitlines()
    needle = term.lower()
    spans: List[Tuple[int, int]] = []
    for i, line in enumerate(lines):
        if needle in line.lower():
            lo, hi = max(0, i - HIT_CONTEXT_LINES), min(len(lines), i + HIT_CONTEXT_LINES + 1)
            if spans and lo <= spans[-1][1]:
                spans[-1] = (spans[-1][0], hi)
            else:
                spans.append((lo, hi))
    return ["\n".join(lines[lo:hi]) for lo, hi in spans]


def render_document(
    doc: ConvertedDocument,
    url: str,
    *,
    seiten: Optional[str] = None,
    suchbegriff: Optional[str] = None,
    max_chars: int = MAX_RESULT_CHARS,
) -> str:
    term = (suchbegriff or "").strip()
    head = [f"Dokument: {url}"]
    if doc.page_count:
        head.append(f"Seiten gesamt: {doc.page_count}")
    blocks: List[str] = []

    if doc.pages is not None:
        selected = doc.pages
        if seiten:
            wanted = set(parse_page_range(seiten))
            selected = [(n, t) for n, t in selected if n in wanted]
            if not selected:
                raise DocumentFetchError(f"Seiten {seiten!r} gibt es in diesem Dokument nicht (Seiten gesamt: {doc.page_count})")
        if term:
            selected = [(n, t) for n, t in selected if term.lower() in t.lower()]
            if not selected:
                raise DocumentFetchError(
                    f"„{term}“ kommt im Dokument nicht vor — anderes Dokument oder zweite Fundstelle suchen"
                )
            head.append(f"Seiten mit „{term}“: {', '.join(str(n) for n, _ in selected)}")
        blocks = [f"--- Seite {n} ---\n{t}" for n, t in selected if t]
    else:
        head.append(
            "Seitenzuordnung: nicht verfügbar (der Konverter lieferte für dieses Dokument keine "
            "Seitentrennung) — zitiere mit Dokument-URL und Abschnittsüberschrift statt Seitenzahl."
        )
        if seiten:
            head.append(f"Seitenangabe {seiten!r} ist daher nicht anwendbar; geliefert wird das ganze Dokument.")
        if term:
            hits = _hits_in_place(doc.full_text, term)
            if not hits:
                raise DocumentFetchError(
                    f"„{term}“ kommt im Dokument nicht vor — anderes Dokument oder zweite Fundstelle suchen"
                )
            head.append(f"Fundstellen für „{term}“: {len(hits)}")
            blocks = [f"--- Fundstelle {i} ---\n{h}" for i, h in enumerate(hits, 1)]
        else:
            blocks = [doc.full_text]

    out = "\n".join(head) + "\n\n"
    used = len(out)
    kept = 0
    for block in blocks:
        if used + len(block) + 2 > max_chars:
            break
        out += block + "\n\n"
        used += len(block) + 2
        kept += 1
    if kept < len(blocks):
        if kept == 0:
            out += blocks[0][: max_chars - used] + "\n\n"
        out += (
            f"[GEKÜRZT: {len(blocks) - max(kept, 1)} von {len(blocks)} Abschnitten nicht enthalten "
            f"(Grenze {max_chars} Zeichen) — mit `seiten` oder `suchbegriff` gezielt nachladen.]"
        )
    return out.strip()


# ---------------------------------------------------------------------------
# One call
# ---------------------------------------------------------------------------

async def fetch_document(
    args: Dict[str, Any],
    counters: DocumentCounters,
    *,
    client: Optional[httpx.AsyncClient] = None,
    convert: Converter = convert_via_privacy_service,
    check_url: Callable[[str], Awaitable[None]] = _check_public_url,
) -> str:
    """Run one fetch_document call. Returns the text for the model; raises
    DocumentFetchError with a named reason (budget, download, conversion)."""
    url = str((args or {}).get("url") or "").strip()
    if not url:
        raise DocumentFetchError("fetch_document braucht den Parameter 'url'.")
    if counters.calls >= counters.max_uses:
        raise DocumentFetchError(
            f"Budget dieser Recherche erschöpft ({counters.max_uses} Aufrufe)."
        )
    counters.calls += 1
    try:
        if client is not None:
            content, content_type, final_url = await download_document(url, client, check_url=check_url)
        else:
            async with httpx.AsyncClient() as c:
                content, content_type, final_url = await download_document(url, c, check_url=check_url)
        mime, ext = detect_document_type(content, content_type, final_url)
        body = await convert(content, f"dokument.{ext}", mime)
        doc = parse_converted(body)
        text = render_document(
            doc, final_url, seiten=(args or {}).get("seiten"), suchbegriff=(args or {}).get("suchbegriff")
        )
    except DocumentFetchError as e:
        counters.errors += 1
        logger.warning(f"research: fetch_document {url} -> refused: {e}")
        raise
    logger.info(
        f"research: fetch_document {final_url} -> ok, {len(content)} bytes, "
        f"pages={'split' if doc.pages is not None else 'none'}, {len(text)} chars"
    )
    return text
