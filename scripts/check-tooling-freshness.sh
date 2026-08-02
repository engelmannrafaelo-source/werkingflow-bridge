#!/usr/bin/env bash
# ============================================================================
# check-tooling-freshness.sh — refuse to gate a deploy with stale tooling
# ============================================================================
# bridge-deploy.sh fast-forwards the TARGET HOST to origin/develop (Phase 2),
# but the smoke that decides whether that code may stay (Phase 5) runs from the
# checkout the operator happened to invoke:
#
#     smoke_script="$(dirname "${BASH_SOURCE[0]}")/bridge_smoke.py"
#
# Nothing kept those two in step. An old checkout therefore judges current code
# by an old verdict, and a false FAIL routes straight into auto-rollback: healthy
# code is reverted because the judge was out of date — and the deploy "fails" for
# a reason that no longer exists in the code it just tested.
#
# Two commits moved that verdict:
#   f32c801 (2026-07-29)  pool-capacity 429 is state, not a broken endpoint
#   8092057 (2026-07-31)  two further probes (9 -> 11)
# A checkout predating them reports SMOKE_FAIL for a Bridge that is healthy and
# merely rate-limited. That is the exact shape of the 2026-08-02 red run
# (9 probes, research 429 -> rollback), re-verified afterwards as 11/11 green
# with three of four accounts sitting at their weekly wall.
#
# Policy:
#   behind origin/develop  -> FATAL. It can only weaken the gate: Phase 2 pulls
#                             origin/develop on the host regardless, so this
#                             checkout cannot change WHAT ships — only how well
#                             it is judged. Refusing costs nothing and prevents
#                             a false rollback of good commits.
#   ahead / dirty tooling  -> WARN. Deploying from a checkout carrying unpushed
#                             work is a legitimate workflow, and a guard that
#                             cries wolf gets ignored — the same reasoning
#                             phase_foreign_commit_gate states for its warning.
#
# Exit codes:
#   0  fresh (warnings possible) — clear to deploy
#   1  behind the deploy ref — must not gate a deploy
#   2  freshness undecidable (not a checkout, fetch failed, ref unresolvable)
#
# Usage: check-tooling-freshness.sh [checkout-dir]    (default: this script's dir)
# ============================================================================
set -euo pipefail

DEPLOY_REF="${DEPLOY_REF:-origin/develop}"
FETCH_TIMEOUT="${FETCH_TIMEOUT:-30}"

log()    { printf '[%s %s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" "${*:2}"; }
info()   { log "INFO " "$@"; }
warn()   { log "WARN " "$@" >&2; }
error_() { log "ERROR" "$@" >&2; }

target_dir="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

repo_dir=""
if ! repo_dir="$(git -C "$target_dir" rev-parse --show-toplevel 2>/dev/null)"; then
    error_ "Not a git checkout: ${target_dir}"
    error_ "  Then nothing can establish whether bridge-deploy.sh and bridge_smoke.py are"
    error_ "  current, and 'probably fine' is precisely the assumption this check exists"
    error_ "  to refuse. Run the deploy from a checkout of the Bridge repository."
    exit 2
fi

fetch_err=""
if ! fetch_err="$(timeout "$FETCH_TIMEOUT" git -C "$repo_dir" fetch origin 2>&1)"; then
    error_ "git fetch origin failed in ${repo_dir} (timeout ${FETCH_TIMEOUT}s):"
    while IFS= read -r line; do [[ -n "$line" ]] && error_ "    ${line}"; done <<< "$fetch_err"
    error_ "  Freshness cannot be established. Assuming current is the failure mode this"
    error_ "  check prevents — fix connectivity to origin and re-run."
    exit 2
fi

if ! git -C "$repo_dir" rev-parse --verify --quiet "${DEPLOY_REF}^{commit}" >/dev/null; then
    error_ "${DEPLOY_REF} does not resolve in ${repo_dir} — cannot compare against the"
    error_ "  ref the host is deployed from. Check the remote configuration."
    exit 2
fi

head_sha="$(git -C "$repo_dir" rev-parse HEAD)"
ref_sha="$(git -C "$repo_dir" rev-parse "$DEPLOY_REF")"
behind="$(git -C "$repo_dir" rev-list --count "HEAD..${DEPLOY_REF}")"
ahead="$(git -C "$repo_dir" rev-list --count "${DEPLOY_REF}..HEAD")"

# The two files that actually decide a deploy's fate. Uncommitted edits here mean
# what runs is not what any SHA describes — worth saying out loud, not worth
# blocking on: editing the smoke while chasing a red deploy is normal work.
dirty="$(git -C "$repo_dir" status --porcelain -- scripts/bridge-deploy.sh scripts/bridge_smoke.py 2>/dev/null || true)"
if [[ -n "$dirty" ]]; then
    warn "Uncommitted changes in the deploy tooling — what runs is not what is committed:"
    while IFS= read -r line; do [[ -n "$line" ]] && warn "    ${line}"; done <<< "$dirty"
fi

if (( behind > 0 )); then
    error_ "ABORTED: deploy tooling is ${behind} commit(s) behind ${DEPLOY_REF}."
    error_ "  Checkout: ${repo_dir}"
    error_ "  HEAD ${head_sha:0:7}   ${DEPLOY_REF} ${ref_sha:0:7}"
    error_ "  The host is fast-forwarded to ${DEPLOY_REF}, so this checkout cannot change"
    error_ "  WHAT ships — only how well it is judged. A smoke older than the code it"
    error_ "  tests can fail a healthy Bridge and auto-roll-back good commits."
    error_ "  Missing here:"
    git -C "$repo_dir" log --no-merges --format='      %h %ad %s' --date=short \
        "HEAD..${DEPLOY_REF}" 2>/dev/null | head -10 >&2 || true
    error_ "  Fix:  git -C ${repo_dir} pull --ff-only origin develop"
    error_ "  Nothing was contacted or changed on any deploy target."
    exit 1
fi

if (( ahead > 0 )); then
    warn "Deploy tooling is ${ahead} commit(s) AHEAD of ${DEPLOY_REF} (unpushed work)."
    warn "  The host deploys ${DEPLOY_REF}; probes that exist only here may test routes"
    warn "  the deployed build does not have. Proceeding — this is not an error."
    info "Tooling freshness: no missing commits — clear to deploy"
    exit 0
fi

info "Tooling freshness: ${repo_dir} at ${head_sha:0:7} matches ${DEPLOY_REF} — clear"
exit 0
