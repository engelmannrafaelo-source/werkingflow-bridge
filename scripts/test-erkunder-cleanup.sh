#!/usr/bin/env bash
# Dedicated disposable IPC namespaces; never inspect/delete host IPC.
set -euo pipefail
image=${1:?Pass the image built with docker/Dockerfile.erkunder}
root=$(cd "$(dirname "$0")/.." && pwd)
for number in 1 2 3; do
  docker run --rm --network none --read-only --ipc private --cap-drop ALL \
    --security-opt no-new-privileges:true --pids-limit 128 \
    --memory 1g --memory-swap 1g --user "110${number}:1100" \
    --tmpfs "/arbeit:uid=110${number},gid=1100,mode=700,size=32m" \
    --tmpfs /tmp:size=32m \
    --mount "type=bind,src=$root/tests/erkunder/container_cleanup_probe.py,dst=/probe.py,readonly" \
    -e ERKUNDER_INTERNAL_TOKEN=synthetic-container-token -e PYTHONPATH=/app \
    "$image" python /probe.py
done
