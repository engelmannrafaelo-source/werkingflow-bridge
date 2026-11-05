-- Fresh central operator policy + both technical pools; never an allow-cache.
-- Transport is internal. The token is not sent upstream or returned to clients.
local http = require "resty.http"
local json = require "cjson.safe"
local pick = require "pool_pick"
local M = {}

function M.select(min_tokens, local_worker, already_hopped, preferred)
    local base = (os.getenv("BRIDGE_ACCOUNT_POLICY_URL") or ""):gsub("/$", "")
    local token = os.getenv("BRIDGE_ACCOUNT_POLICY_TOKEN") or ""
    if base == "" or token == "" then return nil, nil, "policy_not_configured" end
    local client = http.new()
    client:set_timeout(8000)
    local response, err = client:request_uri(base .. "/runtime", {
        method = "GET", headers = { ["X-Bridge-Service-Token"] = token }, keepalive = false,
    })
    client:close()
    if not response or response.status ~= 200 then
        return nil, nil, "policy_unavailable"
    end
    local data = json.decode(response.body)
    if type(data) ~= "table" or type(data.accounts) ~= "table"
        or type(data.revision) ~= "number" then return nil, nil, "policy_invalid" end
    local eligible, preferred_eligible = {}, nil
    for account, info in pairs(data.accounts) do
        if type(info) == "table" and info.bridge_policy_revision == data.revision
            and type(info.worker) == "string" and info.worker:match("^[%w_-]+$")
            and (not already_hopped or local_worker(info.worker))
            and pick.account_rejection(info, min_tokens) == nil then
            eligible[account] = info
            if account == preferred then preferred_eligible = account end
        end
    end
    local selected = preferred_eligible
    if not selected then
        -- Priority is user-controlled; equal ranks keep the existing capacity policy.
        selected = pick.pick_weighted_account(eligible, min_tokens)
    end
    if not selected then return nil, data, "no_released_capacity" end
    data.selected = selected
    local reason = preferred and preferred ~= selected and "preferred_unavailable_fallback" or "manual_priority"
    return eligible[selected], data, reason
end
return M
