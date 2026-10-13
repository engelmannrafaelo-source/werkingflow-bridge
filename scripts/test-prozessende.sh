#!/bin/bash
# The *_prozessende test modules, each in its own pytest process (BRUMBAU2R
# P2-5). They need the real claude_code_sdk and real process ends; a mixed
# run leaves them out (tests/conftest.py) and points here. Extra arguments go
# to every pytest call. Exit: 0 only if every module passed; otherwise the
# first non-zero pytest code, after all modules ran.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 2
shopt -s nullglob
module=(tests/unit/*_prozessende.py tests/*_prozessende.py tests/*/*_prozessende.py)
mapfile -t module < <(printf '%s\n' "${module[@]}" | sort -u)
if (( ${#module[@]} == 0 )); then
    echo "PROZESSENDE: no *_prozessende.py module found — nothing proven" >&2
    exit 2
fi
rc=0
for m in "${module[@]}"; do
    echo "=== ${m}"
    python -m pytest "$m" "$@"
    code=$?
    (( code != 0 && rc == 0 )) && rc=$code
    echo "=== ${m}: exit ${code}"
done
exit "$rc"
