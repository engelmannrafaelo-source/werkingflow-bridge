#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# Exercise the real code-update function with inert SSH stubs.
sed -n '/^phase_code_update() {/,/^}/p' scripts/bridge-deploy.sh > "$tmp/function"
source "$tmp/function"
info() { :; }; step() { :; }; error_() { echo "$*" >&2; }
REMOTE_REPO=/repo
DEPLOY_REF=origin/codex/haiku55-prod-20261026
KOPPEL_ZIEL_SHA=""
DRY_RUN=false
rssh() {
    printf '%s\n' "$*" >> "$tmp/commands"
    case "$*" in
        *"git rev-parse HEAD"*) echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ;;
        *"git rev-parse 'origin/codex/"*) echo bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ;;
        *"git fetch origin"*) return 0 ;;
        *) echo "unexpected SSH: $*" >&2; return 1 ;;
    esac
}
dry_rssh() { printf '%s\n' "$*" >> "$tmp/commands"; }
phase_foreign_commit_gate() { echo gate >> "$tmp/commands"; return "${gate_rc:-0}"; }
phase_code_update test-host
grep -q "git rev-parse 'origin/codex/haiku55-prod-20261026'" "$tmp/commands"
grep -q "git merge --ff-only 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'" "$tmp/commands"
grep -q '^gate$' "$tmp/commands"
: > "$tmp/commands"
gate_rc=1
if phase_code_update test-host; then echo "foreign gate ignored"; exit 1; fi
if grep -q 'git merge' "$tmp/commands"; then echo "merged after gate refusal"; exit 1; fi
echo "PASS explicit release ref, pinned fast-forward, foreign gate preserved"
# Invalid refs must fail before locks, fetches or any host operation.
if DEPLOY_REF="origin/x;echo bad" bash scripts/bridge-deploy.sh hetzner > "$tmp/invalid" 2>&1; then exit 1; fi
grep -q 'Invalid DEPLOY_REF' "$tmp/invalid"
echo "PASS unsafe ref rejected"
