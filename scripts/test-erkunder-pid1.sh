#!/usr/bin/env bash
# Usage: scripts/test-erkunder-pid1.sh <image-built-from-this-checkout>
# Only disposable, networkless containers; no credentials or customer volumes.
set -euo pipefail
image=${1:?Pass the image built with docker/Dockerfile.erkunder}
root=$(cd "$(dirname "$0")/.." && pwd)
container=
trap 'if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null; fi' EXIT
for number in 1 2 3; do
  printf 'Testing place %s\n' "$number"
  args=(--network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --pids-limit 512 \
    --memory 1g --memory-swap 1g --user "110${number}:1100" \
    --tmpfs "/arbeit:uid=110${number},gid=1100,mode=700,size=16m" \
    --tmpfs /tmp:size=16m \
    --mount "type=bind,src=$root/tests/erkunder/container_pid1_probe.py,dst=/probe.py,readonly" \
    -e ERKUNDER_INTERNAL_TOKEN=synthetic-container-token -e PYTHONPATH=/app)
  # First check the actual Compose command, including uvicorn startup.
  container=$(docker run -d "${args[@]}" "$image" python -m src.erkunder.platz)
  docker exec "$container" python -c '
import subprocess, time, urllib.error, urllib.request
from pathlib import Path
from src.erkunder import platz
import inspect, re
port = re.search(r"port=(\d+)", inspect.getsource(platz)).group(1)
for attempt in range(100):
    try:
        urllib.request.urlopen("http://127.0.0.1:" + port + "/abbrechen", timeout=1)
    except urllib.error.HTTPError as error:
        assert error.code == 403
        break
    except urllib.error.URLError:
        time.sleep(0.1)
else:
    raise AssertionError("Platz server did not become ready")
assert b"src.erkunder.platz" in Path("/proc/1/cmdline").read_bytes()
probe = subprocess.run(["bash", "-c", "cat /proc/1/environ >/dev/null"],
                       capture_output=True)
assert probe.returncode != 0 and b"Permission denied" in probe.stderr
print("actual Platz/uvicorn PID 1: same-UID Bash denied")
'
  docker rm -f "$container" >/dev/null
  container=
  docker run --rm "${args[@]}" "$image" python /probe.py
done
