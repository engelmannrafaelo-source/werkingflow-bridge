-- pool_pick.lua — account ranking policy for the Bridge pool router.
--
-- Pure Lua on purpose: no ngx.*, no shdict, no resty.http. The decision is the
-- part worth testing, and keeping it free of nginx lets a plain luajit run it
-- (tests/nginx/test_pool_pick.lua). pool_router.lua keeps the I/O, the shared
-- state and the logging.
--
-- MEASURED vs UNMEASURED — why this module exists in this shape
--
-- `weekly_percent` used to arrive as 0 both when an account was genuinely idle
-- and when nobody had ever measured it. 0% is the single most attractive value
-- in every ranking here, so the pool preferred exactly the accounts it knew
-- nothing about. The production bridge sat in that state for months: four
-- workers, no usage snapshot ever delivered to that host, all four advertising
-- weekly=0% and headroom=100%.
--
-- Fix: two buckets. Accounts with a fresh measurement rank normally. Accounts
-- without one are held back and used ONLY when no measured account can take
-- the request.
--
-- Held back rather than excluded, deliberately: a missing measurement is a
-- monitoring failure, and a hard reject would turn a blind spot into a pool
-- outage — on the production bridge today it would reject 100% of traffic.
-- The caller is told which bucket the pick came from so "we routed blind" is
-- visible in the log and in the pool_decision, instead of being
-- indistinguishable from a healthy pick.

local M = {}

-- Hard wall — accounts above this weekly% are never eligible regardless
-- of effective_cap_tokens. Guards against weekly_pct flicker (e.g. 96 ↔ 97
-- across the Anthropic-side rolling window) where mult bounces between
-- 0.10 (eligible at ~127K capacity) and 0.0 (excluded). The flicker would
-- briefly route real traffic to a near-wall account and trigger Anthropic
-- 429s that the cross-worker retry then has to clean up.
M.WEEKLY_HARD_EXCLUDE_PCT = 96

-- Eligibility of ONE account — the single definition used by the weighted pick
-- and by the affinity check, so "eligible" cannot mean two things.
-- Returns nil when eligible, else a short reason (also the affinity fallback
-- reason in the X-Cache-Affinity header and the log).
function M.account_rejection(info, min_required)
    if type(info) ~= "table" then return "unknown_account" end
    local cooldown   = tonumber(info.cooldown_remaining_s) or 0
    local eff_cap    = tonumber(info.effective_cap_tokens) or 0
    local in_flight  = tonumber(info.current_in_flight_tokens) or 0
    local weekly_pct = tonumber(info.weekly_percent)
    -- Strictly `== true`: a missing field decodes to nil, and anything that
    -- is not an explicit true must count as unmeasured. Never `~= false` —
    -- a worker on an image that predates usage_known has measured nothing.
    local known      = (info.usage_known == true)
    local capacity   = eff_cap - in_flight
    -- The weekly hard wall can only exclude on a real reading. With no
    -- measurement there is no weekly% to compare against, so the check is
    -- skipped and the account lands in the unmeasured bucket instead.
    local past_wall  = known and weekly_pct ~= nil
                       and weekly_pct >= M.WEEKLY_HARD_EXCLUDE_PCT
    if not info.available then return "unavailable" end
    if cooldown ~= 0 then return "cooldown" end
    if past_wall then return "weekly_wall" end
    if not (capacity > (min_required or 0)) then return "no_capacity" end
    return nil
end

