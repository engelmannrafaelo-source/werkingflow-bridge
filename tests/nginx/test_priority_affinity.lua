-- Unit tests for the X-Priority pool (CACHE2N): pool_router.priority_pool()
-- and priority_pool_header_filter() with a stub ngx.
--
-- Run:  tests/nginx/run_pool_pick_tests.sh
--
-- The property under test: a request that carries X-Cache-Affinity-Key but runs
-- through @production_streaming / @production_jobs (no choose(), no account
-- pick) is never silently treated like an affinity hit. Without the key nothing
-- is set and nothing is logged.

package.path = "/lua/?.lua;" .. package.path

local failures, checks = 0, 0
local function check(cond, msg)
    checks = checks + 1
    if not cond then
        failures = failures + 1
        io.write("  NOT OK: ", msg, "\n")
    end
end

package.loaded["cjson.safe"] = { decode = function() return nil end, encode = function() return "{}" end }
package.loaded["resty.http"] = {}

local logs, store, resp_headers

-- var: request variables; returns the router module with a fresh stub ngx
local function router(var)
    logs, store, resp_headers = {}, {}, {}
    ngx = {
        now = function() return 0 end, INFO = 0, WARN = 1, ERR = 2,
        log = function(lvl, ...) logs[#logs + 1] = { lvl, table.concat({ ... }) } end,
        timer = { every = function() end, at = function() end },
        worker = { id = function() return 1 end },
        shared = { pool_state = {
            get = function(_, k) return store[k] end,
            set = function(_, k, v) store[k] = v end,
            incr = function(_, k, n, init) store[k] = (store[k] or init or 0) + n; return store[k] end,
        } },
        var = var,
        header = resp_headers,
    }
    package.loaded["pool_router"] = nil
    return dofile("/lua/pool_router.lua")
end

local function logged(level, needle)
    for _, l in ipairs(logs) do
        if l[1] == level and l[2]:find(needle, 1, true) then return true end
    end
    return false
end

print("priority_pool: production topology (mode bypass)")
do
    local var = { http_x_cache_affinity_key = "job-1", x_cache_affinity = "" }
    local M = router(var)
    M.priority_pool("bypass")
    check(var.x_cache_affinity == "bypass:priority_pool",
          "key on the priority pool → header bypass:priority_pool, got " .. tostring(var.x_cache_affinity))
    check(logged(1, "AFFINITY BYPASSED") and logged(1, "bypass:priority_pool"),
          "bypass must be logged at WARN with the reason")
    check(store.affinity_bypass == 1 and store.affinity_last_bypass == "bypass:priority_pool",
          "bypass must be counted for /internal/pool-router/state")
    check(var.affinity_upstream == nil, "bypass must not touch the upstream")
end

do
    local var = { http_x_cache_affinity_key = nil, x_cache_affinity = "" }
    local M = router(var)
    M.priority_pool("bypass")
    check(var.x_cache_affinity == "", "no key → no header")
    check(#logs == 0 and store.affinity_bypass == nil, "no key → no log, no counter")
end

do
    local var = { http_x_cache_affinity_key = string.rep("k", 300), x_cache_affinity = "" }
    local M = router(var)
    M.priority_pool("bypass")
    check(var.x_cache_affinity == "fallback:key_too_long", "overlong key keeps its own reason")
end

do
    local var = { http_x_cache_affinity_key = "job-1", x_cache_affinity = "" }
    local M = router(var)
    M.priority_pool("something-else")
    check(var.x_cache_affinity == "bypass:priority_pool" and logged(2, "unknown $priority_pool_affinity"),
          "unknown mode → ERR line and treated as bypass (never silent)")
end

do  -- the header filter is for mode peer only
    local var = { http_x_cache_affinity_key = "job-1", x_cache_affinity = "bypass:priority_pool" }
    local M = router(var)
    M.priority_pool_header_filter("bypass")
    check(resp_headers["X-Cache-Affinity"] == nil and #logs == 0, "header filter is inert in mode bypass")
end

print("priority_pool: primary topology (mode peer)")
do
    local var = { http_x_cache_affinity_key = "job-1", x_cache_affinity = "" }
    local M = router(var)
    M.priority_pool("peer")
    check(var.x_cache_affinity == "" and #logs == 0, "peer: access phase leaves the decision to the peer")
end

do
    local var = { http_x_cache_affinity_key = "job-1", upstream_http_x_cache_affinity = "hit",
                  upstream_addr = "192.0.2.10:8000" }
    local M = router(var)
    M.priority_pool_header_filter("peer")
    check(resp_headers["X-Cache-Affinity"] == nil and #logs == 0,
          "peer stated its decision → passed through untouched, no log")
end

do
    local var = { http_x_cache_affinity_key = "job-1", upstream_http_x_cache_affinity = nil,
                  upstream_addr = "172.18.0.5:8000" }
    local M = router(var)
    M.priority_pool_header_filter("peer")
    check(resp_headers["X-Cache-Affinity"] == "bypass:peer_without_affinity",
          "answer without the peer's decision → bypass:peer_without_affinity")
    check(logged(1, "AFFINITY BYPASSED") and logged(1, "172.18.0.5:8000"),
          "WARN names the upstream that answered")
end

do
    local var = { http_x_cache_affinity_key = nil, upstream_http_x_cache_affinity = nil }
    local M = router(var)
    M.priority_pool_header_filter("peer")
    check(resp_headers["X-Cache-Affinity"] == nil and #logs == 0, "peer without key → nothing")
end

print(string.format("priority_affinity: %d checks, %d failures", checks, failures))
if failures > 0 then os.exit(1) end
print("PRIORITY_AFFINITY_OK")
