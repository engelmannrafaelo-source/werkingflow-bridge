# Performance Threshold Configuration Guide

**Quick Guide**: How to analyze and configure performance thresholds for tool-enabled and non-tool requests.

**Status**: ✅ Pure ASGI Implementation (Streaming-Safe)

**Last Updated**: 2025-10-09 - Migrated to Pure ASGI middleware for streaming compatibility

---

## 🚨 Problem: False Alarms from Tool-Enabled Requests

**Different request types have vastly different performance characteristics**:

### Non-Tool Requests (Simple Chat)
- Typical duration: 2-10 seconds
- No external operations (pure LLM inference)
- Default thresholds: `5.0s` (slow), `10.0s` (very slow)

### Tool-Enabled Requests (enable_tools=true)
- Typical duration: 30-120 seconds
- Includes: Bash commands, file operations, web searches, multi-turn tool use
- Default thresholds: `30.0s` (slow), `60.0s` (very slow)

**The Problem**: Using same thresholds for both creates false alarms:
```
❌ ERROR - VERY SLOW REQUEST [non-tools]: 12.3s (threshold: 10.0s)
   → Actual problem: request is slower than expected

✅ WARNING - Slow request [tools]: 45.2s (threshold: 30.0s)
   → Normal: tools take time, not an error
```

**Solution**: Separate thresholds based on tool usage (automatic detection)

---

## ✅ Solution: Tool-Aware Performance Monitoring

### Step 1: Collect Data (Make Different Request Types)

```bash
# Start wrapper
./start-wrappers.sh

# Make non-tool requests (simple chat)
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4",
    "messages": [{"role": "user", "content": "Explain quantum computing"}],
    "enable_tools": false
  }'

# Make tool-enabled requests (with Bash, Read, Edit, etc.)
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4",
    "messages": [{"role": "user", "content": "List files in current directory"}],
    "enable_tools": true
  }'
```

### Step 2: Analyze Request Durations (Separated by Tool Usage)

```bash
# Run tool-aware analysis script
./scripts/analyze_thresholds.sh
```

**Example Output**:

```
📊 Request Duration Statistics - SEPARATED BY TOOL USAGE
==================================================

NON-TOOL REQUESTS (enable_tools=false)
--------------------------------------------------
Sample Size:    850 requests

Central Tendency:
  Average:      4.2s
  Median (P50): 3.8s

Spread:
  Std Dev:      1.5s
  Min:          1.2s
  Max:          12.3s

Percentiles:
  P75:          5.1s  (75% of requests faster)
  P90:          6.8s  (90% of requests faster)
  P95:          8.2s  (95% of requests faster)
  P99:          11.5s (99% of requests faster)

💡 Recommended Thresholds (Non-Tool)
--------------------------------------------------
Method 1: Statistical (avg ± σ)
  SLOW_REQUEST_THRESHOLD=5.7       # avg + 1σ
  VERY_SLOW_REQUEST_THRESHOLD=7.2  # avg + 2σ

Method 2: Percentile-based (P90, P95)
  SLOW_REQUEST_THRESHOLD=6.8       # P90
  VERY_SLOW_REQUEST_THRESHOLD=8.2  # P95

==================================================

TOOL-ENABLED REQUESTS (enable_tools=true)
--------------------------------------------------
Sample Size:    400 requests

Central Tendency:
  Average:      42.3s
  Median (P50): 38.5s

Spread:
  Std Dev:      18.2s
  Min:          15.3s
  Max:          125.4s

Percentiles:
  P75:          52.1s  (75% of requests faster)
  P90:          68.5s  (90% of requests faster)
  P95:          85.2s  (95% of requests faster)
  P99:          110.8s (99% of requests faster)

💡 Recommended Thresholds (Tool-Enabled)
--------------------------------------------------
Method 1: Statistical (avg ± σ)
  SLOW_REQUEST_THRESHOLD_TOOLS=60.5       # avg + 1σ
  VERY_SLOW_REQUEST_THRESHOLD_TOOLS=78.7  # avg + 2σ

Method 2: Percentile-based (P90, P95)
  SLOW_REQUEST_THRESHOLD_TOOLS=68.5       # P90
  VERY_SLOW_REQUEST_THRESHOLD_TOOLS=85.2  # P95

==================================================

📋 READY TO COPY TO .env
==================================================
# Add to .env file:

# Non-tool request thresholds
SLOW_REQUEST_THRESHOLD=6.8
VERY_SLOW_REQUEST_THRESHOLD=8.2

# Tool-enabled request thresholds
SLOW_REQUEST_THRESHOLD_TOOLS=68.5
VERY_SLOW_REQUEST_THRESHOLD_TOOLS=85.2
```

