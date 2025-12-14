# Production Logging Deployment Guide

**ECO OpenAI Wrapper** - Production-Ready Logging Configuration

## 🎯 Quick Start

### Recommended Production Settings

```bash
# .env Configuration
LOG_LEVEL=INFO                    # Production default
ENABLE_DIAGNOSTIC=false           # Disable debug logs
LOG_TO_FILE=true                  # Enable file logging
LOG_TO_CONSOLE=false              # Disable console (for background processes)
FILTER_SENSITIVE_DATA=true        # Enable security filter
```

### Environment-Specific Settings

**Development**:
```bash
LOG_LEVEL=DEBUG
ENABLE_DIAGNOSTIC=true
LOG_TO_CONSOLE=true
FILTER_SENSITIVE_DATA=false  # See full data in dev
```

**Staging**:
```bash
LOG_LEVEL=INFO
ENABLE_DIAGNOSTIC=false
LOG_TO_CONSOLE=true
FILTER_SENSITIVE_DATA=true
```

**Production**:
```bash
LOG_LEVEL=INFO
ENABLE_DIAGNOSTIC=false
LOG_TO_CONSOLE=false  # Background processes only
FILTER_SENSITIVE_DATA=true
```

---

## 📊 Log Files Overview

### Python Logs (Rotating, 10MB each, 5 backups)

| File | Content | Max Size | Backups | Total |
|------|---------|----------|---------|-------|
| `logs/app.log` | All logs (DEBUG+) | 10MB | 5 | 60MB |
| `logs/error.log` | Errors only (ERROR+) | 10MB | 5 | 60MB |
| `logs/diagnostic.log` | Emoji markers (🔴🟡🔵🟣) | 10MB | 2 | 30MB |

### Shell Script Logs

| File | Content | Rotation |
|------|---------|----------|
| `logs/startup.log` | Shell events (start/stop/errors) | Manual (via `scripts/rotate-logs.sh`) |

### Instance-Specific Logs

| Instance | Instance Tag | Location | Purpose |
|----------|--------------|----------|---------|
| shared | `[shared]` | `logs/app.log` | Main wrapper (port 8000) |
| eco-backend | `[eco-backend]` | `instances/eco-backend/logs/app.log` | Backend wrapper (port 8010) |
| eco-diagnostics | `[eco-diagnostics]` | `instances/eco-diagnostics/logs/app.log` | Diagnostics wrapper (port 8020) |

**Note**: No separate wrapper.log files exist. Uvicorn output is redirected to `/dev/null` to prevent console blocking while Python application logs use the centralized logging system with instance identification.

**Total Max Disk Usage**: ~210MB (60MB + 60MB + 30MB per instance)

---

## 📝 Log Format Details

### Standard Format (Human-Readable)

**With Instance Identification** (Multi-Instance Deployments):
```
2025-10-10 12:00:00 - [eco-backend] - middleware.performance_monitor - WARNING - [middleware:send_with_timing:95] - Slow request [tools]: POST /v1/chat/completions - 35.2s
                      ^^^^^^^^^^^^^   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                      Instance Name   Logger Name                               Module:Function:Line
```

**Format Components**:
- `%(asctime)s`: Timestamp (YYYY-MM-DD HH:MM:SS)
- `[INSTANCE_NAME]`: Instance identifier (shared, eco-backend, eco-diagnostics)
- `%(name)s`: Logger name (module path)
- `%(levelname)s`: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- `[%(module)s:%(funcName)s:%(lineno)d]`: Source code location
- `%(message)s`: Log message content

**Configuration**:
```python
# Automatically set via environment variables in start-wrappers.sh
INSTANCE_NAME=eco-backend PORT=8010 poetry run uvicorn main:app ...
```

**Benefits**:
- ✅ Instance identification in every log entry
- ✅ Works with log aggregation tools (ELK, Splunk, Datadog)
- ✅ Easy filtering by instance name
- ✅ No separate wrapper.log files needed

**Filter Examples**:
```bash
# Filter logs by instance
grep '\[shared\]' logs/app.log
grep '\[eco-backend\]' instances/eco-backend/logs/app.log

# Aggregate all logs, filter specific instance
cat logs/app.log instances/*/logs/app.log | grep '\[eco-backend\]'

# Monitor specific instance in real-time
tail -f instances/eco-backend/logs/app.log | grep '\[eco-backend\]'
```

---

## 🔒 Security Filter

### Sensitive Data Patterns (Automatically Masked)

The security filter **automatically masks** the following in logs:

