-- Unit tests for account affinity (CACHE2): pool_pick.pick_with_affinity and
-- its use in pool_router.choose().
--
-- Run:  tests/nginx/run_pool_pick_tests.sh   (runs this file after test_pool_pick.lua)
--
-- The property under test: the same X-Cache-Affinity-Key lands on the same
-- account as long as that account passes the unchanged guards; when it does not,
-- the old weighted pick applies and the miss is said out loud (header value,
-- WARN line, decision label) — never silently.

package.path = "/lua/?.lua;" .. package.path
local pp = require("pool_pick")

local failures, checks = 0, 0
local function check(cond, msg)
    checks = checks + 1
    if not cond then
        failures = failures + 1
        io.write("  NOT OK: ", msg, "\n")
    end
end

local function acct(opts)
    return {
        worker = opts.worker,
        available = (opts.available ~= false),
        cooldown_remaining_s = opts.cooldown or 0,
        effective_cap_tokens = opts.cap or 400000,
        current_in_flight_tokens = opts.in_flight or 0,
        weekly_percent = opts.weekly or 20,
        usage_known = (opts.known ~= false),
    }
end

local function pool(over)
    local base = {
        engelmann = acct({ worker = "worker1" }),
        office    = acct({ worker = "worker2" }),
        gmail     = acct({ worker = "worker3" }),
        werking   = acct({ worker = "worker4" }),
    }
    for k, v in pairs(over or {}) do base[k] = v end
    return base
end

local function first() return 0 end
local function last() return 1 end

-- A key whose preferred account is `want` (searched, so the tests do not
-- depend on hard-coded hash values).
local function key_for(accounts, want)
    for i = 1, 10000 do
        local k = "job-" .. i
        if (pp.preferred_account(accounts, k)) == want then return k end
    end
    error("no key found for " .. want)
end

print("pool_affinity: hash")
check(pp.fnv1a32("") == 2166136261, "fnv1a32('') = 0x811c9dc5")
check(pp.fnv1a32("a") == 0xe40c292c, "fnv1a32('a') = 0xe40c292c, got " .. string.format("%x", pp.fnv1a32("a")))
check(pp.fnv1a32("foobar") == 0xbf9cf968, "fnv1a32('foobar') = 0xbf9cf968")

print("pool_affinity: same key, same account")
do
    local a = pool()
    local spread = {}
    for i = 1, 200 do
        local k = "lauf-" .. i
        local p1 = pp.preferred_account(a, k)
        local n, _, _, _, _, _, aff = pp.pick_with_affinity(a, 500, k, math.random)
        check(n == p1 and aff == "hit", "key " .. k .. " must hit its preferred account")
        local n2 = pp.pick_with_affinity(a, 500, k, last)
        check(n2 == p1, "key " .. k .. " must not depend on the random draw")
        spread[p1] = (spread[p1] or 0) + 1
    end
    local used = 0
    for _, c in pairs(spread) do
        used = used + 1
        check(c >= 20, "200 keys spread over the pool, one account got only " .. c)
    end
    check(used == 4, "200 keys must use all 4 accounts, used " .. used)
end

print("pool_affinity: preferred account is stable when ANOTHER account changes")
do
    local a = pool()
    local k = key_for(a, "gmail")
    local b = pool({ werking = acct({ worker = "worker4", available = false }) })
    local n, w, _, _, _, _, aff = pp.pick_with_affinity(b, 500, k, first)
    check(n == "gmail" and w == "worker3" and aff == "hit", "gmail must stay preferred, got " .. tostring(n))
end

print("pool_affinity: guards win, the miss is reported")
do
    local cases = {
        { "unavailable",  acct({ worker = "worker3", available = false }) },
        { "cooldown",     acct({ worker = "worker3", cooldown = 30 }) },
        { "no_capacity",  acct({ worker = "worker3", cap = 1000, in_flight = 900 }) },
        { "weekly_wall",  acct({ worker = "worker3", weekly = 97 }) },
        { "held_back_unmeasured", acct({ worker = "worker3", known = false }) },
    }
    for _, c in ipairs(cases) do
        local a = pool({ gmail = c[2] })
        local k = key_for(a, "gmail")
        for _, draw in ipairs({ first, last }) do
            local n, _, _, _, _, _, aff, pref = pp.pick_with_affinity(a, 500, k, draw)
            check(aff == "fallback:" .. c[1], c[1] .. ": affinity must be fallback:" .. c[1] .. ", got " .. tostring(aff))
            check(n ~= "gmail" and n ~= nil, c[1] .. ": the guarded account must not be picked, got " .. tostring(n))
            check(pref == "gmail", c[1] .. ": preferred must be reported")
            local plain = pp.pick_weighted_account(a, 500, draw)
            check(n == plain, c[1] .. ": fallback must be exactly the old weighted pick")
        end
    end
end

print("pool_affinity: unreachable worker is a fallback, not a guess")
do
    local a = pool()
    local k = key_for(a, "office")
    local n, _, _, _, _, _, aff = pp.pick_with_affinity(a, 500, k, first,
        function(w) return w ~= "worker2" end)
    check(aff == "fallback:unknown_worker" and n ~= "office", "worker_ok=false must fall back")
end

print("pool_affinity: all unmeasured → affinity still works (nothing measured to prefer)")
do
    local a = {
        sahori = acct({ worker = "worker-sahori", known = false }),
        kurt   = acct({ worker = "worker-kurt", known = false }),
    }
    local k = key_for(a, "kurt")
    local n, _, _, _, from_measured, _, aff = pp.pick_with_affinity(a, 500, k, first)
    check(n == "kurt" and aff == "hit" and from_measured == false, "unmeasured-only pool: hit on kurt")
end

