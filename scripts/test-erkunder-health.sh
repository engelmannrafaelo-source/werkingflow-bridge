#!/usr/bin/env bash
# Reuse a runtime image, mount the source being tested. No model or host services.
set -euo pipefail
cd "$(dirname "$0")/.."
image=${1:?Usage: test-erkunder-health.sh existing-erkunder-image}
prefix="b1n-health-${RANDOM}-$$"
cleanup() {
    for role in platz leitstand proxy; do
        docker rm -f "$prefix-$role" >/dev/null 2>&1 || true
    done
}
trap cleanup EXIT
for role in platz leitstand proxy; do
    command=(python -m "src.erkunder.$role")
    [[ $role == proxy ]] && command=(/usr/local/bin/erkunder-proxy)
    docker run -d --name "$prefix-$role" --network none --memory 512m --pids-limit 64 \
        -e ERKUNDER_INTERNAL_TOKEN=synthetic-health-token \
        -v "$PWD/src:/app/src:ro" "$image" "${command[@]}" >/dev/null
    ready=false
    for attempt in {1..30}; do
        if docker exec "$prefix-$role" python -m src.erkunder.gesundheit "$role" 2>/dev/null; then
            ready=true; break
        fi
        sleep 0.2
    done
    [[ $ready == true ]] || { docker logs "$prefix-$role"; exit 1; }
    echo "PASS $role readiness"
    if [[ $role != proxy ]]; then
        if docker exec -e ERKUNDER_INTERNAL_TOKEN=wrong "$prefix-$role" \
            python -m src.erkunder.gesundheit "$role" 2>/dev/null; then
            echo "FAIL $role accepted wrong token"; exit 1
        fi
        echo "PASS $role wrong token fails"
    fi
done
