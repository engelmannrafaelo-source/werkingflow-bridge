# Privacy & Document Service

Standalone FastAPI app that runs as **Container 4** in the Bridge stack. Holds
all heavy NLP / OCR dependencies (Presidio, spaCy, Docling, PyTorch, LibreOffice)
in one image so the lightweight worker containers stay small. Internal-only —
not exposed to the public internet; reachable from workers via the Docker
network at `http://privacy-service:8100`.

## Endpoints

| Path | Purpose |
|------|---------|
| `POST /anonymize` | Bulk message anonymisation (Presidio) |
| `POST /deanonymize` | Reverse a Presidio mapping on a single string |
| `POST /smart-anonymize` | Presidio + local Flair NER, deterministic (no cloud calls) |
| `POST /convert-pdf` | Legacy PDF → Markdown via Docling (kept for back-compat) |
| `POST /convert-pdf-to-semantic-html` | PDF → semantic Flexbox HTML via ConvertAPI + AI |
| `POST /convert-semantic-html` | Pixel HTML → semantic Flexbox HTML |
| `POST /document/convert` | **Universal Markdown converter** (PDF/DOCX/PPTX/XLSX/CSV/HTML/MSG/EML/image) |
| `POST /document/convert-and-anonymize` | **Atomic** convert + smart-anonymize in one call |
| `GET /health` / `GET /ready` / `GET /status` | Liveness, drain readiness, Presidio status |

## Universal Document Conversion

`POST /document/convert` auto-routes uploads by MIME type or filename
extension and returns a single shape regardless of source format:

```json
{
  "success": true,
  "format": "xlsx",
  "markdown": "## Sheet1\n\n| ... |\n",
  "metadata": {
    "filename": "report.xlsx",
    "original_size_bytes": 12345,
    "sheet_count": 2,
    "sheets": [{ "name": "Sheet1", "rows": 100, "columns": [...] }]
  },
  "conversion_time_seconds": 0.42
}
```

### Adapter chain

The endpoint is backed by an explicit `AdapterChain` (`adapters.py`). Each
adapter implements a uniform interface:

```python
class BaseAdapter(ABC):
    name: str
    def can_handle(self, fmt: str, mime: str | None, filename: str) -> bool: ...
    def convert(self, content: bytes, filename: str, mime: str | None) -> ConversionResult: ...
```

The chain walks adapters in order; the first one whose `can_handle()` returns
true gets to convert. Deterministic adapters come first (cheap, free) and the
`AiFallbackAdapter` is the catch-all last entry that delegates to the Bridge's
own `/v1/chat/completions` endpoint for exotic formats none of the
deterministic adapters can parse:

| Order | Adapter pipeline | When it runs |
|-------|------------------|--------------|
| 1 | `PdfAdapter` — Docling (OCR + tables) | `.pdf` / `application/pdf`. Returns base64 PNG images alongside Markdown |
| 2 | `DocxAdapter` — LibreOffice → PDF → Docling | `.docx` / `.doc` |
| 3 | `PptxAdapter` — LibreOffice → PDF → Docling | `.pptx` / `.ppt` (best-effort for complex layouts) |
| 4 | `XlsxAdapter` — openpyxl + pandas | `.xlsx` / `.xls`. One `## SheetName` heading per sheet, truncates above 5000 rows |
| 5 | `CsvAdapter` — pandas (auto delimiter + encoding sniffing) | `.csv` (truncates above 10000 rows) |
| 6 | `HtmlAdapter` — markdownify (ATX headings) | `.html` / `.htm` |
| 7 | `MsgAdapter` — extract-msg | `.msg` (Outlook). Extracts headers + body + attachment list |
| 8 | `EmlAdapter` — stdlib `email` | `.eml` (RFC822). Falls back to markdownify for HTML-only mails |
| 9 | `ImageAdapter` — Docling vision OCR | `.png` / `.jpg` / `.tif` / `.webp` |
| 10 | `AiFallbackAdapter` — Bridge self-call (Claude) | Anything else, or any deterministic adapter that fails |

**AI fallback (`AiFallbackAdapter`)**

When the chain reaches the catch-all, the adapter:
1. Tries to text-decode the bytes (`utf-8` / `utf-16` / `cp1252` / `latin-1`).
2. Truncates to `MAX_TEXT_CHARS` (60 000).
3. Posts a Markdown-conversion prompt to `BRIDGE_SELF_URL` (default
   `http://localhost:8000/v1/chat/completions`) with `API_KEY` as bearer if set.
4. If Claude returns the literal token `<<UNCONVERTIBLE>>` or the bytes are
   binary (>5 % control chars after decoding), the adapter raises
   `AdapterError` and the chain surfaces an `HTTP 415`.
5. On success the response carries `metadata.adapter = "ai-fallback"` and
   `metadata.warning = "converted via AI fallback - quality may vary"` so
   callers can flag the result.

Standard formats never burn AI tokens — only files that none of the
deterministic adapters can handle reach the fallback. Set
`DISABLE_AI_FALLBACK=1` (or pass `enable_ai_fallback=False` to
`build_default_chain`) to force pure fail-loud behaviour, e.g. in tests.

**Fail-loud behaviour:**
- Unknown extension/MIME with no AI fallback (or AI declares the file
  unconvertible) ⇒ HTTP `415 Unsupported Media Type`.
- Empty upload ⇒ HTTP `400`.
- File above 100 MB ⇒ HTTP `413`.
- Adapter failures ⇒ HTTP `500` with the underlying exception message.
- No silent format fallbacks within the deterministic tier — the chain only
  cascades to the next adapter when the current one explicitly raises.

### Atomic convert + anonymize

`POST /document/convert-and-anonymize` returns the smart-anonymised Markdown
and the Presidio mapping in a single response so caller apps can persist the
mapping themselves. The Bridge **does not** keep the mapping — that ownership
stays with the calling app to avoid cross-tenant leakage.

```json
{
  "success": true,
  "format": "pdf",
  "anonymized_markdown": "Hallo ANON_PERSON_001 ...",
  "mapping": { "ANON_PERSON_001": "Anna Müller" },
  "detected_entities": [...],
  "restored_entities": [...],
  "metadata": { ... },
  "privacy_mode": "smart",
  "language": "de",
  "convert_time_seconds": 1.2,
  "total_time_seconds": 4.6
}
```

Multipart fields:

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `file` | yes | — | Binary upload |
| `mime_type_hint` | no | — | Override extension-based detection |
| `language` | no | `de` | `de` / `en` |
| `privacy_mode` | no | `smart` | `smart` (Presidio + local Flair NER) or `basic` (Presidio only) |
| `context_hint` | no | — | Document type hint to bias Smart-Anonymize decisions |

## Container

Build:

```bash
docker build -f docker/Dockerfile.privacy-pdf -t wt-privacy-pdf-service .
```

The image installs `libreoffice-{core,writer,impress,calc}` for Office
conversion and the Python extras `privacy + pdf + documents` from
`pyproject.toml` so all adapters are available out of the box.

Smoke test once the container is up:

```bash
curl http://localhost:8100/health
curl -F file=@sample.csv http://localhost:8100/document/convert
curl -F file=@sample.docx http://localhost:8100/document/convert
```

## Tests

```bash
poetry run pytest tests/unit/test_document_converter.py -v
```

Tests for adapters that need optional deps (openpyxl, pandas, extract-msg,
markdownify) skip cleanly when the dep is missing, so the suite runs both
inside the privacy-pdf container and in a minimal base checkout.
