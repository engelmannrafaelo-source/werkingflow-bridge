#!/usr/bin/env bash
# ============================================================================
# test_tooling_freshness.sh — proves check-tooling-freshness.sh
# ============================================================================
# The gate decides whether a deploy may be judged by this checkout's smoke.
# Getting it wrong in either direction is expensive: too lax and a stale smoke
# auto-rolls-back healthy code (2026-08-02), too strict and the guard gets
# routinely bypassed until it means nothing.
#
# Runs against throwaway repositories only — never touches a real checkout,
# never contacts a deploy target.
#
# Usage: tests/deploy/test_tooling_freshness.sh     (exit 0 = all cases hold)
# ============================================================================
set -euo pipefail

CHECKER="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scripts" && pwd)/check-tooling-freshness.sh"
[[ -x "$CHECKER" ]] || { echo "FAIL: not executable: $CHECKER"; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Isolated identity/config: the test must not depend on — or touch — the
# operator's git configuration.
G=(git -c user.email=test@example.invalid -c user.name=tooling-test
   -c commit.gpgsign=false -c init.defaultBranch=develop)

pass=0
fail=0
check() {  # name  expected_exit  dir
    local name="$1" want="$2" dir="$3" got=0
    "$CHECKER" "$dir" >/dev/null 2>&1 || got=$?
    if [[ "$got" == "$want" ]]; then
        printf '  ok    %-46s (exit %s)\n' "$name" "$got"
        pass=$((pass + 1))
    else
        printf '  FAIL  %-46s expected %s, got %s\n' "$name" "$want" "$got"
        fail=$((fail + 1))
    fi
}

# --- fixture: bare origin on develop, plus a working clone --------------------
"${G[@]}" init --bare -q "$tmp/origin.git"
git -C "$tmp/origin.git" symbolic-ref HEAD refs/heads/develop
"${G[@]}" clone -q "$tmp/origin.git" "$tmp/work" 2>/dev/null
mkdir -p "$tmp/work/scripts"
printf 'probe one\n' > "$tmp/work/scripts/bridge_smoke.py"
"${G[@]}" -C "$tmp/work" add scripts/bridge_smoke.py
"${G[@]}" -C "$tmp/work" commit -qm "base: smoke tooling"
"${G[@]}" -C "$tmp/work" push -q origin HEAD:develop
"${G[@]}" -C "$tmp/work" branch -q --set-upstream-to=origin/develop develop 2>/dev/null || true

echo "check-tooling-freshness.sh"

# 1. In sync with the deploy ref → the only unambiguously safe state.
check "in sync with origin/develop passes" 0 "$tmp/work/scripts"

# 2. Behind: another clone advances origin. This is the case that let a
#    pre-f32c801 smoke roll back a healthy Bridge — it must be fatal, and
#    fatal specifically with exit 1 (deploy aborts), not 2 (undecidable).
"${G[@]}" clone -q "$tmp/origin.git" "$tmp/other" 2>/dev/null
printf 'probe one\nprobe two\n' > "$tmp/other/scripts/bridge_smoke.py"
"${G[@]}" -C "$tmp/other" add scripts/bridge_smoke.py
"${G[@]}" -C "$tmp/other" commit -qm "smoke: add second probe"
"${G[@]}" -C "$tmp/other" push -q origin HEAD:develop
check "behind origin/develop is fatal" 1 "$tmp/work/scripts"

# 3. Caught up again → passes. Proves the failure in (2) was the staleness and
#    not something sticky about the fixture.
"${G[@]}" -C "$tmp/work" pull -q --ff-only origin develop
check "after pull --ff-only passes again" 0 "$tmp/work/scripts"

# 4. Ahead with unpushed work → warns, does not block. A deploy from a checkout
#    carrying local commits is legitimate; the host still ships origin/develop.
printf 'probe one\nprobe two\nlocal\n' > "$tmp/work/scripts/bridge_smoke.py"
"${G[@]}" -C "$tmp/work" add scripts/bridge_smoke.py
"${G[@]}" -C "$tmp/work" commit -qm "local: unpushed probe"
check "ahead of origin/develop still passes" 0 "$tmp/work/scripts"

# 5. Not a checkout at all → undecidable, never a silent pass.
mkdir -p "$tmp/plain"
check "non-repository is undecidable" 2 "$tmp/plain"

# 6. Unresolvable deploy ref → undecidable rather than a guessed comparison.
DEPLOY_REF="origin/does-not-exist" "$CHECKER" "$tmp/work/scripts" >/dev/null 2>&1 && got=0 || got=$?
if [[ "$got" == 2 ]]; then
    printf '  ok    %-46s (exit 2)\n' "unresolvable deploy ref is undecidable"
    pass=$((pass + 1))
else
    printf '  FAIL  %-46s expected 2, got %s\n' "unresolvable deploy ref is undecidable" "$got"
    fail=$((fail + 1))
fi

echo
if (( fail > 0 )); then
    echo "FAIL: ${fail} case(s) failed, ${pass} passed"
    exit 1
fi
echo "OK: ${pass}/${pass} cases passed"
exit 0
