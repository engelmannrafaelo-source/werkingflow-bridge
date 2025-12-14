# Research Function Test

**Test Script:** `research_function_test.py`

## Purpose

Tests the `/sc:research` functionality across all 3 Docker wrapper instances with:

- ✅ Parallel execution (all instances tested simultaneously)
- ✅ File recovery validation (base64 decode, checksum verify)
- ✅ Content quality checks (ensures actual research, not meta-responses)
- ✅ Output file preservation

## Usage

### Prerequisites

1. **Docker containers running:**
   ```bash
   docker ps | grep eco-wrapper
   # Should show 3 healthy containers on ports 8000, 8010, 8020
   ```

2. **Python dependencies:**
   ```bash
   cd /Users/lorenz/ECO/projects/eco-openai-wrapper
   poetry install  # or pip install httpx
   ```

### Run Test

```bash
# From project root
python tests/research_function_test.py

# Or directly
./tests/research_function_test.py
```

### Expected Output

```
2025-11-05 11:00:00 - __main__ - INFO - 🚀 Starting parallel research tests for 3 instances...
2025-11-05 11:00:00 - __main__ - INFO -    Prompt: Python async/await best practices
2025-11-05 11:00:00 - __main__ - INFO -    Timeout: 600.0s

2025-11-05 11:00:00 - __main__ - INFO - 🔬 Testing eco-wrapper-1 (port 8000)...
2025-11-05 11:00:00 - __main__ - INFO - 🔬 Testing eco-wrapper-2 (port 8010)...
2025-11-05 11:00:00 - __main__ - INFO - 🔬 Testing eco-wrapper-3 (port 8020)...

2025-11-05 11:01:30 - __main__ - INFO -   Session ID: abc123...
2025-11-05 11:01:30 - __main__ - INFO -   Files discovered: 1
2025-11-05 11:01:30 - __main__ - INFO -   Content validated: 850 words, 42 lines
2025-11-05 11:01:30 - __main__ - INFO -   Saved: tests/research_outputs/eco-wrapper-1/abc123/web_research.md

======================================================================
📊 TEST SUMMARY: 3/3 instances passed
======================================================================
✅ eco-wrapper-1
   Session ID: abc123...
   Duration: 35.2s
   Files: 1
   Content: 850 words, 5432 chars
   Output: tests/research_outputs/eco-wrapper-1/abc123

✅ eco-wrapper-2
   ...

✅ eco-wrapper-3
   ...

======================================================================
```

## Output Files

Research files are saved to:

```
tests/research_outputs/
├── eco-wrapper-1/
│   └── {session_id}/
│       └── web_research.md
├── eco-wrapper-2/
│   └── {session_id}/
│       └── web_research.md
└── eco-wrapper-3/
    └── {session_id}/
        └── web_research.md
```

## Test Configuration

**Test Prompt:** `Python async/await best practices`
**Research Depth:** `quick` (faster than `deep`)
**Timeout:** 600s (10 minutes)
**Instances:** All 3 wrappers in parallel

## Validation Checks

### 1. File Recovery
- ✅ `x_claude_metadata` present in response
- ✅ `discovery_status: "success"`
- ✅ `files_created[]` not empty
- ✅ `content_base64` field present
- ✅ Base64 decode successful
- ✅ UTF-8 decode successful
- ⚠️  Checksum verification (warning only)

### 2. Content Quality
- ✅ Minimum 500 characters
- ✅ Contains research indicators:
  - Markdown headings (`##`, `###`)
  - Technical terms (`async`, `await`, `function`)
  - Lists (`-`, `*`, `1.`)
- ❌ NOT meta-response patterns:
  - "I need permission"
  - "cannot conduct"
  - "Status Update:"

### 3. Error Handling
- ✅ Session ID extraction (from header)
- ✅ HTTP status validation
- ✅ Timeout handling
- ✅ Explicit error messages with session context

## Troubleshooting

### All Tests Fail: "Connection refused"

**Cause:** Docker containers not running

**Fix:**
```bash
docker compose up -d
docker ps  # Verify all 3 containers healthy
```

### Test Fails: "Missing x_claude_metadata"

**Cause:** Permission mode not set or tools disabled

**Fix:**
```bash
# Verify permission mode in container
docker exec eco-wrapper-1 printenv | grep CLAUDE_PERMISSION_MODE
# Should show: CLAUDE_PERMISSION_MODE=bypassPermissions

# If missing, restart containers
docker compose down && docker compose up -d
```

### Test Fails: "Content appears to be meta-response"

**Cause:** Research failed due to permission issues

**Check logs:**
```bash
docker logs eco-wrapper-1 | grep -E "(Permission|permission|I need)"
```

**Expected:** No permission errors
**If errors found:** Permission mode not active → restart containers

### Test Fails: "No files created by research"

**Cause:** File discovery failed or research produced no output

**Check logs:**
```bash
docker logs eco-wrapper-1 | grep -E "(File discovery|discovery_status)"
```

## Implementation Guide

Based on: `/Users/lorenz/ECO/Claude Code/LLM_how-to/2025-10-31-GUIDE_LLM_WRAPPER_RESEARCH_CLIENT.md`

**Key Patterns:**
- LAW 1: Session ID always in header `X-Claude-Session-ID`
- LAW 2: Never silent file transfer failures
- LAW 3: Timeout must match task duration

**Minimal Implementation:**
- Request with `enable_tools: True`, `timeout: 600.0`
- Extract session ID from header (not metadata)
- Validate `x_claude_metadata` exists (no silent fallback)
- Decode base64 with explicit error handling
- Validate content quality (not just meta-response)

## Exit Codes

- `0` - All tests passed
- `1` - One or more tests failed
- `130` - Interrupted by user (Ctrl+C)

## Related Files

- **Guide:** `/Users/lorenz/ECO/Claude Code/LLM_how-to/2025-10-31-GUIDE_LLM_WRAPPER_RESEARCH_CLIENT.md`
- **Docker Config:** `docker-compose.yml`
- **Permission Fix:** `CLAUDE_PERMISSION_MODE=bypassPermissions` (lines 37, 82, 127)
- **Wrapper Code:** `claude_cli.py` (permission mode handling)

---

**Version:** 1.0
**Created:** 2025-11-05
**Author:** Automated test generation
