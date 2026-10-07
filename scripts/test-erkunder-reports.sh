#!/usr/bin/env bash
# Disposable, synthetic DAC/lifecycle probe. No model calls, no host data.
set -euo pipefail
image=${1:?Pass the image built with docker/Dockerfile.erkunder}
root=$(cd "$(dirname "$0")/.." && pwd)
for number in 1 2 3; do
  docker run --rm --network none --read-only --cap-drop ALL \
    --cap-add CHOWN --cap-add SETUID --cap-add SETGID \
    --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add KILL \
    --security-opt no-new-privileges:true --pids-limit 128 \
    --memory 1g --memory-swap 1g --user 0:0 \
    --tmpfs /arbeit:uid=0,gid=0,mode=711,size=16m \
    --mount "type=bind,src=$root/tests/erkunder/container_reports_probe.py,dst=/probe.py,readonly" \
    -e PYTHONPATH=/app \
    "$image" python /probe.py "110${number}"
done