### Step 3: Update Configuration

**Edit \`.env\` file**:

```bash
# Non-tool request thresholds (simple chat completions)
SLOW_REQUEST_THRESHOLD=6.8
VERY_SLOW_REQUEST_THRESHOLD=8.2

# Tool-enabled request thresholds (with Bash, Read, Edit, etc.)
SLOW_REQUEST_THRESHOLD_TOOLS=68.5
VERY_SLOW_REQUEST_THRESHOLD_TOOLS=85.2
```

### Step 4: Restart Wrapper

```bash
# Restart to apply new settings
./stop-wrappers.sh
./start-wrappers.sh

# Verify new thresholds in startup logs
grep "Performance Monitor initialized" logs/app.log | tail -1
# Output: Performance Monitor initialized:
#   Non-tool requests: slow=6.8s, very_slow=8.2s
#   Tool requests: slow=68.5s, very_slow=85.2s
```

---

## 🤖 How Tool Detection Works

### Automatic Detection in Middleware (Pure ASGI)

The performance monitor automatically detects tool usage using streaming-safe Pure ASGI implementation:

```python
# From middleware/performance_monitor.py (Pure ASGI - Streaming-Safe!)
async def receive_with_tool_detection():
    message = await receive()

    # Capture body chunks for tool detection
    if message["type"] == "http.request" and method == "POST" and path == "/v1/chat/completions":
        body_chunk = message.get("body", b"")
        if body_chunk:
            body_chunks.append(body_chunk)

        # Parse when last chunk received
        if not message.get("more_body", False) and body_chunks:
            try:
                full_body = b"".join(body_chunks)
                body_data = json.loads(full_body.decode())
                tools_enabled = body_data.get('enable_tools', False)
            except:
                # If parsing fails, use non-tool thresholds
                pass

    return message  # ← Body forwarded to app, NOT consumed!

# Select appropriate thresholds in send callback
if tools_enabled:
    slow_threshold = self.slow_threshold_tools      # 30s default
    very_slow_threshold = self.very_slow_threshold_tools  # 60s default
else:
    slow_threshold = self.slow_threshold            # 5s default
    very_slow_threshold = self.very_slow_threshold  # 10s default
