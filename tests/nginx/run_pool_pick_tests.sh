#!/usr/bin/env bash
# Runs the pure-Lua pool ranking tests inside the same OpenResty base image the
# nginx-lb is built from, so the luajit doing the checking is the luajit that
# will run the code. No shdict / resty.http needed — pool_pick.lua has no nginx
# dependency, which is exactly why the policy lives there.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${OPENRESTY_IMAGE:-openresty/openresty:1.27.1.1-alpine}"

exec docker run --rm \
  -v "$REPO/docker/lua:/lua:ro" \
  -v "$REPO/tests/nginx:/tests:ro" \
  "$IMAGE" luajit /tests/test_pool_pick.lua
