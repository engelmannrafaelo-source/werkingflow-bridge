#!/usr/bin/env bash
# Bridge Postgres backup — fail-fast, defensive.
#
# Runs every 6h via cron. Dumps the bridge database from the
# bridge-postgres-prod container, gzip-compresses, retains 14 days locally,
# verifies the dump is non-trivial before declaring success.
#
# Restore:
#   gunzip < bridge-YYYYMMDD-HHMMSS.sql.gz | docker exec -i bridge-postgres-prod \
#     psql -U bridge -d bridge
#
# Exit codes:
#   0  success
#   1  pg_dump failed
#   2  dump too small (likely a Postgres outage produced empty SQL)
#   3  retention prune failed

set -euo pipefail

readonly CONTAINER="bridge-postgres-prod"
readonly DB_USER="bridge"
readonly DB_NAME="bridge"
readonly BACKUP_DIR="/root/werkingflow-bridge/backups"
readonly RETENTION_DAYS=14
readonly MIN_BYTES=1024           # below this is suspicious (empty dump)
readonly TS="$(date -u +%Y%m%dT%H%M%SZ)"
readonly OUT="${BACKUP_DIR}/bridge-${TS}.sql.gz"

log() { printf "[bridge-backup %s] %s\n" "$(date -u +%H:%M:%S)" "$*" >&2; }
die() { log "FATAL: $*"; exit "${2:-1}"; }

mkdir -p "${BACKUP_DIR}"

# 1) Verify container is running before attempting dump
if ! docker inspect -f '{{.State.Running}}' "${CONTAINER}" 2>/dev/null | grep -q true; then
    die "container ${CONTAINER} is not running — aborting" 1
fi

# 2) pg_dump → gzip, atomic via temp file
readonly TMP="${OUT}.tmp"
log "starting pg_dump → ${OUT}"
if ! docker exec "${CONTAINER}" pg_dump -U "${DB_USER}" -d "${DB_NAME}" --no-owner --no-acl 2>/dev/null \
        | gzip -9 > "${TMP}"; then
    rm -f "${TMP}"
    die "pg_dump failed" 1
fi

# 3) Sanity check: dump must contain at least one CREATE TABLE
readonly SIZE="$(stat -c %s "${TMP}")"
if [ "${SIZE}" -lt "${MIN_BYTES}" ]; then
    rm -f "${TMP}"
    die "dump suspiciously small (${SIZE} bytes < ${MIN_BYTES}) — refusing to keep" 2
fi
# Disable pipefail just for this sanity probe: `head -N` legitimately closes
# stdin early, which signals SIGPIPE to gunzip and would otherwise abort us.
set +o pipefail
if ! gunzip -c "${TMP}" | head -200 | grep -q "CREATE TABLE"; then
    rm -f "${TMP}"
    set -o pipefail
    die "dump contains no CREATE TABLE — refusing to keep" 2
fi
set -o pipefail

mv "${TMP}" "${OUT}"
log "wrote ${OUT} (${SIZE} bytes)"

# 4) Retention: prune dumps older than N days
readonly PRUNED="$(find "${BACKUP_DIR}" -maxdepth 1 -name 'bridge-*.sql.gz' -mtime "+${RETENTION_DAYS}" -print -delete | wc -l)"
log "retention: pruned ${PRUNED} dumps older than ${RETENTION_DAYS} days"

log "ok"
