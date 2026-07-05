#!/usr/bin/env python3
"""bridge_smoke_coverage.py — enforce that every Bridge endpoint is smoke-covered.

Parses the `@app.post/get(...)` route decorators out of src/main.py and asserts
that each `/v1/*` route is EITHER:
  - probed by bridge_smoke.py (PROBES / COVERED_ALIASES), OR
  - listed in bridge_smoke.EXCLUDED with an explicit reason.

A route that is neither is a coverage gap → exit 1. This is what stops the next
new endpoint from silently shipping untested (the exact failure mode that let
/v1/document/convert 415 for weeks with a green deploy).

Run standalone or wire into the bridge pre-push / CI gate:
    python3 scripts/bridge_smoke_coverage.py
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MAIN = os.path.join(REPO, "src", "main.py")

sys.path.insert(0, HERE)
import bridge_smoke  # noqa: E402

ROUTE_RE = re.compile(r'@app\.(?:post|get|put|delete)\("(/v1/[^"]+)"')

# Non-functional / internal /v1 prefixes we never gate on (documented once here).
IGNORE_PREFIXES = (
    "/v1/metrics/prompt-performance",  # dashboards, read-only observability
    "/v1/metrics/cc-usage",
    "/v1/metrics/request-log",
    "/v1/metrics/limiter-trajectory",
    "/v1/metrics/queue-forecast",
    "/v1/metrics/throughput",
    "/v1/metrics/upstream-health",
    "/v1/metrics/usage-breakdown",
    "/v1/metrics/attribution",
    "/v1/metrics/contract",
    "/v1/metrics/sandbox-observed-rate-limit",
    "/v1/cli-sessions/stats",
    "/v1/debug",
)


def discover_routes() -> set:
    with open(MAIN) as fh:
        src = fh.read()
    routes = set(ROUTE_RE.findall(src))
    return {r for r in routes if not r.startswith(IGNORE_PREFIXES)}


def covered_endpoints() -> set:
    probed = {p.endpoint for p in bridge_smoke.PROBES}
    aliased = set(bridge_smoke.COVERED_ALIASES.keys())
    excluded = set(bridge_smoke.EXCLUDED.keys())
    return probed | aliased | excluded


def main():
    routes = discover_routes()
    covered = covered_endpoints()
    gaps = sorted(r for r in routes if r not in covered)

    print(f"Bridge endpoint coverage: {len(routes) - len(gaps)}/{len(routes)} /v1 routes covered "
          f"(probed or explicitly excluded).")
    if gaps:
        print("\nUNCOVERED endpoints (add a probe to bridge_smoke.PROBES or an entry to EXCLUDED):", file=sys.stderr)
        for g in gaps:
            print(f"  - {g}", file=sys.stderr)
        sys.exit(1)
    print("COVERAGE_OK: every functional /v1 route is probed or excluded-with-reason.")
    sys.exit(0)


if __name__ == "__main__":
    main()
