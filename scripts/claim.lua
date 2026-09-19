-- scripts/claim.lua
-- Non-destructively claims a task from a work queue with a lease TTL.
-- Automatically reclaims expired leases and routes to DLQ after max_retries.
-- ARGV[1]: prefix (e.g. "locutus:")
-- ARGV[2]: queue name (e.g. "render_jobs")
-- ARGV[3]: agent name (e.g. "worker-1")
-- ARGV[4]: lease duration in seconds (default 120)
-- ARGV[5]: max retries before moving to DLQ (default 3)

local prefix = ARGV[1]
local qname = ARGV[2]
local agent = ARGV[3] or "unknown"
local lease_sec = tonumber(ARGV[4]) or 120
local max_retries = tonumber(ARGV[5]) or 3

local queue_key = prefix .. "queue:{" .. qname .. "}"
local leases_key = prefix .. "leases:{" .. qname .. "}"
local dlq_key = prefix .. "queue:dlq:{" .. qname .. "}"
local attempts_key = prefix .. "attempts:{" .. qname .. "}"

local now_ts = tonumber(redis.call('TIME')[1])

-- 1. Auto-reclaim expired leases for this queue
local expired = redis.call('ZRANGEBYSCORE', leases_key, '-inf', now_ts, 'LIMIT', 0, 20)
for _, task_id in ipairs(expired) do
    local active_key = prefix .. "active:{" .. qname .. "}:" .. task_id
    local task_data = redis.call('GET', active_key)
    redis.call('ZREM', leases_key, task_id)
    redis.call('DEL', active_key)
    if task_data then
        local attempts = tonumber(redis.call('HGET', attempts_key, task_id) or 1)
        if attempts >= max_retries then
            -- Exceeded max retries, move to Dead-Letter Queue
            redis.call('LPUSH', dlq_key, task_data)
            redis.call('HDEL', attempts_key, task_id)
        else
            -- Increment retry count and re-queue at tail of queue (LPUSH prevents head-of-line jamming)
            redis.call('HINCRBY', attempts_key, task_id, 1)
            redis.call('LPUSH', queue_key, task_data)
        end
    end
end

-- 2. Try to pop the next available task
local task_json = redis.call('RPOP', queue_key)
if not task_json then
    return nil
end

-- 3. Extract or generate task ID
local decoded = cjson.decode(task_json)
local task_id = decoded["id"]
if not task_id or task_id == "" then
    task_id = "task_" .. now_ts .. "_" .. tostring(math.random(1000, 9999))
    decoded["id"] = task_id
    task_json = cjson.encode(decoded)
end

-- 4. Set active lease and expiration
local expire_ts = now_ts + lease_sec
local active_key = prefix .. "active:{" .. qname .. "}:" .. task_id
redis.call('SET', active_key, task_json, 'EX', math.max(lease_sec * 4, 300))
redis.call('ZADD', leases_key, expire_ts, task_id)
redis.call('HSETNX', attempts_key, task_id, 1)

return task_json
