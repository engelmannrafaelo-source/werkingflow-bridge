#!/bin/bash
# Bridge Health Monitor — läuft jede Minute via cron auf Hetzner (self-check)
# Loggt Status beider Pools + markiert bei 3 Fehlern in Folge als unhealthy.
#
# Log: /var/log/bridge-health.log
# State: /var/run/bridge-unhealthy (exists = unhealthy)

set -u
LOG_TS=$(date '+%Y-%m-%d %H:%M:%S')
STATE_FILE=/var/run/bridge-unhealthy
COUNTER_FILE=/var/run/bridge-health-counter
FAIL_THRESHOLD=3

check() {
  local name="$1" url="$2" header="${3:-}"
  local code
  if [ -n "$header" ]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H "$header" "$url" 2>/dev/null)
  else
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null)
  fi
  echo "$code"
}

WORKERS=$(check "workers"        "http://localhost:8000/health")
PROD=$(check    "prod-bridge"    "http://localhost:8000/health/production")
LB_STATUS=$(check "lb-status"    "http://localhost:8000/lb-status")

OK=1
[ "$WORKERS" = "200" ] || OK=0
[ "$PROD" = "200" ]    || OK=0

if [ "$OK" = "1" ]; then
  echo 0 > "$COUNTER_FILE"
  rm -f "$STATE_FILE" 2>/dev/null
  echo "$LOG_TS OK  workers=$WORKERS prod=$PROD lb-status=$LB_STATUS"
else
  CNT=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
  CNT=$((CNT + 1))
  echo "$CNT" > "$COUNTER_FILE"
  echo "$LOG_TS FAIL workers=$WORKERS prod=$PROD lb-status=$LB_STATUS fail_count=$CNT"
  if [ "$CNT" -ge "$FAIL_THRESHOLD" ] && [ ! -f "$STATE_FILE" ]; then
    echo "$LOG_TS" > "$STATE_FILE"
    echo "$LOG_TS ALERT bridge marked UNHEALTHY after $CNT consecutive failures"
  fi
fi
