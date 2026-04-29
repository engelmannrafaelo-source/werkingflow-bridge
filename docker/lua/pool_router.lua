-- pool_router.lua — Bridge nginx account-aware pool router
--
-- PHASE C (LIVE): choose() is called from access_by_lua_block in /v1/(chat/completions|research).
-- Distributes load across all eligible Anthropic accounts (token-budget aware).
-- Sets ngx.var.target_worker to one of: worker1, worker2, worker3, worker4, unavailable
-- Sets ngx.var.x_pool_decision to: highest_headroom | round_robin_fallback | all_unavail
--
-- Decision logic:
--   1. Refresh timer reads /v1/metrics/account-pool-state every 2s
--   2. Per request: estimates input tokens from request_length
--   3. Eligible = available AND session<95% AND cooldown=0 AND headroom>est_tokens
--   4. Pick eligible account with HIGHEST headroom_percent → pool_decision=highest_headroom
--   5. State stale (>10s) OR fetch/decode error → round-robin across all 4 workers
--      → pool_decision=round_robin_fallback
--   6. Zero eligible → bogus peer → triggers @bridge_full 429 envelope
--      → pool_decision=all_unavail

local cjson = require "cjson.safe"
local http  = require "resty.http"

local shared = ngx.shared.pool_state

local METRICS_URL        = "http://metrics-reader:8000"
local REFRESH_INTERVAL_S = 2
local STALE_THRESHOLD_S  = 10

-- Account name → worker upstream name (must match docker-compose service names)
local ACCOUNT_WORKER = {
    engelmann = "worker1",
    office    = "worker2",
    gmail     = "worker3",
    werking   = "worker4",
}

local WORKER_NAMES = {"worker1", "worker2", "worker3", "worker4"}

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
    httpc:set_timeout(5000)  -- 5s — robust against slow metrics-reader restarts
    local res, err = httpc:request_uri(METRICS_URL .. "/v1/metrics/account-pool-state", {
        method    = "GET",
        headers   = { ["Accept"] = "application/json" },
        keepalive = false,  -- no keepalive: avoids stale sockets after container recreation
    })
    -- ALWAYS close — every code path, no exception
    httpc:close()

    if not res then
        shared:set("last_refresh_status", "error")
        shared:set("last_refresh_err", tostring(err or "connection failed"))
        ngx.log(ngx.WARN, "pool_router: metrics-reader unreachable: ", tostring(err or "unknown"))
        return
    end
    if res.status ~= 200 then
        shared:set("last_refresh_status", "error")
        shared:set("last_refresh_err", "HTTP " .. tostring(res.status))
        ngx.log(ngx.WARN, "pool_router: metrics-reader HTTP ", res.status)
        return
    end

    local ok, data = pcall(cjson.decode, res.body)
    if not ok or type(data) ~= "table" or type(data.accounts) ~= "table" then
        shared:set("last_refresh_status", "error")
        shared:set("last_refresh_err", "invalid JSON from metrics-reader")
        ngx.log(ngx.WARN, "pool_router: invalid JSON from metrics-reader")
        return
    end

    local ok2, encoded = pcall(cjson.encode, data)
    if not ok2 then
        shared:set("last_refresh_status", "error")
        shared:set("last_refresh_err", "cjson encode failed")
        ngx.log(ngx.ERR, "pool_router: cjson encode failed")
        return
    end

    shared:set("state", encoded)
    shared:set("ts", ngx.now())   -- ONLY set ts on success
    shared:set("last_refresh_status", "ok")
    shared:set("last_refresh_err", "")
end

-- ---------------------------------------------------------------------------
-- Deterministic round-robin picker (atomic shared counter, 1-indexed)
-- Returns worker name (worker1..worker4)
-- ---------------------------------------------------------------------------
local function round_robin_worker()
    local idx = shared:incr("rr_counter", 1, 0)
    return WORKER_NAMES[((idx - 1) % 4) + 1]
end

-- ---------------------------------------------------------------------------
-- Increment per-worker decision counter (for /internal/pool-router/state)
-- ---------------------------------------------------------------------------
local function count_decision(worker)
    for i, w in ipairs(WORKER_NAMES) do
        if w == worker then
            shared:incr("rr_worker_count_" .. i, 1, 0)
            return
        end
    end
end

-- ---------------------------------------------------------------------------
-- Best-account picker: returns the eligible account with the highest
-- headroom_percent, plus that percent value.
-- Returns nil, -1 when all accounts are exhausted or ineligible.
--
-- Eligible = available AND session_percent<95 AND cooldown=0 AND
--            headroom_tokens > est_tokens
-- ---------------------------------------------------------------------------
local function pick_best_account(accounts, est_tokens)
    local best_name = nil
    local best_pct  = -1
    for name, info in pairs(accounts) do
        local session_pct  = tonumber(info.session_percent) or 100
        local cooldown     = tonumber(info.cooldown_remaining_s) or 0
        local headroom_tok = tonumber(info.headroom_tokens) or 0
        local headroom_pct = tonumber(info.headroom_percent) or 0
        local available    = info.available
        if available and session_pct < 95 and cooldown == 0
                and headroom_tok > (est_tokens or 0) then
            if headroom_pct > best_pct then
                best_pct  = headroom_pct
                best_name = name
            end
        end
    end
    return best_name, best_pct
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

    ngx.log(ngx.INFO, "pool_router: initialized (refresh=",
            REFRESH_INTERVAL_S, "s, stale_threshold=", STALE_THRESHOLD_S, "s)")