```

**Key Differences from BaseHTTPMiddleware**:
- ✅ **Streaming-Safe**: Does NOT block on `await call_next()`
- ✅ **Body Not Consumed**: Message forwarded to app, not cached
- ✅ **Parallel Execution**: App runs concurrently with monitoring
- ✅ **Supports Streaming Responses**: Logs AFTER last chunk sent

### Log Output Shows Tool Type

```
INFO - Slow request [non-tools]: POST /v1/chat/completions - 7.2s (threshold: 5.0s)
WARNING - Slow request [tools]: POST /v1/chat/completions - 45.3s (threshold: 30.0s)
```

### Event Logs Include Tool Information

```json
{
  "timestamp": "2025-10-09T14:30:45Z",
  "event_type": "chat_completion",
  "data": {
    "session_id": "abc123",
    "model": "claude-sonnet-4",
    "duration_seconds": 45.3,
    "tools_enabled": true
  }
}
```

---

## 📊 Choosing the Right Method

### Method 1: Statistical (Recommended for most cases)

**Formula**:
- Slow: \`average + 1 × standard_deviation\`
- Very Slow: \`average + 2 × standard_deviation\`

**When to use**:
- ✅ Your request durations follow a normal distribution
- ✅ You want to catch statistical outliers
- ✅ General production monitoring

**Catches**:
- ~16% of requests as "slow"
- ~2.5% of requests as "very slow"

---

### Method 2: Percentile-based

**Formula**:
- Slow: \`P90\` (90th percentile)
- Very Slow: \`P95\` (95th percentile)

**When to use**:
- ✅ Your request durations are NOT normally distributed
- ✅ You have occasional extreme outliers (e.g., 5min requests)
- ✅ You want predictable alerting (always 10% and 5%)

**Catches**:
- Exactly 10% of requests as "slow"
- Exactly 5% of requests as "very slow"

---

## 🎯 Examples for Different Systems

### Fast System (Mostly Non-Tool Requests)

```
Non-tool: Average 2.1s, P90: 3.5s, P95: 4.2s
Tool: Average 25.3s, P90: 35.1s, P95: 42.8s

Recommendation: Keep defaults or slight adjustment
SLOW_REQUEST_THRESHOLD=3.5
VERY_SLOW_REQUEST_THRESHOLD=4.2
SLOW_REQUEST_THRESHOLD_TOOLS=35.1
VERY_SLOW_REQUEST_THRESHOLD_TOOLS=42.8
```

---

### Medium System (Mixed Usage)

```
Non-tool: Average 4.5s, P90: 7.2s, P95: 9.8s
Tool: Average 38.2s, P90: 52.5s, P95: 68.3s

Recommendation: Percentile-based
SLOW_REQUEST_THRESHOLD=7.2
VERY_SLOW_REQUEST_THRESHOLD=9.8
SLOW_REQUEST_THRESHOLD_TOOLS=52.5
VERY_SLOW_REQUEST_THRESHOLD_TOOLS=68.3
```

---

### Heavy Tool Usage System (Complex Operations)

```
Non-tool: Average 3.2s, P90: 5.8s, P95: 7.1s
Tool: Average 65.8s, P90: 95.3s, P95: 125.4s

Recommendation: Statistical method (tool), Percentile (non-tool)
SLOW_REQUEST_THRESHOLD=5.8
VERY_SLOW_REQUEST_THRESHOLD=7.1
SLOW_REQUEST_THRESHOLD_TOOLS=84.0  # avg + 1σ
VERY_SLOW_REQUEST_THRESHOLD_TOOLS=102.2  # avg + 2σ
```

---

## 🔄 Re-calibration Schedule

### When to Re-analyze

**Weekly** (first month):
- Your usage patterns are stabilizing
- Helps catch early trends
- Separate analysis for tool vs non-tool

**Monthly** (after stabilization):
- Regular health check
- Seasonal variations (if applicable)
- Verify tool/non-tool split remains accurate

**After major changes**:
- New features deployed (especially new tools)
- Hardware upgrades/downgrades
- MCP server changes
- Model changes (e.g., Opus → Sonnet)
- Tool usage pattern changes

### How to Re-calibrate

```bash
# 1. Analyze current data (separated by tool usage)
./scripts/analyze_thresholds.sh

# 2. Compare with current settings
grep "SLOW_REQUEST_THRESHOLD" .env

# 3. If >20% change recommended, update
# 4. Restart wrapper
./stop-wrappers.sh && ./start-wrappers.sh
```

---

## 📈 Monitoring After Changes

### Verify New Thresholds Work

```bash
# Check non-tool slow requests today
grep "Slow request \[non-tools\]" logs/app.log | grep "$(date +%Y-%m-%d)" | wc -l

# Check tool-enabled slow requests today
grep "Slow request \[tools\]" logs/app.log | grep "$(date +%Y-%m-%d)" | wc -l

