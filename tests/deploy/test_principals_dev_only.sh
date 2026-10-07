#!/usr/bin/env bash
# ============================================================================
# test_principals_dev_only.sh — proves the dev-only exclusion in
# sync-principals.sh and check-principal-drift.sh (principals-dev-only.txt).
# ============================================================================
# A name on the dev-only list must never be inserted on a non-dev host, the
# skip must be said out loud, every other name keeps the union behaviour, and
# the drift check must not report the by-design asymmetry as drift.
#
# Never contacts a host: a fake `sudo` on PATH answers the psql queries from
# per-host fixture files and records every INSERT statement.
#
# Usage: tests/deploy/test_principals_dev_only.sh     (exit 0 = all cases hold)
# ============================================================================
set -euo pipefail

SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scripts" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/db"

# Fake `sudo -n ssh <opts> root@HOST "<remote cmd>"`; SQL arrives on stdin.
cat > "$tmp/bin/sudo" <<'FAKE'
#!/usr/bin/env bash
host=""; for a in "$@"; do [[ "$a" == root@* ]] && host="${a#root@}"; done
remote="${!#}"
db="$FAKE_DB/$host"
if [[ "$remote" == *"-tAc"* ]]; then          # check-principal-drift.sh
  awk -F'\t' '$7=="t" { print $1 "|" substr($2,1,12) "|apps=" $4 }' "$db"; exit 0
fi
sql="$(cat)"
case "$sql" in
  *"WHERE active"*) awk -F'\t' -v OFS='\t' '$7=="t" { print $1,$2,$3,$4,$5,$6 }' "$db" ;;
  "SELECT name FROM"*) cut -f1 "$db" ;;
  *INSERT*) printf '%s\n' "$sql" >> "$FAKE_DB/$host.inserts" ;;
esac
FAKE
chmod +x "$tmp/bin/sudo"
export PATH="$tmp/bin:$PATH" FAKE_DB="$tmp/db"

