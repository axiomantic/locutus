-- scripts/ack.lua
-- Acknowledges task completion, removing it from active leases and attempts tracking.
-- ARGV[1]: prefix (e.g. "locutus:")
-- ARGV[2]: queue name
-- ARGV[3]: task_id

local prefix = ARGV[1]
if not prefix or prefix == "" then
    return redis.error_reply("ERR: Missing prefix")
end

local qname = ARGV[2]
if not qname or qname == "" then
    return redis.error_reply("ERR: Missing queue name")
end

local task_id = ARGV[3]
if not task_id or task_id == "" then
    return redis.error_reply("ERR: Missing task ID")
end

local leases_key = prefix .. "leases:{" .. qname .. "}"
local active_key = prefix .. "active:{" .. qname .. "}:" .. task_id
local attempts_key = prefix .. "attempts:{" .. qname .. "}"

local rem_lease = redis.call('ZREM', leases_key, task_id)
local del_active = redis.call('DEL', active_key)
redis.call('HDEL', attempts_key, task_id)

if rem_lease > 0 or del_active > 0 then
    return 1
else
    return 0
end