end


-- Live routing — called from access_by_lua_block in /v1/(chat/completions|research)
-- Sets ngx.var.target_worker and ngx.var.x_pool_decision
function M.choose()
    local state_str      = shared:get("state")
    local state_ts       = tonumber(shared:get("ts")) or 0
    local age            = ngx.now() - state_ts
    local refresh_status = shared:get("last_refresh_status") or "unknown"
    local refresh_err    = shared:get("last_refresh_err") or ""

    -- Stale state: deterministic round-robin fallback (never random — avoids clustering)
    if not state_str or age > STALE_THRESHOLD_S then
        local pick = round_robin_worker()
        count_decision(pick)
        ngx.var.target_worker   = pick
        ngx.var.x_pool_decision = "round_robin_fallback"
        ngx.log(ngx.WARN, "pool_router.choose: state stale",
                " state_age_s=", string.format("%.1f", age),
                " refresh_status=", refresh_status,
                " last_err=", refresh_err,
                " — round_robin_fallback to ", pick)
        return
    end

    local ok_d, data = pcall(cjson.decode, state_str)
    if not ok_d or type(data) ~= "table" or type(data.accounts) ~= "table" then
        local pick = round_robin_worker()
        count_decision(pick)
        ngx.var.target_worker   = pick
        ngx.var.x_pool_decision = "round_robin_fallback"
        ngx.log(ngx.ERR, "pool_router.choose: invalid cached state",
                " state_age_s=", string.format("%.1f", age),
                " refresh_status=", refresh_status,
                " last_err=", refresh_err,
                " — round_robin_fallback to ", pick)
        return
    end

    -- Empty accounts (workers not yet exposing metrics endpoint)
    local has_any = false
    for _ in pairs(data.accounts) do has_any = true; break end
    if not has_any then
        local pick = round_robin_worker()
        count_decision(pick)
        ngx.var.target_worker   = pick
        ngx.var.x_pool_decision = "round_robin_fallback"
        ngx.log(ngx.WARN, "pool_router.choose: empty accounts",
                " state_age_s=", string.format("%.1f", age),
                " refresh_status=", refresh_status,
                " — round_robin_fallback to ", pick)
        return
    end

    -- Token estimation from Content-Length (access phase: request_length is available)
    local req_len    = tonumber(ngx.var.request_length) or 1000
    local est_tokens = math.max(500, math.floor(req_len / 4))

    local picked, picked_pct = pick_best_account(data.accounts, est_tokens)

    if not picked then
        -- All accounts exhausted → bogus upstream → @bridge_full emits 429
        ngx.var.target_worker   = "unavailable"
        ngx.var.x_pool_decision = "all_unavail"
        ngx.log(ngx.WARN, "pool_router.choose: all_unavail",
                " est_tokens=", est_tokens,
                " state_age_s=", string.format("%.1f", age),
                " refresh_status=", refresh_status)
        return
    end

    -- Route to account with highest headroom_percent
    local worker = ACCOUNT_WORKER[picked] or "worker1"
    count_decision(worker)
    ngx.var.target_worker   = worker
    ngx.var.x_pool_decision = "highest_headroom"
    ngx.log(ngx.INFO, "pool_router.choose: routed_to=", worker,
            " reason=highest_headroom_", string.format("%.0f", picked_pct), "pct",
            " account=", picked,
            " est_tokens=", est_tokens,
            " state_age_s=", string.format("%.1f", age))
end

-- ---------------------------------------------------------------------------
-- Internal state endpoint: called from content_by_lua_block
-- in location = /internal/pool-router/state (Docker-network-only access)
-- ---------------------------------------------------------------------------
function M.state_handler()
    local now            = ngx.now()
    local state_ts       = tonumber(shared:get("ts")) or 0
    local state_age_s    = now - state_ts
    local refresh_status = shared:get("last_refresh_status") or "unknown"
    local refresh_err    = shared:get("last_refresh_err") or ""
    local state_str      = shared:get("state") or "{}"

    -- Decision counters per worker
    local counters = {}
    for i, w in ipairs(WORKER_NAMES) do
        counters[w] = shared:get("rr_worker_count_" .. i) or 0
    end
    local ok_c, counters_enc = pcall(cjson.encode, counters)
    if not ok_c then counters_enc = "{}" end

    -- Last state snapshot (re-encode to guarantee valid JSON output)
    local state_snapshot = "{}"
    local ok_s, parsed = pcall(cjson.decode, state_str)
    if ok_s and type(parsed) == "table" then
        local ok_e, enc = pcall(cjson.encode, parsed)
        if ok_e then state_snapshot = enc end
    end

    -- Escape any quotes in refresh_err to keep JSON valid
    local safe_err = (refresh_err or ""):gsub('\\', '\\\\'):gsub('"', '\\"')

    ngx.header["Content-Type"] = "application/json"
    ngx.status = 200
    ngx.say(string.format(
        '{"ts":%.3f,"state_age_s":%.1f,"last_refresh_status":"%s",' ..
        '"last_refresh_err":"%s","decision_counter_per_worker":%s,' ..
        '"last_state_snapshot":%s}',
        state_ts, state_age_s, refresh_status, safe_err,
        counters_enc, state_snapshot
    ))
end

return M
