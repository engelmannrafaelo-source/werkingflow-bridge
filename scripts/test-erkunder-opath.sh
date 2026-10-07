#!/usr/bin/env bash
# Disposable container only; root prepares DAC, probe drops to each real place UID.
set -euo pipefail
image=${1:?Pass the image built with docker/Dockerfile.erkunder}
root=$(cd "$(dirname "$0")/.." && pwd)
for number in 1 2 3; do
  docker run --rm --network none --read-only --cap-drop ALL \
    --cap-add CHOWN --cap-add SETUID --cap-add SETGID \
    --security-opt no-new-privileges:true --pids-limit 512 \
    --memory 1g --memory-swap 1g --user 0:0 \
    --tmpfs /arbeit:uid=0,gid=0,mode=711,size=16m \
    --tmpfs /tmp:size=16m \
    --mount "type=bind,src=$root/tests/erkunder/container_opath_probe.py,dst=/probe.py,readonly" \
    -e ERKUNDER_INTERNAL_TOKEN=synthetic-container-token -e PYTHONPATH=/app \
    "$image" python /probe.py "110${number}"
done
