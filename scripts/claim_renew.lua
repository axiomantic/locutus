-- scripts/claim_renew.lua
-- Renews the lease of an in-flight task on a work queue without returning it to the queue.
-- ARGV[1]: prefix (e.g. "locutus:")
-- ARGV[2]: queue name (e.g. "jobs")
-- ARGV[3]: task ID
-- ARGV[4]: lease duration in seconds (default 120)

local prefix = ARGV[1]
local qname = ARGV[2]
local task_id = ARGV[3]
local lease_sec = tonumber(ARGV[4]) or 120

if not task_id or task_id == "" then
    return redis.error_reply("ERR: Missing task_id")
end

local leases_key = prefix .. "leases:{" .. qname .. "}"
local active_key = prefix .. "active:{" .. qname .. "}:" .. task_id

local now_ts = tonumber(redis.call('TIME')[1])
local score = redis.call('ZSCORE', leases_key, task_id)

if not score then
    return redis.error_reply("ERR: Task " .. task_id .. " not found or lease expired")
end

local score_num = tonumber(score)
if score_num < now_ts then
    return redis.error_reply("ERR: Lease for task " .. task_id .. " already expired")
end

local new_expire = now_ts + lease_sec
redis.call('ZADD', leases_key, new_expire, task_id)
redis.call('EXPIRE', active_key, math.max(lease_sec * 4, 300))

local res = {
    task_id = task_id,
    queue = qname,
    renewed = true,
    lease_sec = lease_sec,
    expires_at = new_expire
}
return cjson.encode(res)