# Should be ~10-20% and ~2-5% of respective request types
```

### Track Metrics by Tool Usage

```bash
# Extract tool vs non-tool statistics from event logs
grep "EVENT:" logs/app.log | grep "chat_completion" | \
  jq -r 'select(.data.tools_enabled == true) | .data.duration_seconds' | \
  awk '{sum+=$1; count++} END {print "Tool avg: " sum/count "s (" count " requests)"}'

grep "EVENT:" logs/app.log | grep "chat_completion" | \
  jq -r 'select(.data.tools_enabled == false or .data.tools_enabled == null) | .data.duration_seconds' | \
  awk '{sum+=$1; count++} END {print "Non-tool avg: " sum/count "s (" count " requests)"}'
```

---

## 🔧 Troubleshooting

### Problem: Too Many "Slow Request" Warnings for Tool Requests

**Symptoms**:
```bash
grep "Slow request \[tools\]" logs/app.log | wc -l
# Output: 320  (out of 400 tool requests = 80%)
```

**Solution**: Increase \`SLOW_REQUEST_THRESHOLD_TOOLS\`

```bash
# Re-analyze with current data
./scripts/analyze_thresholds.sh

# Update .env with recommended tool thresholds
SLOW_REQUEST_THRESHOLD_TOOLS=68.5
VERY_SLOW_REQUEST_THRESHOLD_TOOLS=85.2
```

---

### Problem: Non-Tool Requests Triggering False Alarms

**Symptoms**:
```bash
grep "VERY SLOW REQUEST \[non-tools\]" logs/error.log | wc -l
# Output: 425  (out of 500 non-tool requests = 85%)
```

**Solution**: Your non-tool requests are genuinely slower than defaults

```bash
# Re-analyze to find appropriate non-tool thresholds
./scripts/analyze_thresholds.sh

# Update non-tool thresholds in .env
SLOW_REQUEST_THRESHOLD=11.7
VERY_SLOW_REQUEST_THRESHOLD=14.9
```

---

### Problem: Analysis Script Shows Insufficient Data

**Symptoms**:
```bash
./scripts/analyze_thresholds.sh
# Output: ⚠️  Only 5 tool-enabled requests found. Need at least 10 for reliable analysis.
```

**Solution**: Make more requests of the missing type

```bash
# For tool-enabled requests
for i in {1..15}; do
  curl -X POST http://localhost:8000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{
      "model": "claude-sonnet-4",
      "messages": [{"role": "user", "content": "List files"}],
      "enable_tools": true
    }'
done

# Then re-analyze
./scripts/analyze_thresholds.sh
```

---

## 📚 Related Documentation

- [Performance Monitoring Middleware](../middleware/performance_monitor.py#L30) - Tool detection logic
- [Production Logging Documentation](DOCS_PRODUCTION_LOGGING.md#L108) - Performance monitoring overview
- [Event Logger](../middleware/event_logger.py#L84) - Tool-aware event logging
- [Analysis Script](../scripts/analyze_thresholds.sh) - Tool-separated statistics

---

## 🎯 Quick Reference

| Action | Command |
|--------|---------|
| **Analyze durations (tool-aware)** | \`./scripts/analyze_thresholds.sh\` |
| **View current thresholds** | \`grep "SLOW_REQUEST" .env\` |
| **Set new thresholds** | Edit \`.env\` → Add/Update values |
| **Restart wrapper** | \`./stop-wrappers.sh && ./start-wrappers.sh\` |
| **Verify settings** | \`grep "Performance Monitor initialized" logs/app.log \| tail -1\` |
| **Check non-tool slow requests** | \`grep "Slow request \[non-tools\]" logs/app.log \| tail -20\` |
| **Check tool slow requests** | \`grep "Slow request \[tools\]" logs/app.log \| tail -20\` |
| **Separate tool/non-tool stats** | See "Track Metrics by Tool Usage" above |

---

**Last Updated**: 2025-10-09
**Version**: 2.0 - Tool-Aware Performance Threshold Configuration