DEV=49.12.72.66; PROD=178.104.178.79
row() { printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$@"; }
# name hash prefix apps paths cap active
{ row energy-erkunder-dev aaaaaaaaaaaaaaaa aaaaaaaa werking-energy '*' NULL t
  row energy-dev bbbbbbbbbbbbbbbb bbbbbbbb werking-energy '*' NULL t
  row new-on-dev cccccccccccccccc cccccccc werking-report '*' NULL t; } > "$tmp/db/$DEV"
{ row energy-dev bbbbbbbbbbbbbbbb bbbbbbbb werking-energy '*' NULL t; } > "$tmp/db/$PROD"

pass=0; fail=0
ok()  { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }

out="$(bash "$SCRIPTS/sync-principals.sh" 2>&1)" && rc=0 || rc=$?
[[ $rc -eq 0 ]] && ok "sync exits 0" || bad "sync exit $rc: $out"
grep -q "SKIPPED dev-only principal 'energy-erkunder-dev' for $PROD" <<< "$out" \
  && ok "sync names the skipped dev-only principal" || bad "no loud skip line: $out"
grep -q "e-pva-erkunder-prodort" <<< "$out" && ok "skip line cites the open prod question" \
  || bad "skip line without reason"
if grep -q "energy-erkunder-dev" "$tmp/db/$PROD.inserts" 2>/dev/null; then
  bad "dev-only principal was inserted on prod"
else ok "dev-only principal NOT inserted on prod"; fi
grep -q "'new-on-dev'" "$tmp/db/$PROD.inserts" 2>/dev/null \
  && ok "other principals keep the union (new-on-dev copied)" || bad "union broken for normal names"
[[ ! -e "$tmp/db/$DEV.inserts" ]] && ok "nothing inserted on dev" || bad "unexpected insert on dev"

# Drift: with new-on-dev now on prod too, the only asymmetry is the dev-only name.
row new-on-dev cccccccccccccccc cccccccc werking-report '*' NULL t >> "$tmp/db/$PROD"
out="$(bash "$SCRIPTS/check-principal-drift.sh" 2>&1)" && rc=0 || rc=$?
[[ $rc -eq 0 ]] && ok "drift check passes with only the dev-only asymmetry" || bad "drift exit $rc: $out"
grep -q "NOTE: dev-only principals excluded.*energy-erkunder-dev" <<< "$out" \
  && ok "drift check names the excluded principal" || bad "exclusion not named: $out"

# A real drift on a normal name must still fail loud.
row stray-on-dev dddddddddddddddd dddddddd werking-report '*' NULL t >> "$tmp/db/$DEV"
out="$(bash "$SCRIPTS/check-principal-drift.sh" 2>&1)" && rc=0 || rc=$?
[[ $rc -eq 1 ]] && grep -q "stray-on-dev" <<< "$out" && ok "real drift still detected (exit 1)" \
  || bad "real drift missed (exit $rc): $out"

# Missing list file is a hard error, never a silent "no exclusions".
out="$(DEV_ONLY_FILE="$tmp/nope.txt" bash "$SCRIPTS/sync-principals.sh" 2>&1)" && rc=0 || rc=$?
[[ $rc -eq 2 ]] && ok "sync: missing list -> exit 2" || bad "sync with missing list exit $rc"
out="$(DEV_ONLY_FILE="$tmp/nope.txt" bash "$SCRIPTS/check-principal-drift.sh" 2>&1)" && rc=0 || rc=$?
[[ $rc -eq 2 ]] && ok "drift: missing list -> exit 2" || bad "drift with missing list exit $rc"

# List hygiene: CRLF, surrounding blanks and comments are tolerated; a name that
# merely shares a prefix with a dev-only name is NOT excluded.
printf '# comment only\r\n  energy-erkunder-dev  \r\n' > "$tmp/crlf.txt"
cp "$tmp/db/$DEV" "$tmp/db/$DEV.keep"; cp "$tmp/db/$PROD" "$tmp/db/$PROD.keep"
row energy-erkunder-dev2 eeeeeeeeeeeeeeee eeeeeeee werking-energy '*' NULL t >> "$tmp/db/$DEV"
rm -f "$tmp/db/$PROD.inserts"
out="$(DEV_ONLY_FILE="$tmp/crlf.txt" bash "$SCRIPTS/sync-principals.sh" 2>&1)" && rc=0 || rc=$?
if [[ $rc -eq 0 ]] && ! grep -q "'energy-erkunder-dev'" "$tmp/db/$PROD.inserts" \
   && grep -q "'energy-erkunder-dev2'" "$tmp/db/$PROD.inserts"; then
  ok "CRLF/blank list still excludes; prefix-sharing name is copied"
else bad "list hygiene/prefix (exit $rc): $(cat "$tmp/db/$PROD.inserts" 2>/dev/null)"; fi
cp "$tmp/db/$DEV.keep" "$tmp/db/$DEV"; cp "$tmp/db/$PROD.keep" "$tmp/db/$PROD"

# Only comments in the list: no exclusions, union for everything, exit 0.
printf '# nothing here\n' > "$tmp/empty.txt"; rm -f "$tmp/db/$PROD.inserts"
out="$(DEV_ONLY_FILE="$tmp/empty.txt" bash "$SCRIPTS/sync-principals.sh" 2>&1)" && rc=0 || rc=$?
[[ $rc -eq 0 ]] && grep -q "'energy-erkunder-dev'" "$tmp/db/$PROD.inserts" \
  && ok "comment-only list = no exclusions (explicit, not silent default)" || bad "empty list (exit $rc)"

# Leak: a dev-only name already active on prod must be loud in sync AND fail the drift check.
row energy-erkunder-dev aaaaaaaaaaaaaaaa aaaaaaaa werking-energy '*' NULL t >> "$tmp/db/$PROD"
sed -i '/^stray-on-dev/d' "$tmp/db/$DEV"
out="$(bash "$SCRIPTS/sync-principals.sh" 2>&1)" && rc=0 || rc=$?
grep -q "dev-only principal 'energy-erkunder-dev' EXISTS on $PROD" <<< "$out" \
  && ok "sync reports a leaked dev-only principal" || bad "leak silent in sync: $out"
out="$(bash "$SCRIPTS/check-principal-drift.sh" 2>&1)" && rc=0 || rc=$?
[[ $rc -eq 1 ]] && grep -q "DEV-ONLY PRINCIPAL ACTIVE ON $PROD" <<< "$out" \
  && ok "drift check fails on a leaked dev-only principal (exit 1)" || bad "leak not failing drift (exit $rc): $out"

echo "passed: $pass  failed: $fail"
[[ $fail -eq 0 ]]
