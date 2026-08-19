-- Unit tests for the pool ranking policy (docker/lua/pool_pick.lua).
--
-- Run:  tests/nginx/run_pool_pick_tests.sh
--
-- The property under test: an account whose usage was never measured must not
-- outrank one that was. Before the tri-state, "never measured" arrived as
-- weekly_percent=0 — the most attractive value in this ranking — so the
-- production bridge advertised four unmeasured workers as 100% free and the
-- router happily believed it.

package.path = "/lua/?.lua;" .. package.path
local pick = require("pool_pick").pick_weighted_account

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
        worker = opts.worker or "w",
        available = (opts.available ~= false),
        cooldown_remaining_s = opts.cooldown or 0,
        effective_cap_tokens = opts.cap or 400000,
        current_in_flight_tokens = opts.in_flight or 0,
        weekly_percent = opts.weekly,
        usage_known = opts.known,
    }
end

-- Draw the first element of the sorted eligible list, deterministically.
local function first() return 0 end
-- Draw the last element.
local function last() return 1 end

print("pool_pick: measured vs unmeasured")

-- 1. A measured account wins over an unmeasured one even when the unmeasured
--    one advertises MORE capacity — the whole point.
do
    local accounts = {
        gmail  = acct({ known = true,  weekly = 50, cap = 100000, worker = "worker3" }),
        sahori = acct({ known = false,             cap = 900000, worker = "worker-sahori" }),
    }
    local name, worker, _, _, from_measured = pick(accounts, 500, first)
    check(name == "gmail", "measured account must win, got " .. tostring(name))
    check(worker == "worker3", "worker must come from the account row")
    check(from_measured == true, "from_measured must be true")
end

-- 2. Nothing measured → still serves, but flagged. Held back, not excluded:
--    on the production bridge today NO account is measured, and a hard reject
--    would drop 100% of traffic.
do
    local accounts = {
        sahori = acct({ known = false, worker = "worker-sahori" }),
        kurt   = acct({ known = false, worker = "worker-kurt" }),
    }
    local name, _, _, _, from_measured, unmeasured = pick(accounts, 500, first)
    check(name ~= nil, "must still pick when nothing is measured")
    check(from_measured == false, "from_measured must be false")
    check(#unmeasured == 2, "both accounts must be reported as unmeasured")
end

-- 3. A missing usage_known field (worker on a pre-tri-state image) counts as
--    unmeasured — never as measured.
do
    local legacy = acct({ cap = 900000, worker = "legacy" })
    legacy.usage_known = nil
    local accounts = { legacy = legacy, gmail = acct({ known = true, weekly = 90, cap = 10000, worker = "worker3" }) }
    local name = pick(accounts, 500, first)
    check(name == "gmail", "missing usage_known must not rank as measured, got " .. tostring(name))
end

-- 4. usage_known = false explicitly behaves the same.
do
    local accounts = {
        a = acct({ known = false, cap = 900000, worker = "wa" }),
        b = acct({ known = true, weekly = 10, cap = 20000, worker = "wb" }),
    }
    check(pick(accounts, 500, first) == "b", "explicit false must be held back")
end

-- 5. A MEASURED zero is a real measurement and must rank normally — the other
--    half of the contract. Fixing the phantom must not punish a genuinely
--    idle account.
do
    local accounts = {
        fresh = acct({ known = true, weekly = 0, cap = 800000, worker = "wf" }),
        busy  = acct({ known = true, weekly = 80, cap = 100000, worker = "wb" }),
    }
    local name, _, _, _, from_measured = pick(accounts, 500, first)
    check(name == "busy" or name == "fresh", "both measured accounts stay eligible")
    check(from_measured == true, "measured zero must count as measured")
    -- weighted draw: with r→1 the heaviest (last in cumulative walk) is taken
    local hi = pick(accounts, 500, last)
    check(hi ~= nil, "weighted walk must terminate")
end

-- 6. The weekly hard wall still excludes — but only on a real reading.
do
    local accounts = {
        wall  = acct({ known = true, weekly = 97, cap = 900000, worker = "ww" }),
        ok    = acct({ known = true, weekly = 10, cap = 10000, worker = "wo" }),
    }
    check(pick(accounts, 500, first) == "ok", "account past the weekly wall must be excluded")
end

-- 7. An unmeasured account cannot be excluded by the weekly wall (there is no
--    reading to compare) — it is demoted instead, and remains a last resort.
do
    local accounts = { blind = acct({ known = false, weekly = nil, worker = "wb" }) }
    local name, _, _, _, from_measured = pick(accounts, 500, first)
    check(name == "blind", "unmeasured account stays available as last resort")
    check(from_measured == false, "and is reported as unmeasured")
end

-- 8. Availability / cooldown / capacity filters are unchanged by the split.
do
    local accounts = {
        down     = acct({ known = true, weekly = 1, available = false }),
        cooling  = acct({ known = true, weekly = 1, cooldown = 30 }),
        tiny     = acct({ known = true, weekly = 1, cap = 100 }),
    }
    local name, _, _, _, _, unmeasured = pick(accounts, 500, first)
    check(name == nil, "no eligible account expected, got " .. tostring(name))
    check(#unmeasured == 0, "excluded-for-other-reasons accounts are not 'unmeasured'")
end

print(string.format("pool_pick: %d checks, %d failures", checks, failures))
os.exit(failures == 0 and 0 or 1)
