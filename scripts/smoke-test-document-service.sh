#!/usr/bin/env bash
# Smoke test for the privacy-pdf-service /document/* endpoints.
#
# Usage:
#   scripts/smoke-test-document-service.sh [HOST]
#
# HOST defaults to http://localhost:8100. Run after `docker run` or
# `docker compose up -d privacy-service` to confirm the universal document
# converter is wired up correctly across all adapters.
#
# Exits non-zero on any failure so it can gate CI / deploy steps.

set -euo pipefail

HOST="${1:-http://localhost:8100}"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

green()  { printf '\033[32m%s\033[0m\n' "$*"; }
red()    { printf '\033[31m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }

PASS=0
FAIL=0

assert_jq_success() {
    local label="$1" path="$2" expected_format="$3"
    local response="$4"

    local success format
    success=$(echo "$response" | jq -r '.success // empty' 2>/dev/null || true)
    format=$(echo "$response" | jq -r '.format // empty'   2>/dev/null || true)

    if [[ "$success" == "true" && "$format" == "$expected_format" ]]; then
        green "  ✓ $label (format=$format)"
        PASS=$((PASS + 1))
    else
        red "  ✗ $label (success=$success format=$format expected=$expected_format)"
        echo "    response: $(echo "$response" | head -c 400)"
        FAIL=$((FAIL + 1))
    fi
}

# ---- Health ----
yellow "→ /health"
HEALTH=$(curl -fsS "$HOST/health" || true)
if [[ "$(echo "$HEALTH" | jq -r '.status // empty')" == "healthy" ]]; then
    green "  ✓ healthy"
    PASS=$((PASS + 1))
else
    red "  ✗ health response unexpected: $HEALTH"
    FAIL=$((FAIL + 1))
fi

# ---- CSV ----
yellow "→ /document/convert (CSV)"
printf 'name,city\nAlice,Wien\nBob,Graz\n' > "$SCRATCH/sample.csv"
RESPONSE=$(curl -fsS -F "file=@$SCRATCH/sample.csv" "$HOST/document/convert" || true)
assert_jq_success "CSV adapter" "$SCRATCH/sample.csv" "csv" "$RESPONSE"

# ---- HTML ----
yellow "→ /document/convert (HTML)"
printf '<html><body><h1>Hi</h1><p>Hello</p></body></html>' > "$SCRATCH/sample.html"
RESPONSE=$(curl -fsS -F "file=@$SCRATCH/sample.html" "$HOST/document/convert" || true)
assert_jq_success "HTML adapter" "$SCRATCH/sample.html" "html" "$RESPONSE"

# ---- EML ----
yellow "→ /document/convert (EML)"
cat > "$SCRATCH/sample.eml" <<'EOF'
From: alice@example.com
To: bob@example.com
Subject: Hi
Content-Type: text/plain; charset=utf-8

Body text.
EOF
RESPONSE=$(curl -fsS -F "file=@$SCRATCH/sample.eml" "$HOST/document/convert" || true)
assert_jq_success "EML adapter" "$SCRATCH/sample.eml" "eml" "$RESPONSE"

# ---- XLSX ---- (build via python in the container if openpyxl available)
yellow "→ /document/convert (XLSX)"
python3 - <<PY > "$SCRATCH/sample.xlsx" 2>/dev/null || yellow "  (skip: openpyxl not on host — XLSX smoke must be run inside the container)"
import sys
try:
    from openpyxl import Workbook
    import io
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["a", "b"])
    ws.append([1, 2])
    buf = io.BytesIO()
    wb.save(buf)
    sys.stdout.buffer.write(buf.getvalue())
except ImportError:
    sys.exit(1)
PY

if [[ -s "$SCRATCH/sample.xlsx" ]]; then
    RESPONSE=$(curl -fsS -F "file=@$SCRATCH/sample.xlsx" "$HOST/document/convert" || true)
    assert_jq_success "XLSX adapter" "$SCRATCH/sample.xlsx" "xlsx" "$RESPONSE"
fi

# ---- Unsupported format → expect 415 ----
yellow "→ /document/convert (unsupported extension)"
echo "binary blob" > "$SCRATCH/sample.xyz"
HTTP_CODE=$(curl -s -o "$SCRATCH/_resp" -w "%{http_code}" -F "file=@$SCRATCH/sample.xyz" "$HOST/document/convert")
if [[ "$HTTP_CODE" == "415" ]]; then
    green "  ✓ 415 Unsupported Media Type"
    PASS=$((PASS + 1))
else
    red "  ✗ expected 415, got $HTTP_CODE"
    cat "$SCRATCH/_resp" | head -c 300
    echo
    FAIL=$((FAIL + 1))
fi

# ---- Empty file → expect 400 ----
yellow "→ /document/convert (empty)"
: > "$SCRATCH/empty.csv"
HTTP_CODE=$(curl -s -o "$SCRATCH/_resp" -w "%{http_code}" -F "file=@$SCRATCH/empty.csv" "$HOST/document/convert")
if [[ "$HTTP_CODE" == "400" ]]; then
    green "  ✓ 400 Empty"
    PASS=$((PASS + 1))
else
    red "  ✗ expected 400, got $HTTP_CODE"
    FAIL=$((FAIL + 1))
fi

echo
if [[ "$FAIL" == "0" ]]; then
    green "ALL SMOKE TESTS PASSED ($PASS/$((PASS + FAIL)))"
    exit 0
else
    red "SMOKE TESTS FAILED ($FAIL/$((PASS + FAIL)))"
    exit 1
fi