1. **API Keys**: `api_key=***`, `ANTHROPIC_API_KEY=***`
2. **Tokens**: `Bearer ***`
3. **Passwords**: `password=***`
4. **Session IDs**: Shows first 8 chars, rest masked

### Example

**Before Filtering**:
```
INFO - Request with api_key=sk-1234567890abcdefghij and password=secretpass123
```

**After Filtering**:
```
INFO - Request with api_key=*** and password=***
```

### Disabling in Development

```bash
# .env
FILTER_SENSITIVE_DATA=false  # Only in dev/test!
```

⚠️ **Never disable in production** - risk of credential leaks!

---

## 📈 Performance Monitoring

### Automatic Request Tracking with Tool Detection

Every request is automatically monitored with **tool-aware thresholds**:

**Non-Tool Requests** (enable_tools=false):
- **Duration**: Time from request start to completion
- **Slow Request Warning**: > 5s threshold (default)
- **Very Slow Request Error**: > 10s threshold (default)

**Tool-Enabled Requests** (enable_tools=true):
- **Duration**: Includes Bash, file operations, web searches
- **Slow Request Warning**: > 30s threshold (default)
- **Very Slow Request Error**: > 60s threshold (default)

**Why Different Thresholds?**
Tool requests naturally take 5-10x longer due to external operations. Using the same thresholds would create false alarms.

### Performance Metrics Endpoint

```bash
# Get current performance metrics
curl http://localhost:8000/v1/metrics

# Response:
{
  "metrics": {
    "total_requests": 1250,
    "average_duration": 2.345,
    "slow_requests": 42,
    "very_slow_requests": 5,
    "endpoints": {
      "/v1/chat/completions": {
        "count": 1200,
        "avg_duration": 2.1,
        "min_duration": 0.5,
        "max_duration": 15.3,
        "slow_count": 40,
        "very_slow_count": 5
      }
    }
  },
  "thresholds": {
    "non_tool": {
      "slow_request": "5.0s",
      "very_slow_request": "10.0s"
    },
    "tool_enabled": {
      "slow_request": "30.0s",
      "very_slow_request": "60.0s"
    }
  }
}
```

### Custom Performance Headers

Every response includes:
```
X-Request-Duration: 2.345s
```

---

## 📊 Structured Event Logs

### Event Types

All business logic events are logged in JSON format:

1. **chat_completion**: Successful chat requests
2. **chat_completion_error**: Failed chat requests
3. **authentication**: Auth success/failure
4. **session_management**: Session created/updated/deleted
5. **rate_limit**: Rate limiting events
6. **error**: General errors

### Event Format

```json
{
  "timestamp": "2025-10-09T14:30:45Z",
  "instance": "eco-backend",
  "port": 8010,
  "event_type": "chat_completion",
  "data": {
    "session_id": "abc123",
    "model": "claude-sonnet-4",
    "message_count": 5,
    "stream": false,
    "duration_seconds": 2.345,
    "tokens": 150,
    "tools_enabled": true
  }
}
```

**Instance Fields** (when using JSON logging):
- `instance`: Instance name (shared, eco-backend, eco-diagnostics)
- `port`: Instance port number (8000, 8010, 8020)

### Extracting Events for Analytics

```bash
# Extract all chat completion events
grep "EVENT:" logs/app.log | grep "chat_completion" | jq .

# Count errors by type
grep "EVENT:" logs/error.log | jq -r '.data.error' | sort | uniq -c

# Average request duration (all requests)
grep "EVENT:" logs/app.log | jq -r '.data.duration_seconds' | awk '{sum+=$1; count++} END {print sum/count}'

# Average duration separated by tool usage
grep "EVENT:" logs/app.log | grep "chat_completion" | \
  jq -r 'select(.data.tools_enabled == true) | .data.duration_seconds' | \
  awk '{sum+=$1; count++} END {print "Tool avg: " sum/count "s (" count " requests)"}'

grep "EVENT:" logs/app.log | grep "chat_completion" | \
  jq -r 'select(.data.tools_enabled == false or .data.tools_enabled == null) | .data.duration_seconds' | \
  awk '{sum+=$1; count++} END {print "Non-tool avg: " sum/count "s (" count " requests)"}'
```

---

## 🔄 Log Rotation

### Automatic Rotation (Python Logs)

Python logs rotate **automatically** when reaching 10MB:
- `app.log` → `app.log.1` → `app.log.2` → ... → `app.log.5` (deleted)
- Configured in `config/logging_config.py`
- No manual intervention needed

