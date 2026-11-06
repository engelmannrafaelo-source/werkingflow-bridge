#!/usr/bin/env bash
# Preserve the deploy hook name; repair belongs to the existing deploy owner.
# No automatic account rotation, model start or competing deploy session.
set -uo pipefail
exec python3 "$(dirname "${BASH_SOURCE[0]}")/report-infra-failure.py"