print("pool_affinity: no key → exactly the old pick")
do
    local a = pool()
    for _, draw in ipairs({ first, last }) do
        local n, w, wt, tot, fm, _, aff, pref = pp.pick_with_affinity(a, 500, nil, draw)
        local n2, w2, wt2, tot2, fm2 = pp.pick_weighted_account(a, 500, draw)
        check(n == n2 and w == w2 and wt == wt2 and tot == tot2 and fm == fm2,
              "no key must equal pick_weighted_account")
        check(aff == nil and pref == nil, "no key → no affinity status")
    end
    local n = pp.pick_with_affinity(a, 500, "", first)
    check(n == pp.pick_weighted_account(a, 500, first), "empty key == no key")
end

print("pool_affinity: nothing eligible → nil, with the reason")
do
    local a = pool({
        engelmann = acct({ worker = "worker1", available = false }),
        office    = acct({ worker = "worker2", available = false }),
        gmail     = acct({ worker = "worker3", available = false }),
        werking   = acct({ worker = "worker4", available = false }),
    })
    local n, _, _, _, _, _, aff = pp.pick_with_affinity(a, 500, "x", first)
    check(n == nil and aff == "fallback:unavailable", "exhausted pool → nil + fallback:unavailable")
end

-- ---------------------------------------------------------------------------
-- pool_router.choose() with a stub ngx: header, upstream variable, log
-- ---------------------------------------------------------------------------
print("pool_router: affinity variables, header and log")

local STATE
local logs
package.loaded["cjson.safe"] = {
    decode = function(raw) if raw == "body" then return STATE.body end; return STATE end,
    encode = function() return "{}" end,
}
package.loaded["resty.http"] = {}

local function run_router(headers, state, stale)
    STATE = state
    logs = {}
    local store = { state = "x", ts = stale and "-100" or "0" }
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
        req = { read_body = function() end, get_body_data = function() return "body" end },
        var = {
            uri = headers.uri,
            request_length = "100",
            http_x_cache_affinity_key = headers.key,
            affinity_upstream = "claude_workers",
            x_cache_affinity = "",
        },
    }
    package.loaded["pool_router"] = nil
    local M = dofile("/lua/pool_router.lua")
    M.choose({ overflow_capable = false })
    return ngx.var
end

local function logged(level, needle)
    for _, l in ipairs(logs) do
        if l[1] == level and l[2]:find(needle, 1, true) then return true end
    end
    return false
end

do
    local st = { accounts = pool() }
    local k = key_for(st.accounts, "werking")
    local v = run_router({ key = k }, st)
    check(v.affinity_upstream == "claude_workers_affin_worker4",
          "hit must name the preferred worker's group, got " .. tostring(v.affinity_upstream))
    check(v.x_cache_affinity == "hit", "header value must be hit")
    check(v.x_pool_decision == "affinity", "decision must be affinity, got " .. tostring(v.x_pool_decision))
    check(v.target_worker == "worker4", "target_worker must be worker4")
end

do
    local st = { accounts = pool({ werking = acct({ worker = "worker4", cap = 100, in_flight = 0 }) }) }
    local k = key_for(st.accounts, "werking")
    local v = run_router({ key = k }, st)
    check(v.affinity_upstream == "claude_workers", "fallback must keep the old pool")
    check(v.x_cache_affinity == "fallback:no_capacity", "header must carry the reason, got " .. tostring(v.x_cache_affinity))
    check(v.x_pool_decision == "weighted_capacity+affinity_fallback", "decision label, got " .. tostring(v.x_pool_decision))
    check(logged(1, "AFFINITY LOST") and logged(1, "fallback:no_capacity"), "fallback must be logged at WARN with the reason")
end

do
    local v = run_router({ key = nil }, { accounts = pool() })
    check(v.affinity_upstream == "claude_workers" and v.x_cache_affinity == "", "no key: old pool, empty header")
    check(v.x_pool_decision == "weighted_capacity", "no key: decision unchanged")
    check(not logged(1, "AFFINITY"), "no key: no affinity log line")
end

do
    local v = run_router({ key = "job-1" }, { accounts = pool() }, true)
    check(v.x_cache_affinity == "fallback:state_stale" and v.affinity_upstream == "claude_workers",
          "stale state: fallback:state_stale, old pool")
    check(v.x_pool_decision == "round_robin_fallback", "stale state: decision unchanged")
end

do
    local v = run_router({ key = string.rep("k", 300) }, { accounts = pool() })
    check(v.x_cache_affinity == "fallback:key_too_long" and v.affinity_upstream == "claude_workers",
          "overlong key: fallback, old pool")
end

do
    local empty = pool()
    for _, account in pairs(empty) do account.available = false end
    local st = { accounts = empty, api_reserve = { state = "available", active_on_this_bridge = true,
        automatic_paid_fallback = false, available = true }, body = {messages = {}, enable_tools = false} }
    local v = run_router({uri = "/v1/chat/completions"}, st)
    check(v.x_pool_decision == "api_reserve_candidate", "empty subscriptions must reach reserve admission")
    st.body.session_id = "existing-session"
    v = run_router({uri = "/v1/chat/completions"}, st)
    check(v.target_worker == "unavailable", "continued sessions must not use reserve")
    st.body.session_id = nil; st.body.enable_tools = true
    v = run_router({uri = "/v1/chat/completions"}, st)
    check(v.target_worker == "unavailable", "tool calls must not use reserve")
    st.body.enable_tools = false; st.api_reserve.state = "unavailable"
    v = run_router({uri = "/v1/chat/completions"}, st)
    check(v.target_worker == "unavailable", "unknown reserve must not claim capacity")
end

print(string.format("pool_affinity: %d checks, %d failures", checks, failures))
if failures > 0 then os.exit(1) end
print("POOL_AFFINITY_OK")
