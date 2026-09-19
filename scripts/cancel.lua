-- cancel.lua
-- Atomic Run Cancellation Tokens for Orchestrators & Meshes
-- Keys:
--   cancel_key: prefix .. "cancel:" .. run_id
--   channels: prefix .. "channel:cancellations", prefix .. "channel:cancel:" .. run_id

local prefix = ARGV[1]
local action = ARGV[2]
local run_id = ARGV[3]

if not run_id or run_id == "" then
    return redis.error_reply("ERR: Missing run_id")
end

local cancel_key = prefix .. "cancel:" .. run_id

if action == "cancel" or action == "set" then
    local reason = (ARGV[4] and ARGV[4] ~= "") and ARGV[4] or "Cancelled by orchestrator"
    local by_agent = (ARGV[5] and ARGV[5] ~= "") and ARGV[5] or "orchestrator"
    local ttl = tonumber(ARGV[6]) or 3600
    local ts = (ARGV[7] and ARGV[7] ~= "") and ARGV[7] or tostring(redis.call("TIME")[1])
    local sig = (ARGV[8] and ARGV[8] ~= "") and ARGV[8] or ""

    local obj = {
        run_id = run_id,
        reason = reason,
        by = by_agent,
        timestamp = ts,
        sig = sig,
        cancelled = true
    }
    local payload = cjson.encode(obj)
    redis.call("SET", cancel_key, payload, "EX", ttl)

    -- Publish cancellation events
    local global_chan = prefix .. "channel:cancellations"
    local run_chan = prefix .. "channel:cancel:" .. run_id
    redis.call("PUBLISH", global_chan, payload)
    redis.call("PUBLISH", run_chan, payload)

    return "CANCELLED"

elseif action == "check" or action == "status" then
    local val = redis.call("GET", cancel_key)
    if val then
        return val
    else
        return ""
    end

elseif action == "clear" or action == "reset" then
    redis.call("DEL", cancel_key)
    return "CLEARED"

else
    return redis.error_reply("ERR: Unknown cancel action '" .. tostring(action) .. "'")
end
