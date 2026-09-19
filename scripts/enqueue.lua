-- scripts/enqueue.lua
-- Pushes a task message onto a shared, load-balanced competing-consumers work queue.
-- ARGV[1]: prefix (e.g. "locutus:")
-- ARGV[2]: queue name (e.g. "tasks" -> key prefix .. "queue:" .. queue_name)
-- ARGV[3]: message JSON string
-- ARGV[4]: queue TTL seconds (default 604800)

local prefix = ARGV[1]
local qname = ARGV[2]
local msg_json = ARGV[3]
local ttl = tonumber(ARGV[4]) or 604800

local key
if string.sub(qname, 1, 4) == "dlq:" then
    key = prefix .. "queue:dlq:{" .. string.sub(qname, 5) .. "}"
else
    key = prefix .. "queue:{" .. qname .. "}"
end
local len = redis.call('LPUSH', key, msg_json)
redis.call('EXPIRE', key, ttl)
return len
