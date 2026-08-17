#!/usr/bin/env bash
# bridge-drift-check.sh — read-only comparison of "what SHOULD be running"
# against "what IS running", per bridge host.
#
# WHY THIS EXISTS (Rafael, 17.08.2026)
# -------------------------------------------------------------------------
# bridge-deploy.sh's own provenance check (Phase 1) only runs when someone
# deploys — an out-of-band change on a bridge host (manual `docker restart`
# onto a stale image, a hand-pulled image, a host-level container recreate)
# was invisible until the NEXT deploy happened to notice it, sometimes days
# later (observed 2026-07-31 on server2: checkout sat two commits ahead of
# the running images, unnoticed the whole time). This script is meant to run
# on a short cron (see orchestrator/bin/bridge-drift-watch.py) so the same
# class of drift surfaces within minutes instead of at the next deploy.
#
# "What SHOULD be running" = the release manifest bridge-deploy.sh writes on
# every successful deploy (write_release_manifest(), Phase 7) —
# ${REMOTE_REPO}/.bridge-release-manifest.json, per service: container name,
# image ID, commit, timestamp. "What IS running" = a live
# `docker inspect --format '{{.Image}}' <container>` on the host, read here,
# read-only, nothing is ever written to the bridge hosts by this script.
#
# Usage: bridge-drift-check.sh <hetzner|server2>
#
# EXIT CODES
#   0 = every manifest entry's image matches the live container (or the
#       manifest is empty/not-yet-written, which is a coverage gap, not drift)
#   1 = at least one DRIFT or MISSING finding — see stdout
#   2 = the check itself could not run (host unreachable, manifest corrupt) —
#       distinct from "0" because it says nothing about drift either way
set -uo pipefail

SERVER="${1:-}"
case "$SERVER" in
    hetzner) HOST="49.12.72.66" ;;
    server2) HOST="178.104.178.79" ;;
    *)
        echo "Usage: bridge-drift-check.sh <hetzner|server2>" >&2
        exit 2
        ;;
esac
REMOTE_REPO="/root/werkingflow-bridge"
MANIFEST_FILE="${REMOTE_REPO}/.bridge-release-manifest.json"

log() { echo "[drift:$SERVER] $*"; }

# One remote python call reads the manifest and docker-inspects every
# recorded container server-side — one SSH round-trip regardless of how many
# services are recorded, not N+1. Findings are ALWAYS printed with exit 0
# from python itself; drift/no-drift is signalled via marker lines in stdout,
# not via the process exit code — that keeps "python found drift" cleanly
# distinguishable from "ssh/python itself failed" (which needs its own exit 2,
# not to be confused with exit 1 = drift found).
out=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=yes "root@${HOST}" \
    "python3 - '${MANIFEST_FILE}'" <<'PYEOF' 2>&1
import json, subprocess, sys

manifest_file = sys.argv[1]
try:
    with open(manifest_file) as f:
        manifest = json.load(f)
except FileNotFoundError:
    print("NO_MANIFEST")
    sys.exit(0)
except Exception as e:
    print(f"MANIFEST_UNREADABLE {e}")
    sys.exit(0)

services = manifest.get("services", {})
if not services:
    print("EMPTY_MANIFEST")
    sys.exit(0)

drift = 0
for svc, entry in sorted(services.items()):
    container = entry.get("container")
    expected = entry.get("image_id")
    if not container or not expected:
        print(f"BAD_ENTRY {svc}: manifest entry missing container/image_id")
        drift += 1
        continue
    r = subprocess.run(
        ["docker", "inspect", "--format", "{{.Image}}", container],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0:
        print(f"MISSING {svc} container={container} — not running "
              f"(manifest expects image {expected[:19]}, "
              f"commit {entry.get('deployed_commit', '?')[:12]}, "
              f"deployed_at {entry.get('deployed_at', '?')})")
        drift += 1
        continue
    actual = r.stdout.strip()
    if actual != expected:
        print(f"DRIFT {svc} container={container} "
              f"manifest_image={expected[:19]} running_image={actual[:19]} "
              f"manifest_commit={entry.get('deployed_commit', '?')[:12]} "
              f"manifest_deployed_at={entry.get('deployed_at', '?')}")
        drift += 1
    else:
        print(f"OK {svc} container={container} image={actual[:19]}")

print(f"SUMMARY services={len(services)} drift={drift}")
sys.exit(0)
PYEOF
)
rc=$?

if [[ $rc -ne 0 ]]; then
    echo "[drift:$SERVER] ERROR: cannot reach ${HOST} or run the remote check (ssh/python rc=${rc})" >&2
    [[ -n "$out" ]] && echo "$out" >&2
    exit 2
fi

if echo "$out" | grep -q '^MANIFEST_UNREADABLE'; then
    log "ERROR: release manifest on ${HOST} is corrupt: $(echo "$out" | grep '^MANIFEST_UNREADABLE')"
    exit 2
fi

if echo "$out" | grep -q '^NO_MANIFEST'; then
    log "no release manifest yet on ${HOST} (${MANIFEST_FILE}) — nothing to compare. Not drift: this host has not had a" \
        "deploy since write_release_manifest() shipped. Coverage gap, reported as exit 0 (not a drift finding)."
    exit 0
fi
if echo "$out" | grep -q '^EMPTY_MANIFEST'; then
    log "release manifest on ${HOST} has no service entries yet — coverage gap, not drift."
    exit 0
fi

echo "$out" | grep -v '^SUMMARY' | while IFS= read -r line; do log "$line"; done
drift_count=$(echo "$out" | grep -oP 'SUMMARY .*drift=\K[0-9]+' | tail -1)
drift_count="${drift_count:-0}"

if [[ "$drift_count" -gt 0 ]]; then
    log "DRIFT DETECTED — ${drift_count} service(s) on ${HOST} running code other than the release manifest."
    exit 1
fi
log "OK — all recorded services on ${HOST} match their release manifest."
exit 0
