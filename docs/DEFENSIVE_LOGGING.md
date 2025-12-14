# Defensive Logging & Silent Failure Prevention

## Overview

This document describes the defensive logging mechanisms implemented to prevent silent failures in the ECO OpenAI Wrapper.

## Problem Statement

**Silent Failures** occur when:
1. CLI session completes successfully (`CLI session completed` log)
2. But NO corresponding `EVENT` log is generated
3. Client receives 500 error, but no trace in logs
4. Impossible to debug why the request failed

### Root Cause

The wrapper previously had a logging gap where `HTTPException` was caught and re-raised WITHOUT logging.

## Solution: Multi-Layer Defensive Logging

### 1. HTTPException Logging (main.py:818-831)

**What:** Log HTTPException BEFORE re-raising

**Impact:** All HTTPExceptions now generate an EVENT log entry

### 2. Chunk Collection Visibility (main.py:770-809)

**What:** Log chunk count and provide diagnostic info when parsing fails

**Impact:** When parsing fails, we now know:
- How many chunks were received
- What format they were in
- Why parsing failed

### 3. parse_claude_message() Defensive Logging (claude_cli.py:260-343)

**What:** Detailed logging inside parse_claude_message to understand WHY it returns None

**Impact:** We can now see:
- Which message formats were encountered
- Why parsing failed (empty list, wrong format, no text blocks)
- Message structure for debugging

### 4. Zero-Chunks Detection (claude_cli.py:229-247)

**What:** Detect when SDK completes without yielding any chunks

**Impact:** We now know if SDK returns zero chunks and why

## Monitoring: Silent Failure Detection

### Script: scripts/detect_silent_failures.py

**Purpose:** Detect CLI sessions that completed without EVENT logs

**Usage:**
```bash
# Analyze last 24 hours
python scripts/detect_silent_failures.py --time-window 86400

# Analyze all logs with verbose output
python scripts/detect_silent_failures.py --verbose
```

## Summary

With these defensive logging improvements:

✅ NO MORE SILENT FAILURES - Every failure generates an EVENT log
✅ RICH DIAGNOSTICS - Chunk count visible, parse errors explained
✅ PROACTIVE MONITORING - Detection script identifies issues
✅ BETTER DEBUGGING - Full visibility into failure path
