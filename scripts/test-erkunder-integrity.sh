#!/usr/bin/env bash
set -euo pipefail
image=${1:?Pass the image built with docker/Dockerfile.erkunder}
root=$(cd "$(dirname "$0")/.." && pwd)
docker run --rm --network none --read-only --cap-drop ALL \
  --cap-add CHOWN --cap-add SETUID --cap-add SETGID --cap-add DAC_OVERRIDE \
  --security-opt no-new-privileges:true --memory 512m --memory-swap 512m \
  --pids-limit 128 --tmpfs /arbeit:mode=711,size=16m \
  --mount "type=bind,src=$root/tests/erkunder/container_integrity_probe.py,dst=/probe.py,readonly" \
  -e PYTHONPATH=/app "$image" python /probe.py
