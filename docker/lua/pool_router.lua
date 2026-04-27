-- pool_router.lua — Bridge nginx account-aware pool router
--
-- PHASE C (LIVE): route() is called from balancer_by_lua_block in upstream
-- claude_workers. Picks the worker whose Anthropic account has the most
-- headroom (token-budget aware) and sets balancer.set_current_peer().
--
-- Decision logic:
--   1. Refresh timer reads /v1/metrics/account-pool-state every 10s
--   2. Per request: estimates input tokens from request_length
--   3. Skips accounts: session >= 95%, cooldown > 0, headroom <= est
--   4. State stale (>30s) → random worker (graceful degradation)
--   5. All accounts exhausted → bogus peer → triggers @bridge_full 503

local cjson = require "cjson.safe"
local http  = require "resty.http"

local shared = ngx.shared.pool_state

local METRICS_URL        = "http://metrics-reader:8000"
local REFRESH_INTERVAL_S = 10
local STALE_THRESHOLD_S  = 30

-- Account name → worker upstream name (must match docker-compose service names)
local ACCOUNT_WORKER = {
    engelmann = "worker1",
    office    = "worker2",
    gmail     = "worker3",
    werking   = "worker4",
}

-- ---------------------------------------------------------------------------
-- Token estimation (mirrors adaptive_limiter.estimate_request_tokens)
-- ---------------------------------------------------------------------------
local function estimate_tokens(body)
    if not body or #body == 0 then return 500 end
    local ok, parsed = pcall(cjson.decode, body)
    if not ok or type(parsed) ~= "table" then
        return math.max(100, math.floor(#body / 4))
    end
    local chars = 0
    local sys = parsed.system
    if type(sys) == "string" then
        chars = chars + #sys
    elseif type(sys) == "table" then
        for _, blk in ipairs(sys) do
            if type(blk) == "table" and type(blk.text) == "string" then
                chars = chars + #blk.text
            end
        end
    end
    local msgs = parsed.messages
    if type(msgs) == "table" then
        for _, m in ipairs(msgs) do
            if type(m) == "table" then
                local c = m.content
                if type(c) == "string" then
                    chars = chars + #c
                elseif type(c) == "table" then
                    for _, blk in ipairs(c) do
                        if type(blk) == "table" and type(blk.text) == "string" then
                            chars = chars + #blk.text
                        end
                    end
                end
            end
        end
    end
    return math.max(100, math.floor(chars / 4))
end

-- ---------------------------------------------------------------------------
-- Pool state refresh (runs in background timer every REFRESH_INTERVAL_S)
-- ---------------------------------------------------------------------------
local function refresh_pool_state()
    local httpc = http.new()
    httpc:set_timeout(2000)
    local res, err = httpc:request_uri(METRICS_URL .. "/v1/metrics/account-pool-state", {
        method  = "GET",
        headers = { ["Accept"] = "application/json" },
    })
    if not res or res.status ~= 200 then
        ngx.log(ngx.WARN, "pool_router: metrics-reader unreachable: ",
                err or tostring(res and res.status))
        return
    end
    local ok, data = pcall(cjson.decode, res.body)
    if not ok or type(data) ~= "table" or type(data.accounts) ~= "table" then
        ngx.log(ngx.WARN, "pool_router: invalid JSON from metrics-reader")
        return
    end
    local ok2, encoded = pcall(cjson.encode, data)
    if ok2 then
        shared:set("state", encoded)
        shared:set("ts", ngx.now())
    end
end

-- ---------------------------------------------------------------------------
-- Account selection: highest headroom, no cooldown, session < 95%
-- ---------------------------------------------------------------------------
local function choose_best_account(accounts, est_tokens)
    local best_account = nil
    local best_headroom = -1
    for name, info in pairs(accounts) do
        local session_pct       = tonumber(info.session_percent) or 100
        local cooldown          = tonumber(info.cooldown_remaining_s) or 0
        local headroom          = tonumber(info.headroom_tokens) or 0
        local available         = info.available
        if available
            and session_pct < 95
            and cooldown == 0
            and headroom > (est_tokens or 0)
            and headroom > best_headroom
        then
            best_headroom  = headroom
            best_account   = name
        end
    end
    return best_account, best_headroom
end

-- ---------------------------------------------------------------------------
-- Public API
-- ---------------------------------------------------------------------------
local M = {}

function M.init()
    -- Initial fetch (best-effort, non-blocking)
    local ok, err = ngx.timer.at(0, function(premature)
        if premature then return end
        local ok2, e2 = pcall(refresh_pool_state)
        if not ok2 then ngx.log(ngx.ERR, "pool_router: initial fetch error: ", e2) end
    end)
    if not ok then ngx.log(ngx.WARN, "pool_router: initial timer failed: ", err) end

    -- Recurring refresh timer
    local ok3, err3 = ngx.timer.every(REFRESH_INTERVAL_S, function(premature)
        if premature then return end
        local ok4, e4 = pcall(refresh_pool_state)
        if not ok4 then ngx.log(ngx.ERR, "pool_router: refresh error: ", e4) end
    end)
    if not ok3 then ngx.log(ngx.ERR, "pool_router: failed to start timer: ", err3) end

    ngx.log(ngx.INFO, "pool_router: shadow-mode initialized (refresh=",
            REFRESH_INTERVAL_S, "s, stale_threshold=", STALE_THRESHOLD_S, "s)")
end


-- Live routing — called from balancer_by_lua_block in upstream claude_workers.
-- Cannot read body, cannot ngx.exit(); only set_current_peer() is allowed here.
-- For exhaustion fallback: set bogus peer → upstream returns 502 → nginx error_page
-- routes to @bridge_full which emits canonical 429 envelope with Retry-After.
function M.route()
    local balancer = require "ngx.balancer"

    local state_str = shared:get("state")
    local state_ts  = tonumber(shared:get("ts")) or 0
    local age       = ngx.now() - state_ts

    -- Stale state: round-robin-style random fallback across all 4 workers
    if not state_str or age > STALE_THRESHOLD_S then
        local workers = {"worker1", "worker2", "worker3", "worker4"}
        local pick = workers[math.random(1, 4)]
        local ok, err = balancer.set_current_peer(pick, 8000)
        if not ok then
            ngx.log(ngx.ERR, "pool_router stale-fallback failed: ", err)
        end
        ngx.log(ngx.WARN, "pool_router: state stale (",
                string.format("%.0f", age), "s) — random fallback to ", pick)
        return
    end

    local ok_d, data = pcall(cjson.decode, state_str)
    if not ok_d or type(data) ~= "table" or type(data.accounts) ~= "table" then
        balancer.set_current_peer("worker1", 8000)
        ngx.log(ngx.ERR, "pool_router: invalid cached state, fallback worker1")
        return
    end

    -- Empty accounts table = workers don't have /v1/metrics/account-pool-state
    -- endpoint yet (deploy lag) → graceful degrade to random fallback
    local has_any = false
    for _ in pairs(data.accounts) do has_any = true; break end
    if not has_any then
        local workers = {"worker1", "worker2", "worker3", "worker4"}
        local pick = workers[math.random(1, 4)]
        balancer.set_current_peer(pick, 8000)
        ngx.log(ngx.WARN, "pool_router: empty accounts state — random fallback to ", pick)
        return
    end

    -- balancer phase cannot read body; estimate from Content-Length header
    -- (request_length includes headers; close enough for sizing purposes)
    local req_len   = tonumber(ngx.var.request_length) or 1000
    local est_tokens = math.max(500, math.floor(req_len / 4))

    local best, best_headroom = choose_best_account(data.accounts, est_tokens)

    if not best then
        -- All accounts exhausted: route to bogus peer → upstream 502
        -- → @bridge_full handler returns canonical "bridge capacity full" 429
        ngx.log(ngx.WARN, "pool_router: all accounts exhausted (est_tokens=",
                est_tokens, "), routing to bogus peer for @bridge_full handoff")
        balancer.set_current_peer("0.0.0.1", 1)
        return
    end

    local worker = ACCOUNT_WORKER[best] or "worker1"
    local ok_b, err = balancer.set_current_peer(worker, 8000)
    if not ok_b then
        ngx.log(ngx.ERR, "pool_router: set_current_peer failed for ", worker,
                ": ", err, " — falling back to worker1")
        balancer.set_current_peer("worker1", 8000)
        return
    end

    ngx.log(ngx.INFO, "pool_router: routed_to=", best, "/", worker,
            " headroom=", best_headroom, " est_tokens=", est_tokens,
            " state_age_s=", string.format("%.1f", age))
end

return M
