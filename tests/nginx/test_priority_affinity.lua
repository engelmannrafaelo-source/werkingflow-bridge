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

local STATE
package.loaded["cjson.safe"] = { decode = function() return STATE end, encode = function() return "{}" end }
package.loaded["resty.http"] = {}

local logs, store, resp_headers

-- var: request variables; state: pool state (optional); stale: state too old
-- returns the router module with a fresh stub ngx
local function router(var, state, stale)
    logs, store, resp_headers = {}, {}, {}
    STATE = state
    if state then store.state = "x"; store.ts = stale and "-100" or "0" end
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

-- ---------------------------------------------------------------------------
-- Mode "affinity" (CACHE2N B1b, prepared; generator PRIORITY_AFFINITY=1)
-- ---------------------------------------------------------------------------
print("priority_pool: mode affinity (prepared, production only)")
local pp = require("pool_pick")
local function acct(worker, opts)
    opts = opts or {}
    return { worker = worker, available = (opts.available ~= false), cooldown_remaining_s = 0,
             effective_cap_tokens = opts.cap or 400000, current_in_flight_tokens = 0,
             weekly_percent = opts.weekly or 20, usage_known = true }
end
local function pool(over)
    local a = { engelmann = acct("worker1"), office = acct("worker2"),
                gmail = acct("worker3"), werking = acct("worker4") }
    for k, v in pairs(over or {}) do a[k] = v end
    return a
end
local function key_for(accounts, want)
    for i = 1, 10000 do
        local k = "job-" .. i
        if (pp.preferred_account(accounts, k)) == want then return k end
    end
    error("no key for " .. want)
end
local function pvar(key)
    return { http_x_cache_affinity_key = key, x_cache_affinity = "",
             affinity_upstream = "claude_production", request_length = "100" }
end

do
    local st = { accounts = pool() }
    local var = pvar(key_for(st.accounts, "werking"))
    local M = router(var, st)
    M.priority_pool("affinity")
    check(var.affinity_upstream == "claude_workers_affin_worker4",
          "hit → preferred worker's group, got " .. tostring(var.affinity_upstream))
    check(var.x_cache_affinity == "hit" and store.affinity_hit == 1, "hit → header hit, counted")
    check(var.target_worker == nil, "priority pool sets no target_worker (no gate)")
end

do
    local st = { accounts = pool({ werking = acct("worker4", { cap = 100 }) }) }
    local var = pvar(key_for(st.accounts, "werking"))
    local M = router(var, st)
    M.priority_pool("affinity")
    check(var.affinity_upstream == "claude_production", "no capacity → claude_production unchanged")
    check(var.x_cache_affinity == "fallback:no_capacity", "no capacity → header carries the reason")
    check(logged(1, "AFFINITY LOST") and logged(1, "priority pool"), "fallback logged at WARN")
end

do  -- weekly wall and unavailable are guards too, never overridden by the key
    for _, case in ipairs({ { { weekly = 97 }, "fallback:weekly_wall" },
                            { { available = false }, "fallback:unavailable" } }) do
        local st = { accounts = pool({ werking = acct("worker4", case[1]) }) }
        local var = pvar(key_for(st.accounts, "werking"))
        local M = router(var, st)
        M.priority_pool("affinity")
        check(var.affinity_upstream == "claude_production" and var.x_cache_affinity == case[2],
              "guard " .. case[2] .. " → old pool, got " .. tostring(var.x_cache_affinity))
    end
end

do  -- whole pool exhausted: the priority pool must NOT start rejecting
    local st = { accounts = pool({ engelmann = acct("worker1", { available = false }),
                                   office = acct("worker2", { available = false }),
                                   gmail = acct("worker3", { available = false }),
                                   werking = acct("worker4", { available = false }) }) }
    local var = pvar("job-1")
    local M = router(var, st)
    M.priority_pool("affinity")
    check(var.affinity_upstream == "claude_production" and var.target_worker == nil,
          "exhausted pool → claude_production with its dev-bridge overflow, no reject")
end

do
    local var = pvar("job-1")
    local M = router(var, { accounts = pool() }, true)
    M.priority_pool("affinity")
    check(var.x_cache_affinity == "fallback:state_stale" and var.affinity_upstream == "claude_production",
          "stale state → fallback:state_stale, old pool")
end

do
    local var = pvar(nil)
    local M = router(var, { accounts = pool() })
    M.priority_pool("affinity")
    check(var.x_cache_affinity == "" and var.affinity_upstream == "claude_production" and #logs == 0,
          "no key → nothing changes, nothing logged")
end

do
    local var = pvar(string.rep("k", 300))
    local M = router(var, { accounts = pool() })
    M.priority_pool("affinity")
    check(var.x_cache_affinity == "fallback:key_too_long" and var.affinity_upstream == "claude_production",
          "overlong key → fallback, old pool")
end

print(string.format("priority_affinity: %d checks, %d failures", checks, failures))
if failures > 0 then os.exit(1) end
print("PRIORITY_AFFINITY_OK")
