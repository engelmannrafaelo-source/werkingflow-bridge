-- Gemini-Bildweg (X-Vision-Provider: gemini) darf nicht am leeren Claude-Pool
-- scheitern. Stub-ngx, laeuft mit luajit (siehe run_gemini_bypass_test.sh).
local counter = 0
local store = { state = '{"accounts":{"a":{}}}', ts = "0" }
package.loaded["cjson.safe"] = { decode = function() return { accounts = {} } end }
package.loaded["resty.http"] = {}
package.loaded["pool_pick"] = { pick_weighted_account = function() return nil end }
local function run(hdr)
  ngx = {
    now = function() return 0 end, WARN = 1, ERR = 2, log = function() end,
    timer = { every = function() end, at = function() end },
    worker = { id = function() return 1 end },
    shared = { pool_state = {
      get = function(_, k) return store[k] end,
      set = function() end,
      incr = function() counter = counter + 1; return counter end,
    } },
    var = { http_x_vision_provider = hdr, request_length = "100" },
  }
  package.loaded["pool_router"] = nil
  local M = dofile("/lua/pool_router.lua")
  M.choose({ overflow_capable = false })
  return ngx.var
end
local v = run("gemini")
assert(v.x_pool_decision == "gemini_vision_bypass", "gemini: " .. tostring(v.x_pool_decision))
assert(v.target_worker and v.target_worker ~= "unavailable")
v = run("GEMINI")
assert(v.x_pool_decision == "gemini_vision_bypass", "case-insensitive")
v = run("claude")
assert(v.target_worker ~= nil and v.x_pool_decision ~= "gemini_vision_bypass", "claude must not bypass")
v = run(nil)
assert(v.x_pool_decision ~= "gemini_vision_bypass", "no header must not bypass")
print("GEMINI_BYPASS_OK")