### Manual Rotation (Shell Logs)

Shell logs (`startup.log`) require manual rotation:

```bash
# Run rotation script (checks size, rotates if > 10MB)
./scripts/rotate-logs.sh

# Automated via start-wrappers.sh (runs on each start)
./start-wrappers.sh  # Rotates automatically before starting
```

### Monitoring Disk Usage

```bash
# Check current log sizes
du -h logs/*.log

# Check all logs including backups
du -h logs/

# Monitor in real-time
watch -n 5 'du -h logs/*.log'
```

---

## 🚨 Error Handling & Alerts

### Log Levels for Monitoring

| Level | Use Case | Action |
|-------|----------|--------|
| ERROR | Critical failures | Alert immediately |
| WARNING | Slow requests, auth failures | Monitor trends |
| INFO | Normal operations | Archive for analytics |
| DEBUG | Development only | Disable in production |

### Shell Script Exit Codes

| Code | Meaning | Action |
|------|---------|--------|
| 0 | Success | Continue |
| 10 | Port already in use | Check for conflicts |
| 11 | Process failed to start | Check logs/error.log |

### Health Check Integration

```bash
# start-wrappers.sh includes automatic health checks
./start-wrappers.sh

# Output:
# 🏥 Running health checks...
#   ✅ wrapper-shared is healthy and responding
#   ✅ wrapper-eco-backend is healthy and responding
#   ✅ wrapper-eco-diagnostics is healthy and responding
```

If health check fails:
1. Check `logs/error.log` for Python errors
2. Check `logs/startup.log` for shell script errors
3. Verify Claude Code authentication: `curl http://localhost:8000/v1/auth/status`

---

## 📊 Monitoring Best Practices

### 1. Set Up Log Aggregation

**Recommended Tools**:
- **ELK Stack**: Elasticsearch + Logstash + Kibana
- **Grafana Loki**: Lightweight log aggregation
- **Datadog**: Cloud-based monitoring

**Example: Filebeat → Elasticsearch**

```yaml
# filebeat.yml
filebeat.inputs:
  - type: log
    enabled: true
    paths:
      - /path/to/eco-openai-wrapper/logs/*.log
    json.keys_under_root: true  # For structured events

output.elasticsearch:
  hosts: ["localhost:9200"]
```

### 2. Set Up Alerts

**Critical Alerts** (immediate action):
- ERROR level logs
- Very slow requests (>10s)
- Authentication failures (>10/min)
- Memory threshold exceeded

**Warning Alerts** (monitor trends):
- Slow requests (>5s)
- Log file size approaching limits
- Session count growing

### 3. Regular Maintenance

**Daily**:
- Monitor error rates
- Check performance metrics
- Verify disk space

**Weekly**:
- Review slow requests
- Analyze event patterns
- Check backup file counts

**Monthly**:
- Archive old logs
- Review security filter effectiveness
- Update thresholds based on usage

---

## 🖥️ Console Output Management

### Problem: Terminal Blocked by Continuous Logs

**Symptom**: After running `start-wrappers.sh`, terminal doesn't return to prompt and continues showing log output

**Root Cause**:
- `LOG_TO_CONSOLE=false` controls Python application logs only
- Uvicorn's stdout/stderr still outputs to terminal by default
- This causes terminal to remain attached to process output

**Solution Implemented**:
```bash
# start-wrappers.sh redirects all uvicorn output to /dev/null
poetry run uvicorn main:app --host 0.0.0.0 --port 8000 \
  > /dev/null 2>&1 &
```

**Benefits**:
- ✅ Terminal returns immediately after startup
- ✅ No console pollution from uvicorn access logs
- ✅ All meaningful logs still captured in app.log via Python logging
- ✅ HTTP requests logged by PerformanceMonitor middleware
- ✅ Startup events logged to logs/startup.log

**Verification**:
```bash
# After starting wrappers
./start-wrappers.sh

# Terminal should return immediately with:
# ✅ All wrapper instances started and healthy!
# (no continuous log output)

# Monitor logs using tail instead
tail -f logs/app.log
```

---

## 🔧 Troubleshooting

### Problem: Logs Not Being Created

**Check**:
```bash
# Verify logs directory exists
ls -la logs/

# Check permissions
ls -ld logs/
# Should be writable: drwxr-xr-x

# Check LOG_TO_FILE setting
grep LOG_TO_FILE .env
```

**Fix**:
```bash
mkdir -p logs
chmod 755 logs
```

### Problem: Sensitive Data in Logs

