"""Build docs/service-route-inventory-worker.json from the REAL worker app (R15).

Every effective route (including routers FastAPI includes lazily) gets one row per
method. Enforcement classes:
  dependency  - an enforcing Depends() refuses before the handler
  handler     - the handler (or a named helper it awaits) calls the verifier itself
  public      - deliberately open, reason in
                tests/unit/test_no_unauthenticated_routes.PUBLIC_ROUTES
  none        - declared gate without enforcement; stays visible with an
                "offen" reference
A route without an assessment here makes
tests/unit/test_r15_worker_route_inventory.py red.
Run:  python scripts/worker_route_inventory.py   (writes the JSON; no network, no DB)
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _m in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
    "src.identity.routes",
    "src.db.client",
]:
    sys.modules.setdefault(_m, MagicMock())

from fastapi.routing import APIRoute  # noqa: E402

OUT = ROOT / "docs/service-route-inventory-worker.json"
ENFORCING = {
    "require_service_token",
    "require_admin",
    "require_jwt_or_service",
    "require_jwt",
    "require_internal_service",
    "require_credential_admin_or_user",
}

PUBLIC = "public"
A = {}  # (method, path) -> dict(data=..., assessment=..., **extra)


def row(method, path, data, assessment, **extra):
    A[(method, path)] = {"data": data, "assessment": assessment, **extra}


# --- deliberately public (reasons: test_no_unauthenticated_routes.PUBLIC_ROUTES) ---
for method, path, data in [
    ("GET", "/v1/version", "build sha, build time, contract revision"),
    ("GET", "/health", "liveness + provider health flags"),
    ("GET", "/ready", "active request count"),
    ("GET", "/stats", "aggregate counters"),
    ("GET", "/rate-limits", "current limits"),
    ("GET", "/worker-capacity", "free capacity numbers"),
    ("GET", "/v1/providers", "provider tier catalogue"),
    ("GET", "/v1/privacy/status", "privacy layer on/off + counters"),
    ("GET", "/v1/auth/status", "whether auth is configured; no credentials"),
    ("GET", "/v1/usage/status", "aggregate usage state"),
    ("POST", "/v1/compatibility", "protocol negotiation"),
    ("GET", "/v1/metrics/account-pool-state", "limiter snapshot for nginx pool-router"),
]:
    row(
        method,
        path,
        data,
        "public by decision; no key material, no customer data",
        enforcement=PUBLIC,
    )

# --- credential-holding boundary of the keyless privacy process ---
for path, what in [
    (
        "/internal/privacy/messages",
        "text-only Anthropic call; app label never grants images",
    ),
    ("/internal/privacy/vision", "Gemini-only vision call"),
]:
    row(
        "POST",
        path,
        what,
        "internal: hmac of PRIVACY_MODEL_PROXY_TOKEN in handler before body read; "
        "not reachable with an app token",
        enforcement="handler",
        verifier="PRIVACY_MODEL_PROXY_TOKEN",
    )

# --- LLM / document routes: verify_api_key in the handler (shared key or principal) ---
row(
    "POST",
    "/v1/chat/completions",
    "chat completion",
    "verify_api_key inside _chat_completions_lauf; image egress decided at bild_policy",
    enforcement="handler",
    verifier="verify_api_key",
    via="_chat_completions_lauf",
)
for method, path, data in [
    ("POST", "/v1/research", "research job"),
    ("GET", "/v1/research/async/{request_id}", "async research status"),
    ("POST", "/v1/doc-agent", "document agent run"),
    ("GET", "/v1/research/{session_id}/content", "research report file"),
    ("GET", "/v1/models", "model list"),
    ("POST", "/v1/privacy/smart-anonymize", "pseudonymisation"),
    ("POST", "/v1/convert-pdf", "pdf->markdown"),
    ("POST", "/v1/convert-pdf-to-semantic-html", "pdf->html"),
    ("POST", "/v1/convert-html-to-docx", "html->docx"),
    ("POST", "/v1/convert-docx-to-html", "docx->html"),
    ("POST", "/v1/convert-html-to-pdf", "html->pdf"),
    ("POST", "/v1/convert-html-to-screenshot", "html->png"),
    ("POST", "/v1/convert-pdf-to-html-direct", "pdf->html"),
    (
        "POST",
        "/v1/document/convert",
        "document->markdown; describe_images never for Energy",
    ),
    ("POST", "/v1/document/convert-and-anonymize", "convert + anonymise"),
    ("POST", "/v1/audio/transcriptions", "speech to text"),
    ("GET", "/v1/metrics/anonymization", "audit counters"),
    ("POST", "/v1/jobs", "generic async job"),
    ("GET", "/v1/jobs", "own jobs"),
    ("GET", "/v1/jobs/{job_id}", "own job"),
    ("DELETE", "/v1/jobs/{job_id}", "cancel own job"),
    ("GET", "/v1/erkunder/bibliothek", "erkunder library"),
]:
    row(
        method,
        path,
        data,
        "verify_api_key in handler; app principal scoped by "
        "enforce_app_request/bild_policy",
        enforcement="handler",
        verifier="verify_api_key",
    )
for method, path in [
    ("GET", "/v1/erkunder/bericht/{bericht_id}/ergebnis"),
    ("POST", "/v1/erkunder/bericht/{bericht_id}/aufraeumen"),
]:
    row(
        method,
        path,
        "erkunder result / cleanup",
        "verify_api_key inside _forward",
        enforcement="handler",
        verifier="verify_api_key",
        via="_forward",
    )

# --- operator views (BR13, 498c4ed): shared key only, app principal 403 ---
for method, path, data in [
    ("GET", "/v1/sessions/stats", "session counters"),
    ("GET", "/v1/sessions", "all session ids"),
    ("GET", "/v1/sessions/{session_id}", "one session"),
    ("DELETE", "/v1/sessions/{session_id}", "mutation: deletes a conversation"),
    ("GET", "/v1/cli-sessions", "all CLI runs incl. prompt[:200]"),
    ("GET", "/v1/cli-sessions/stats", "CLI counters"),
    ("GET", "/v1/cli-sessions/{cli_session_id}", "one CLI run"),
    ("DELETE", "/v1/cli-sessions/{cli_session_id}", "mutation: aborts a running call"),
    ("DELETE", "/v1/cli-sessions", "mutation: cleanup of finished CLI runs"),
]:
    row(
        method,
        path,
        data,
        "operator view across all callers: _verify_operator_key (shared key; "
        "resolved app principal 403) — BR13",
        enforcement="handler",
        verifier="_verify_operator_key",
    )

# --- dependency-gated diagnostics ---
row(
    "GET",
    "/debug/tokens",
    "token count/rotation, no key material",
    "service-token diagnostic",
    enforcement="dependency",
)
row(
    "GET",
    "/license-health",
    "license health without previews",
    "service-token diagnostic",
    enforcement="dependency",
)
row(
    "POST",
    "/v1/debug/request",
    "echo of own request",
    "service-token self-diagnostic",
    enforcement="dependency",
)
row(
    "POST",
    "/health/reset",
    "mutation: resets provider breaker state",
    "bridge:admin only (R14); app principals 403",
    enforcement="dependency",
)

# --- declared HTTPBearer, NOT enforced on this head: visible, not hidden ---
for method, path, data in [
    ("GET", "/v1/metrics", "performance summary"),
    ("GET", "/v1/metrics/attribution", "attribution counters"),
    ("GET", "/v1/metrics/jobs-retention", "retention counters"),
    ("GET", "/v1/metrics/prompt-performance", "latency/quality aggregates"),
    ("GET", "/v1/metrics/prompt-performance/timeline", "aggregates per app+agent"),
    ("GET", "/v1/metrics/prompt-performance/calls", "per-call rows incl. user_id"),
    ("GET", "/v1/metrics/throughput", "throughput timeline"),
    ("GET", "/v1/metrics/usage-breakdown", "usage per app/user/model"),
    ("GET", "/v1/metrics/document-performance", "document pipeline aggregates"),
    ("GET", "/v1/metrics/request-log", "request log incl. client IPs"),
    ("GET", "/v1/metrics/cc-usage-history", "account usage history"),
    (
        "POST",
        "/v1/metrics/cc-usage-snapshot",
        "mutation: writes quota store the adaptive limiter reads",
    ),
    ("GET", "/v1/metrics/queue-forecast", "queue forecast incl. account names"),
]:
    row(
        method,
        path,
        data,
        "declared HTTPBearer but the handler never calls verify_api_key: reachable "
        "without a key. Not an Energy image/credential path. Fix exists as BR14 "
        "(bridge 9579599, not in develop); needs scraper/daemon keys first.",
        enforcement="none",
        offen="BR14 9579599 / hotfix 9306dee — not in develop",
    )


def dependency_names(dependant):
    found = set()

    def walk(dep):
        if dep.call is not None:
            found.add(getattr(dep.call, "__name__", type(dep.call).__name__))
        for sub in dep.dependencies:
            walk(sub)

    for dep in dependant.dependencies:
        walk(dep)
    return sorted(found)


def effective_routes(app):
    """Every effective (path, method, context) incl. routers FastAPI includes lazily."""

    def walk(routes):
        for r in routes:
            if isinstance(r, APIRoute):
                yield (
                    r.path,
                    sorted(r.methods - {"HEAD", "OPTIONS"}),
                    r.dependant,
                    r.endpoint,
                    r.body_field,
                )
            elif hasattr(r, "effective_candidates"):
                for c in r.effective_candidates():
                    if hasattr(c, "effective_candidates"):
                        yield from walk([c])
                    else:
                        orig = c.original_route
                        if isinstance(orig, APIRoute):
                            yield (
                                c.path,
                                sorted(orig.methods - {"HEAD", "OPTIONS"}),
                                c.dependant,
                                c.endpoint,
                                c.body_field,
                            )

    yield from walk(app.routes)


def build(app):
    rows, missing = [], []
    for path, methods, dependant, endpoint, body_field in effective_routes(app):
        deps = dependency_names(dependant)
        for method in methods:
            extra = A.get((method, path))
            if extra is None:
                missing.append(f"{method} {path}")
                continue
            gate = next((d for d in deps if d in ENFORCING), None) or (
                "HTTPBearer" if "HTTPBearer" in deps else "none"
            )
            rows.append(
                {
                    "path": path,
                    "method": method,
                    "gate": gate,
                    "dependencies": deps,
                    "endpoint": f"{endpoint.__module__}.{endpoint.__name__}",
                    "has_body": body_field is not None,
                    **extra,
                }
            )
    if missing:
        raise SystemExit(
            "routes without assessment in scripts/worker_route_inventory.py:\n  "
            + "\n  ".join(sorted(missing))
        )
    stale = sorted(
        f"{m} {p}"
        for (m, p) in A
        if not any(r["method"] == m and r["path"] == p for r in rows)
    )
    if stale:
        raise SystemExit(
            "assessments for routes that no longer exist:\n  " + "\n  ".join(stale)
        )
    rows.sort(key=lambda r: (r["path"], r["method"]))
    return {
        "_doc": "Every effective worker route (src/main.py incl. included routers), "
        "one row per method, built by scripts/worker_route_inventory.py from the "
        "real app. enforcement: dependency | handler (verifier[, via]) | public "
        "(reason in tests/unit/test_no_unauthenticated_routes.PUBLIC_ROUTES) | none "
        "(declared gate without enforcement, offen = where the fix is). Checked by "
        "tests/unit/test_r15_worker_route_inventory.py: completeness in both "
        "directions, gate present, enforcement real (behavioural probe or verifier "
        "source).",
        "routes": rows,
    }


if __name__ == "__main__":
    from src.main import app

    doc = build(app)
    OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(doc['routes'])} routes -> {OUT.relative_to(ROOT)}")
