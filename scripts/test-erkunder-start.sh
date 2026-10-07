#!/usr/bin/env bash
# Start the real coordinator before real places, in a disposable internal network.
set -euo pipefail
image=${1:?Pass the image built with docker/Dockerfile.erkunder}
root=$(cd "$(dirname "$0")/.." && pwd)
probe_id="b1o-start-$$"
containers=()
cleanup() {
  for container in "${containers[@]}"; do docker rm -f "$container" >/dev/null; done
  docker network rm "$probe_id" >/dev/null
}
trap cleanup EXIT
docker network create --internal "$probe_id" >/dev/null
# Extract ports from the application source instead of maintaining test copies.
read -r coordinator_port place_port < <(python3 - "$root" <<'PY'
import re, sys
from pathlib import Path
root = Path(sys.argv[1])
print(*(re.search(r"port=(\d+)", (root / "src/erkunder" / (name + ".py")).read_text())[1]
        for name in ("leitstand", "platz")))
PY
)
containers+=("$probe_id-leitstand")
docker run -d --name "$probe_id-leitstand" --network "$probe_id" \
  --memory 512m --memory-swap 512m --read-only --pids-limit 128 \
  --tmpfs /arbeit:mode=711,size=16m --tmpfs /tmp:size=16m \
  -e ERKUNDER_INTERNAL_TOKEN=synthetic-start-token "$image" sh -c \
  'mkdir /arbeit/old-report && exec python -m src.erkunder.leitstand' >/dev/null
# Prove it has entered the wait, then launch the dependent containers.
for attempt in {1..50}; do
  if docker logs "$probe_id-leitstand" 2>&1 | grep -q 'wartet auf Plaetze'; then break; fi
  sleep 0.1
done
docker logs "$probe_id-leitstand" 2>&1 | grep -q 'wartet auf Plaetze'
for number in 1 2 3; do
  containers+=("$probe_id-platz-$number")
  docker run -d --name "$probe_id-platz-$number" --network "$probe_id" \
    --network-alias "erkunder-platz-$number" --ipc private --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --user "110${number}:1100" \
    --memory 512m --memory-swap 512m --pids-limit 128 --tmpfs /tmp:size=16m \
    -e ERKUNDER_INTERNAL_TOKEN=synthetic-start-token \
    "$image" python -m src.erkunder.platz >/dev/null
done
docker exec -i "$probe_id-leitstand" python - "$coordinator_port" <<'PY'
import json, sys, time, urllib.request, urllib.error
from pathlib import Path
port = sys.argv[1]
for attempt in range(100):
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/bereitschaft",
            headers={"X-Erkunder-Intern": "synthetic-start-token"})
        with urllib.request.urlopen(request, timeout=1) as response:
            assert json.load(response) == {"bereit": True}
        break
    except (urllib.error.URLError, TimeoutError):
        time.sleep(0.1)
else:
    raise AssertionError("coordinator never became ready")
assert Path("/arbeit/old-report").stat().st_mode & 0o777 == 0o700
print("PASS: old report sealed; delayed real places; coordinator ready")
PY
test "$(docker inspect -f '{{.RestartCount}}' "$probe_id-leitstand")" = 0
docker logs "$probe_id-leitstand" 2>&1