**Check**:
```bash
# Search for potential leaks
grep -i "api.key\|password\|token" logs/app.log
```

**Fix**:
```bash
# Enable filter
echo "FILTER_SENSITIVE_DATA=true" >> .env

# Restart wrapper
./stop-wrappers.sh && ./start-wrappers.sh
```

### Problem: Logs Growing Too Fast

**Check**:
```bash
# Check current size
du -sh logs/

# Check rate of growth
ls -lht logs/*.log | head -5
```

**Fix**:
```bash
# Reduce log level
sed -i 's/LOG_LEVEL=DEBUG/LOG_LEVEL=INFO/' .env

# Or disable diagnostic logs
sed -i 's/ENABLE_DIAGNOSTIC=true/ENABLE_DIAGNOSTIC=false/' .env

# Restart
./stop-wrappers.sh && ./start-wrappers.sh
```

### Problem: Can't Identify Which Instance Generated Logs

**Symptom**: When viewing aggregated logs from multiple instances, can't tell which wrapper instance generated each log entry

**Check**:
```bash
# Look for instance tags in logs
grep "\[shared\]" logs/app.log | head -5
grep "\[eco-backend\]" instances/eco-backend/logs/app.log | head -5

# If no [instance_name] tags appear, instance identification is not configured
```

**Solution**:
Instance identification is configured in [config/logging_config.py](../config/logging_config.py#L158-160):
```python
instance_name = os.getenv('INSTANCE_NAME', 'main')
port = os.getenv('PORT', '8000')

formatter = logging.Formatter(
    f'%(asctime)s - [{instance_name}] - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
```

**Verification**:
```bash
# Check environment variables are set in start-wrappers.sh
grep "INSTANCE_NAME=" start-wrappers.sh

# Should show:
# INSTANCE_NAME="shared" PORT=8000
# INSTANCE_NAME="eco-backend" PORT=8010
# INSTANCE_NAME="eco-diagnostics" PORT=8020
```

**Filter by Instance**:
```bash
# View specific instance logs
grep '\[shared\]' logs/app.log
grep '\[eco-backend\]' instances/eco-backend/logs/app.log

# Aggregate all logs and filter by instance
cat logs/app.log instances/*/logs/app.log | grep '\[eco-backend\]'
```

### Problem: Performance Degradation

**Check**:
```bash
# Get current metrics
curl http://localhost:8000/v1/metrics | jq .

# Check for slow requests (separated by tool usage)
grep "Slow request \[non-tools\]" logs/app.log | tail -20
grep "Slow request \[tools\]" logs/app.log | tail -20

# Check for very slow requests
grep "VERY SLOW REQUEST" logs/error.log
```

**Analyze**:
```bash
# Analyze request durations separated by tool usage
./scripts/analyze_thresholds.sh

# Or manually extract durations:
# Non-tool requests
grep "Slow request \[non-tools\]" logs/app.log | grep -oP '\d+\.\d+s' | sort -n | tail -10

# Tool-enabled requests
grep "Slow request \[tools\]" logs/app.log | grep -oP '\d+\.\d+s' | sort -n | tail -10
```

**Fix**: See [Performance Threshold Documentation](DOCS_PERFORMANCE_THRESHOLDS.md) for detailed calibration instructions

---

## 📚 Additional Resources

- **Logging Configuration**: [config/logging_config.py](../config/logging_config.py)
- **Performance Monitoring**: [middleware/performance_monitor.py](../middleware/performance_monitor.py) - Tool-aware thresholds
- **Event Logging**: [middleware/event_logger.py](../middleware/event_logger.py) - Tool usage tracking
- **Performance Threshold Documentation**: [DOCS_PERFORMANCE_THRESHOLDS.md](DOCS_PERFORMANCE_THRESHOLDS.md) - Tool-aware calibration
- **Analysis Script**: [scripts/analyze_thresholds.sh](../scripts/analyze_thresholds.sh) - Separate tool/non-tool statistics
- **Shell Logging Tests**: [tests/test_shell_logging.sh](../tests/test_shell_logging.sh)
- **Log Rotation Tests**: [tests/test_log_rotation.py](../tests/test_log_rotation.py)

---

## 📞 Support

For issues or questions:
1. Check `logs/error.log` for error details
2. Run shell logging tests: `./tests/test_shell_logging.sh`
3. Run Python tests: `poetry run pytest tests/test_logging_config.py -v`
4. Check health status: `curl http://localhost:8000/health`

---

**Last Updated**: 2025-10-10
**Version**: 2.1 - Multi-Instance Logging with Instance Identification
