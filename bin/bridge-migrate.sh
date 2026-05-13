#!/usr/bin/env bash
# Bridge Postgres migration runner — fail-fast, idempotent, atomic per file.
#
# Discovers migrations/*.sql, sorted lexicographically (so 001_, 002_, …
# enforce order). Each file is applied inside a single transaction; on the
# first error the transaction rolls back and the runner aborts loudly.
#
# Applied migrations are tracked in `schema_migrations` (filename + sha256
# + applied_at). Re-applying the same file is a no-op when its checksum
# matches; mismatched checksum aborts (someone edited an already-applied
# migration — that's never safe).
#
# Usage:
#   bridge-migrate.sh                # apply all pending
#   bridge-migrate.sh --dry-run      # list what would run
#   bridge-migrate.sh --status       # show applied + pending
#
# Restore note: this script never DOWN-migrates. Forward-only by design.
# A "fix" for a bad migration is a NEW migration that compensates.

set -euo pipefail

readonly CONTAINER="bridge-postgres-prod"
readonly DB_USER="bridge"
readonly DB_NAME="bridge"
readonly MIGRATIONS_DIR="/root/werkingflow-bridge/docker/migrations"
readonly MODE="${1:---apply}"

log() { printf "[migrate %s] %s\n" "$(date +%H:%M:%S)" "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }

psql() { docker exec -i "${CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" "$@"; }

# 1) Ensure tracker table exists (idempotent)
psql -v ON_ERROR_STOP=1 -q <<'SQL' >/dev/null
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    VARCHAR(255) PRIMARY KEY,
    sha256      VARCHAR(64)  NOT NULL,
    applied_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
SQL

# 2) Enumerate migration files (must exist)
[ -d "${MIGRATIONS_DIR}" ] || die "migrations dir not found: ${MIGRATIONS_DIR}"
mapfile -t FILES < <(find "${MIGRATIONS_DIR}" -maxdepth 1 -name '*.sql' -printf '%f\n' | sort)
[ "${#FILES[@]}" -gt 0 ] || die "no .sql files in ${MIGRATIONS_DIR}"

# 3) Load applied set
mapfile -t APPLIED_ROWS < <(psql -t -A -F'|' -c "SELECT filename, sha256 FROM schema_migrations")
declare -A APPLIED_SHA
for row in "${APPLIED_ROWS[@]}"; do
    [ -z "${row}" ] && continue
    APPLIED_SHA["${row%%|*}"]="${row##*|}"
done

case "${MODE}" in
    --status)
        printf "%-40s  %-10s\n" "migration" "status"
        for f in "${FILES[@]}"; do
            if [ -n "${APPLIED_SHA[$f]+x}" ]; then
                printf "%-40s  %-10s\n" "$f" "applied"
            else
                printf "%-40s  %-10s\n" "$f" "PENDING"
            fi
        done
        exit 0
        ;;
    --dry-run)
        log "DRY-RUN — listing pending migrations only"
        ;;
    --apply|"")
        ;;
    *)
        die "unknown mode: ${MODE} (use --apply / --dry-run / --status)"
        ;;
esac

# 4) Apply pending
applied_count=0
for f in "${FILES[@]}"; do
    path="${MIGRATIONS_DIR}/${f}"
    sha="$(sha256sum "${path}" | awk '{print $1}')"

    if [ -n "${APPLIED_SHA[$f]+x}" ]; then
        prev="${APPLIED_SHA[$f]}"
        if [ "${prev}" != "${sha}" ]; then
            die "checksum mismatch on already-applied migration ${f} (db=${prev:0:12} disk=${sha:0:12}) — refusing to continue"
        fi
        continue
    fi

    log "applying ${f} (sha=${sha:0:12})"
    if [ "${MODE}" = "--dry-run" ]; then
        log "  (dry-run: would apply)"
        continue
    fi

    # Wrap migration + tracker insert in one transaction. The migration file
    # itself should NOT contain BEGIN/COMMIT — we drive that here.
    if ! cat "${path}" <(printf "\nINSERT INTO schema_migrations (filename, sha256) VALUES ('%s', '%s');\n" "${f}" "${sha}") \
            | psql -v ON_ERROR_STOP=1 --single-transaction -q; then
        die "migration ${f} failed — rolled back"
    fi
    log "  ok"
    applied_count=$((applied_count + 1))
done

if [ "${MODE}" = "--apply" ]; then
    log "done — applied ${applied_count} new migration(s)"
fi