-- Weighted-capacity picker.
--
-- Weight = effective_cap_tokens − current_in_flight_tokens (= live admit
-- headroom). Weighted-random distributes load proportional to remaining
-- capacity per account. As an account approaches its wall its
-- predictive_multiplier shrinks effective_cap; as in_flight rises the residual
-- shrinks too — so heavy accounts fade out without explicit deprioritisation.
--
-- Returns: name, worker, weight, total, from_measured, unmeasured_names
--          (nil, nil, -1, -1, true, {}) when nothing is eligible.
--
-- `rand` is injectable so the test can pin the weighted draw; production passes
-- nothing and gets math.random.
function M.pick_weighted_account(accounts, est_tokens, rand)
    rand = rand or math.random
    local measured, unmeasured = {}, {}
    local total_m, total_u = 0, 0
    local unmeasured_names = {}
    local min_required = est_tokens or 0

    for name, info in pairs(accounts) do
        local known      = (info.usage_known == true)
        local capacity   = (tonumber(info.effective_cap_tokens) or 0)
                           - (tonumber(info.current_in_flight_tokens) or 0)

        if M.account_rejection(info, min_required) == nil then
            -- Carry the account's own worker upstream name (topology-agnostic:
            -- no hardcoded account→worker map). Fall back to the account name
            -- only if the state somehow omitted `.worker`.
            local entry = { name = name, weight = capacity, worker = info.worker or name }
            if known then
                measured[#measured + 1] = entry
                total_m = total_m + capacity
            else
                unmeasured[#unmeasured + 1] = entry
                unmeasured_names[#unmeasured_names + 1] = name
                total_u = total_u + capacity
            end
        end
    end

    local eligible, total, from_measured = measured, total_m, true
    if #measured == 0 or total_m <= 0 then
        eligible, total, from_measured = unmeasured, total_u, false
    end

    if #eligible == 0 or total <= 0 then
        return nil, nil, -1, -1, true, unmeasured_names
    end

    -- Deterministic iteration for the weighted walk: pairs() order is
    -- unspecified, and an unstable order makes the draw unreproducible.
    table.sort(eligible, function(a, b) return a.name < b.name end)

    local r, acc = rand() * total, 0
    for _, e in ipairs(eligible) do
        acc = acc + e.weight
        if r <= acc then
            return e.name, e.worker, e.weight, total, from_measured, unmeasured_names
        end
    end
    -- Floating-point safety: fall through to last
    local last = eligible[#eligible]
    return last.name, last.worker, last.weight, total, from_measured, unmeasured_names
end

-- ---------------------------------------------------------------------------
-- Account affinity (CACHE2)
--
-- The prompt cache is per account. Spreading the calls of one run over four
-- accounts makes a cache hit a 1-in-4 coincidence (CACHE1, cause 2). A caller
-- sends a key (X-Cache-Affinity-Key: its run or job id); the same key prefers
-- the same account.
--
-- Rendezvous (highest-random-weight) hashing over ALL accounts in the state,
-- not over the eligible ones: the preferred account of a key must not move
-- because some OTHER account is momentarily full. When an account joins or
-- leaves, only the keys that preferred it move.
--
-- The preferred account is used only when it passes the SAME eligibility as
-- the weighted pick and does not jump the measured-before-unmeasured rule.
-- Otherwise the weighted pick runs unchanged and the reason is returned, so the
-- router can say it out loud. Affinity never overrides a guard.
-- ---------------------------------------------------------------------------
local bit = require "bit"

-- 32-bit FNV-1a. Multiplication split in 16-bit halves: a plain h * prime
-- exceeds 2^53 and loses bits in a double.
local function mul32(a, b)
    local a_lo = a % 65536
    local a_hi = (a - a_lo) / 65536
    return ((a_hi * b) % 4294967296 * 65536 + a_lo * b) % 4294967296
end

function M.fnv1a32(s)
    local h = 2166136261
    for i = 1, #s do
        h = bit.bxor(h, s:byte(i)) % 4294967296
        h = mul32(h, 16777619)
    end
    return h
end

-- Preferred account for `key`: highest fnv1a32(key .. "\0" .. name), ties by
-- name. Returns name, info — or nil when the state has no accounts.
function M.preferred_account(accounts, key)
    local best_name, best_score
    for name, _ in pairs(accounts) do
        local score = M.fnv1a32(key .. "\0" .. name)
        if best_score == nil or score > best_score
           or (score == best_score and name < best_name) then
            best_name, best_score = name, score
        end
    end
    if best_name == nil then return nil, nil end
    return best_name, accounts[best_name]
end

-- Pick with affinity. Returns the same six values as pick_weighted_account,
-- plus:
--   7. affinity: nil (no key) | "hit" | "fallback:<reason>"
--   8. preferred account name (nil without key)
-- `worker_ok(worker)` (optional) says whether the router can actually reach
-- that worker as an affinity target; a "no" is a fallback, never a guess.
function M.pick_with_affinity(accounts, est_tokens, key, rand, worker_ok)
    local name, worker, weight, total, from_measured, unmeasured_names =
        M.pick_weighted_account(accounts, est_tokens, rand)
    if key == nil or key == "" then
        return name, worker, weight, total, from_measured, unmeasured_names, nil, nil
    end
    local pref, info = M.preferred_account(accounts, key)
    local reason
    if pref == nil then
        reason = "no_accounts"
    else
        reason = M.account_rejection(info, est_tokens or 0)
        if reason == nil and info.usage_known ~= true and from_measured and name ~= nil then
            -- A measured account could take it: the unmeasured preferred one
            -- stays held back, exactly like in the weighted pick.
            reason = "held_back_unmeasured"
        end
        if reason == nil and worker_ok and not worker_ok(info.worker or pref) then
            reason = "unknown_worker"
        end
    end
    if reason ~= nil then
        return name, worker, weight, total, from_measured, unmeasured_names,
               "fallback:" .. reason, pref
    end
    local capacity = (tonumber(info.effective_cap_tokens) or 0)
                     - (tonumber(info.current_in_flight_tokens) or 0)
    return pref, (info.worker or pref), capacity, total,
           (info.usage_known == true), unmeasured_names, "hit", pref
end

return M
