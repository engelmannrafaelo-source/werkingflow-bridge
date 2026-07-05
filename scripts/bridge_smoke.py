#!/usr/bin/env python3
"""bridge_smoke.py — functional per-endpoint smoke test for the WerkingFlow Bridge.

WHY THIS EXISTS
---------------
The Docker container healthcheck (and the old research-only smoke) only proved
*liveness*: "does the process answer /health?". That let broken endpoints ship
to production silently — most notably `/v1/document/convert` returning 415 on
every real PDF for weeks (Docling/onnxruntime OCR init crash) while /health,
/lb-status and the research smoke all stayed green. Same class of miss let the
PII passthrough on /v1/privacy/smart-anonymize survive for weeks.

This suite exercises each endpoint with a *real minimal payload* and asserts a
*correctness property*, not just HTTP 200. A broken endpoint fails the deploy
(bridge-deploy.sh routes a smoke failure into the existing auto-rollback path),
so the Bridge can no longer go live with a dead endpoint.

COVERAGE DISCIPLINE
-------------------
Every functional `/v1/*` route is either PROBED here or listed in EXCLUDED with
an explicit reason. `bridge_smoke_coverage.py` enforces that — a new endpoint
that is neither probed nor excluded fails the validator. No silent gaps.

USAGE
-----
    python3 scripts/bridge_smoke.py --base-url http://49.12.72.66:8000 --profile hetzner
    python3 scripts/bridge_smoke.py --base-url ... --profile server2
    python3 scripts/bridge_smoke.py --base-url ... --only document_convert --json

Auth: reads AI_BRIDGE_API_KEY from env, else Infisical dev-server/dev.
Exit 0 = all probed-for-this-profile passed. Non-zero = at least one failed.
The final line is machine-readable: `SMOKE_OK: ...` or `SMOKE_FAIL: ...`.
With --json a JSON per-probe result block is printed for the auto-fix hook.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PDF_FIXTURE = os.path.join(REPO, "tests", "fixtures", "smoke", "smoke_min.pdf")

# Attribution: deploy smokes are deliberate infra calls, not app traffic — book
# them to the anonymous bucket so they don't pollute the unattributed-leak metric.
SMOKE_HEADERS = {
    "X-Client-ID": "bridge-deploy/smoke-test",
    "X-User-ID": "anonymous:bridge-deploy-smoke",
}

# A short German PII string used to assert the anonymize *contract*.
PII_TEXT = "Herr Schmidt wohnt in Wien, Email max.schmidt@example.com, Tel 0176 1234567."
PII_MARKERS = ["max.schmidt@example.com", "0176 1234567"]  # must NOT appear verbatim in output


# ---------------------------------------------------------------------------
# Probe result + registry
# ---------------------------------------------------------------------------
@dataclass
class ProbeResult:
    name: str
    endpoint: str
    ok: bool
    detail: str
    http_status: Optional[int] = None
    elapsed_ms: Optional[int] = None


@dataclass
class Probe:
    name: str
    endpoint: str            # canonical /v1/... path (for coverage mapping)
    profiles: set            # which server profiles run this probe
    fn: Callable             # (ctx) -> ProbeResult
    repro: str = ""          # human repro command shown on failure / to fix-session


PROBES: list = []


def probe(name, endpoint, profiles, repro=""):
    def deco(fn):
        PROBES.append(Probe(name=name, endpoint=endpoint, profiles=set(profiles), fn=fn, repro=repro))
        return fn
    return deco


# EXCLUDED: endpoint -> reason. Enforced by the coverage validator. These are
# deliberately NOT smoke-tested; each needs a reason so the gap stays honest.
EXCLUDED = {
    # --- money / mutating billing state (must never fire on a deploy) ---
    "/v1/billing/mollie-webhook": "mutating payment webhook — firing it would create real billing state",
    "/credit-purchases": "mutating billing state",
    "/deduct": "mutating credit balance",
    "/project-pack/checkout": "starts a real payment flow",
    "/issue": "mutating license issuance",
    # --- identity / auth mutations ---
    "/register": "creates a real user",
    "/login": "auth flow, would need a throwaway credential + creates a session",
    "/logout": "session mutation",
    "/forgot-password": "sends a real reset email",
    "/resend-verification": "sends a real email",
    "/reset-password-with-token": "mutating, needs a live token",
    # --- stateful session/lease lifecycle ---
    "/v1/cli-sessions": "creates/leases a CLI session — stateful, cleaned up by lifecycle not smoke",
    "/v1/cli-sessions/{cli_session_id}": "per-session lookup, no stable id at deploy time",
    "/lease-token": "leases real capacity; heartbeat/attach/release are lifecycle-owned",
    "/conversations": "reads live conversation state, no stable id at deploy time",
    # --- needs a media fixture we do not ship yet (TODO: add audio fixture) ---
    "/v1/audio/transcriptions": "TODO: needs a small audio fixture; add probe once fixture committed",
    # --- debug / internal / free-form, low functional-regression risk ---
    "/v1/debug/request": "debug echo, no functional contract",
    "/v1/compatibility": "static capability descriptor",
    "/v1/providers": "static provider list (covered indirectly by chat/completions)",
    "/v1/models": "static model list (covered indirectly by chat/completions)",
    "/v1/auth/status": "auth-key echo, covered indirectly by every authed probe",
    "/v1/privacy/status": "flag echo, the real contract is asserted by the smart-anonymize probe",
    "/v1/usage/status": "read-only usage counters, covered indirectly by metrics probe",
    # --- convert-family variants: primary PDF path covered by document_convert;
    #     these are alternate in/out shapes. TODO: dedicated fixtures + probes. ---
    "/v1/convert-pdf": "alternate PDF-in path; digital-PDF regression covered by document_convert. TODO dedicated probe",
    "/v1/convert-pdf-to-html-direct": "PDF→HTML variant; PDF-in regression covered by document_convert. TODO dedicated probe",
    "/v1/convert-pdf-to-semantic-html": "PDF→semantic-HTML variant; PDF-in covered by document_convert. TODO dedicated probe",
    "/v1/convert-docx-to-html": "DOCX-in variant; TODO: needs a committed docx fixture to probe",
    # --- research sub-resources: need a live request/session id from /v1/research ---
    "/v1/research/async/{request_id}": "async-result lookup, needs a live request_id; parent /v1/research is probed",
    "/v1/research/{session_id}/content": "session-content lookup, needs a live session_id; parent /v1/research is probed",
    # --- stateful session lifecycle (same class as cli-sessions) ---
    "/v1/sessions": "stateful session lifecycle — creating one on a deploy would leak state",
    "/v1/sessions/stats": "aggregate read over session lifecycle state",
    "/v1/sessions/{session_id}": "per-session lookup, no stable id at deploy time",
    # --- agentic LLM endpoint: expensive, exercised indirectly ---
    "/v1/doc-agent": "agentic multi-turn LLM endpoint (expensive); LLM path covered by chat/completions + research",
}


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------
@dataclass
class Ctx:
    base_url: str
    api_key: str
    extra_header: dict = field(default_factory=dict)
    timeout: int = 120

    def headers(self, extra=None):
        h = dict(SMOKE_HEADERS)
        h["Authorization"] = f"Bearer {self.api_key}"
        h.update(self.extra_header)
        if extra:
            h.update(extra)
        return h


def _timed(fn):
    t0 = time.time()
    r = fn()
    return r, int((time.time() - t0) * 1000)


# ---------------------------------------------------------------------------
# Probes — each asserts a CORRECTNESS property, not just HTTP 200
# ---------------------------------------------------------------------------
@probe("research", "/v1/research", {"hetzner", "server2"},
       repro="curl -XPOST $AI_BRIDGE_URL/v1/research -H 'Authorization: Bearer $AI_BRIDGE_API_KEY' -H 'Content-Type: application/json' -d '{\"query\":\"smoke test\",\"depth\":\"quick\",\"max_turns\":5}'")
def _research(ctx: Ctx) -> ProbeResult:
    ep = "/v1/research"
    r, ms = _timed(lambda: requests.post(
        f"{ctx.base_url}{ep}", headers=ctx.headers({"Content-Type": "application/json"}),
        json={"query": "smoke test", "depth": "quick", "max_turns": 5}, timeout=ctx.timeout))
    if r.status_code != 200:
        return ProbeResult("research", ep, False, f"HTTP {r.status_code}: {r.text[:200]}", r.status_code, ms)
    d = r.json()
    if d.get("status") != "success":
        return ProbeResult("research", ep, False, f"status={d.get('status')!r} expected success", r.status_code, ms)
    if d.get("content", "").count("https://") < 1:
        return ProbeResult("research", ep, False, "content has 0 https:// URLs (research returned nothing)", r.status_code, ms)
    return ProbeResult("research", ep, True, f"status=success, exec={d.get('execution_time_seconds','?')}s", r.status_code, ms)


@probe("chat_completions", "/v1/chat/completions", {"hetzner", "server2"},
       repro="curl -XPOST $AI_BRIDGE_URL/v1/chat/completions -H 'Authorization: Bearer $AI_BRIDGE_API_KEY' -H 'Content-Type: application/json' -d '{\"model\":\"claude-haiku-4-5-20251001\",\"max_tokens\":8,\"messages\":[{\"role\":\"user\",\"content\":\"say OK\"}]}'")
def _chat(ctx: Ctx) -> ProbeResult:
    ep = "/v1/chat/completions"
    r, ms = _timed(lambda: requests.post(
        f"{ctx.base_url}{ep}", headers=ctx.headers({"Content-Type": "application/json"}),
        json={"model": "claude-haiku-4-5-20251001", "max_tokens": 8,
              "messages": [{"role": "user", "content": "Reply with the single word OK."}]},
        timeout=ctx.timeout))
    if r.status_code != 200:
        return ProbeResult("chat_completions", ep, False, f"HTTP {r.status_code}: {r.text[:200]}", r.status_code, ms)
    d = r.json()
    choices = d.get("choices") or []
    if not choices:
        return ProbeResult("chat_completions", ep, False, f"no choices in response: {str(d)[:200]}", r.status_code, ms)
    return ProbeResult("chat_completions", ep, True, "completion returned", r.status_code, ms)


@probe("document_convert", "/v1/document/convert", {"hetzner"},
       repro="curl -XPOST $AI_BRIDGE_URL/v1/document/convert -H 'Authorization: Bearer $AI_BRIDGE_API_KEY' -F file=@tests/fixtures/smoke/smoke_min.pdf")
def _doc_convert(ctx: Ctx) -> ProbeResult:
    """THE probe that would have caught the 415-on-every-PDF regression."""
    ep = "/v1/document/convert"
    if not os.path.exists(PDF_FIXTURE):
        return ProbeResult("document_convert", ep, False, f"fixture missing: {PDF_FIXTURE}", None, None)
    with open(PDF_FIXTURE, "rb") as fh:
        pdf = fh.read()
    r, ms = _timed(lambda: requests.post(
        f"{ctx.base_url}{ep}", headers=ctx.headers(),
        files={"file": ("smoke_min.pdf", pdf, "application/pdf")}, timeout=max(ctx.timeout, 180)))
    if r.status_code != 200:
        return ProbeResult("document_convert", ep, False,
                           f"HTTP {r.status_code}: {r.text[:200]}", r.status_code, ms)
    # Body may be JSON {status, markdown/text} or raw text — accept either, but
    # require non-trivial extracted text so an empty/failed convert can't pass.
    text = ""
    try:
        d = r.json()
        text = d.get("markdown") or d.get("text") or d.get("content") or ""
        if d.get("status") == "error":
            return ProbeResult("document_convert", ep, False, f"status=error: {str(d)[:200]}", r.status_code, ms)
    except ValueError:
        text = r.text
    if len(text.strip()) < 10:
        return ProbeResult("document_convert", ep, False,
                           f"extracted text too short ({len(text.strip())} chars) — convert likely failed", r.status_code, ms)
    if "fox" not in text.lower() and "smoke" not in text.lower():
        return ProbeResult("document_convert", ep, False,
                           f"extracted text missing expected fixture words: {text[:120]!r}", r.status_code, ms)
    return ProbeResult("document_convert", ep, True, f"extracted {len(text.strip())} chars", r.status_code, ms)


@probe("smart_anonymize", "/v1/privacy/smart-anonymize", {"hetzner"},
       repro="curl -XPOST $AI_BRIDGE_URL/v1/privacy/smart-anonymize -H 'Authorization: Bearer $AI_BRIDGE_API_KEY' -H 'Content-Type: application/json' -d '{\"text\":\"Herr Schmidt ... max.schmidt@example.com Tel 0176 1234567\",\"language\":\"de\"}'")
def _anonymize(ctx: Ctx) -> ProbeResult:
    """Contract: EITHER anonymize correctly OR fail loud (503).
    NEVER a silent 200-passthrough that echoes PII verbatim with entity_count:0.
    That silent-passthrough is exactly the GDPR miss that survived for weeks."""
    ep = "/v1/privacy/smart-anonymize"
    r, ms = _timed(lambda: requests.post(
        f"{ctx.base_url}{ep}", headers=ctx.headers({"Content-Type": "application/json"}),
        json={"text": PII_TEXT, "language": "de"}, timeout=ctx.timeout))
    # Fail-loud when the detector is intentionally disabled is ACCEPTABLE.
    if r.status_code == 503:
        return ProbeResult("smart_anonymize", ep, True, "fail-loud 503 (detector disabled) — acceptable, no silent passthrough", r.status_code, ms)
    if r.status_code != 200:
        return ProbeResult("smart_anonymize", ep, False, f"HTTP {r.status_code}: {r.text[:200]}", r.status_code, ms)
    d = r.json()
    out = d.get("smart_anonymized_text") or d.get("raw_anonymized_text") or ""
    performed = d.get("anonymization_performed")
    entity_count = d.get("smart_entity_count") or d.get("entity_count") or 0
    leaked = [m for m in PII_MARKERS if m in out]
    if leaked:
        return ProbeResult("smart_anonymize", ep, False,
                           f"SILENT PASSTHROUGH — PII verbatim in output: {leaked} (performed={performed}, entities={entity_count})",
                           r.status_code, ms)
    if not performed and entity_count == 0:
        return ProbeResult("smart_anonymize", ep, False,
                           "200 but anonymization_performed falsey AND entity_count=0 — no evidence detector ran",
                           r.status_code, ms)
    return ProbeResult("smart_anonymize", ep, True, f"anonymized (entities={entity_count}, no PII markers verbatim)", r.status_code, ms)


def _html_convert_probe(name, ep, magic, magic_desc):
    @probe(name, ep, {"hetzner"},
           repro=f"curl -XPOST $AI_BRIDGE_URL{ep} -H 'Authorization: Bearer $AI_BRIDGE_API_KEY' -H 'Content-Type: application/json' -d '{{\"html\":\"<h1>Smoke</h1><p>hello</p>\"}}'")
    def _fn(ctx: Ctx, _ep=ep, _name=name, _magic=magic, _desc=magic_desc) -> ProbeResult:
        r, ms = _timed(lambda: requests.post(
            f"{ctx.base_url}{_ep}", headers=ctx.headers({"Content-Type": "application/json"}),
            json={"html": "<h1>Smoke</h1><p>The quick brown fox.</p>"}, timeout=max(ctx.timeout, 180)))
        if r.status_code != 200:
            return ProbeResult(_name, _ep, False, f"HTTP {r.status_code}: {r.text[:200]}", r.status_code, ms)
        body = r.content
        # response may be raw binary or JSON with base64/url — accept if binary magic present
        # or JSON success shape.
        if body[:len(_magic)] == _magic:
            return ProbeResult(_name, _ep, True, f"returned {_desc} ({len(body)} bytes)", r.status_code, ms)
        try:
            d = r.json()
            if d.get("status") == "error":
                return ProbeResult(_name, _ep, False, f"status=error: {str(d)[:200]}", r.status_code, ms)
            if d.get("status") == "success" or d.get("url") or d.get("base64") or d.get("data"):
                return ProbeResult(_name, _ep, True, "success payload", r.status_code, ms)
            return ProbeResult(_name, _ep, False, f"200 but no {_desc} magic and no success shape: {str(d)[:150]}", r.status_code, ms)
        except ValueError:
            return ProbeResult(_name, _ep, False, f"200 but body is neither {_desc} nor JSON: {body[:60]!r}", r.status_code, ms)
    return _fn


_html_convert_probe("convert_html_to_pdf", "/v1/convert-html-to-pdf", b"%PDF", "PDF")
_html_convert_probe("convert_html_to_docx", "/v1/convert-html-to-docx", b"PK\x03\x04", "DOCX(zip)")
_html_convert_probe("convert_html_to_screenshot", "/v1/convert-html-to-screenshot", b"\x89PNG", "PNG")


@probe("document_convert_and_anonymize", "/v1/document/convert-and-anonymize", {"hetzner"},
       repro="curl -XPOST $AI_BRIDGE_URL/v1/document/convert-and-anonymize -H 'Authorization: Bearer $AI_BRIDGE_API_KEY' -F file=@tests/fixtures/smoke/smoke_min.pdf")
def _doc_convert_anon(ctx: Ctx) -> ProbeResult:
    """Convert + smart-anonymize in one shot (report's check-funnel path).
    Asserts the convert half works end to end; the anonymize *contract* itself
    is asserted by the smart_anonymize probe. Fail-loud 503 (anonymize disabled)
    is acceptable — a silent broken convert is not."""
    ep = "/v1/document/convert-and-anonymize"
    if not os.path.exists(PDF_FIXTURE):
        return ProbeResult("document_convert_and_anonymize", ep, False, f"fixture missing: {PDF_FIXTURE}", None, None)
    with open(PDF_FIXTURE, "rb") as fh:
        pdf = fh.read()
    r, ms = _timed(lambda: requests.post(
        f"{ctx.base_url}{ep}", headers=ctx.headers(),
        files={"file": ("smoke_min.pdf", pdf, "application/pdf")}, timeout=max(ctx.timeout, 180)))
    if r.status_code == 503:
        return ProbeResult("document_convert_and_anonymize", ep, True, "fail-loud 503 (anonymize disabled) — acceptable", r.status_code, ms)
    if r.status_code != 200:
        return ProbeResult("document_convert_and_anonymize", ep, False, f"HTTP {r.status_code}: {r.text[:200]}", r.status_code, ms)
    try:
        d = r.json()
        text = (d.get("anonymized_markdown") or d.get("markdown") or d.get("text")
                or d.get("content") or d.get("anonymized_text") or "")
        if d.get("status") == "error" or d.get("success") is False:
            return ProbeResult("document_convert_and_anonymize", ep, False, f"status=error: {str(d)[:200]}", r.status_code, ms)
    except ValueError:
        text = r.text
    if len(text.strip()) < 10:
        return ProbeResult("document_convert_and_anonymize", ep, False,
                           f"extracted text too short ({len(text.strip())} chars) — convert likely failed", r.status_code, ms)
    return ProbeResult("document_convert_and_anonymize", ep, True, f"convert+anonymize returned {len(text.strip())} chars", r.status_code, ms)


@probe("metrics_account_pool", "/v1/metrics/account-pool-state", {"hetzner", "server2"},
       repro="curl $AI_BRIDGE_URL/v1/metrics/account-pool-state -H 'Authorization: Bearer $AI_BRIDGE_API_KEY'")
def _pool_state(ctx: Ctx) -> ProbeResult:
    ep = "/v1/metrics/account-pool-state"
    r, ms = _timed(lambda: requests.get(f"{ctx.base_url}{ep}", headers=ctx.headers(), timeout=30))
    if r.status_code != 200:
        return ProbeResult("metrics_account_pool", ep, False, f"HTTP {r.status_code}", r.status_code, ms)
    d = r.json()
    if "accounts" not in d:
        return ProbeResult("metrics_account_pool", ep, False, f"no 'accounts' key: {str(d)[:150]}", r.status_code, ms)
    return ProbeResult("metrics_account_pool", ep, True, f"{len(d.get('accounts', []))} accounts reported", r.status_code, ms)


# Endpoints covered by dedicated probes above but whose canonical route differs
# from the probe endpoint are mapped here so the coverage validator sees them.
COVERED_ALIASES = {
    # metrics umbrella + liveness are covered by the pool-state probe + deploy's own /health wait
    "/v1/metrics": "metrics_account_pool",
    "/v1/metrics/{rest:path}": "metrics_account_pool",
    "/health": "container-healthcheck (deploy waits on docker health) + metrics probe",
    "/lb-status": "container-healthcheck",
    "/ready": "container-healthcheck",
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def resolve_api_key() -> str:
    key = os.environ.get("AI_BRIDGE_API_KEY")
    if key:
        return key
    # Infisical fallback (same source the deploy script uses)
    try:
        out = subprocess.run(
            ["bash", "-lc",
             "source /root/.infisical/infisical-api.sh 2>/dev/null && "
             "infisical_get_secret \"$INFISICAL_WS_DEV_SERVER\" dev AI_BRIDGE_API_KEY 2>/dev/null | tail -1"],
            capture_output=True, text=True, timeout=30)
        key = (out.stdout or "").strip()
    except Exception:
        key = ""
    if not key:
        print("SMOKE_FAIL: could not resolve AI_BRIDGE_API_KEY (env or Infisical)", file=sys.stderr)
        sys.exit(3)
    return key


def run(base_url: str, profile: str, only: Optional[str], extra_header: dict, attempts: int) -> list:
    ctx = Ctx(base_url=base_url.rstrip("/"), api_key=resolve_api_key(), extra_header=extra_header)
    selected = [p for p in PROBES if (profile in p.profiles) and (only is None or p.name == only)]
    results = []
    for p in selected:
        last = None
        for attempt in range(1, attempts + 1):
            try:
                last = p.fn(ctx)
            except Exception as e:  # a probe raising = that probe FAILS loud, never skipped
                last = ProbeResult(p.name, p.endpoint, False, f"probe raised: {type(e).__name__}: {e}")
            if last.ok:
                break
            if attempt < attempts:
                time.sleep(5)
        last.repro = p.repro  # attach for fix-session
        results.append(last)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--profile", default="hetzner", choices=["hetzner", "server2"])
    ap.add_argument("--only", default=None, help="run a single probe by name")
    ap.add_argument("--extra-header", default="", help="e.g. 'X-Priority: production'")
    ap.add_argument("--attempts", type=int, default=2, help="per-probe retries for transient flakiness")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON block")
    args = ap.parse_args()

    extra = {}
    if args.extra_header:
        k, v = args.extra_header.split(":", 1)
        extra[k.strip()] = v.strip()

    results = run(args.base_url, args.profile, args.only, extra, args.attempts)
    if not results:
        print(f"SMOKE_FAIL: no probes selected for profile={args.profile} only={args.only}", file=sys.stderr)
        sys.exit(2)

    failures = [r for r in results if not r.ok]
    for r in results:
        mark = "OK  " if r.ok else "FAIL"
        ms = f"{r.elapsed_ms}ms" if r.elapsed_ms is not None else "-"
        print(f"  [{mark}] {r.name:22s} {r.endpoint:34s} ({ms}) {r.detail}")

    if args.json:
        print("SMOKE_JSON:" + json.dumps([r.__dict__ for r in results]))

    if failures:
        names = ", ".join(f"{r.name}({r.endpoint})" for r in failures)
        print(f"SMOKE_FAIL: {len(failures)}/{len(results)} probes failed: {names}", file=sys.stderr)
        for r in failures:
            if r.repro:
                print(f"  repro[{r.name}]: {r.repro}", file=sys.stderr)
        sys.exit(1)

    print(f"SMOKE_OK: {len(results)}/{len(results)} probes passed (profile={args.profile})")
    sys.exit(0)


if __name__ == "__main__":
    main()
