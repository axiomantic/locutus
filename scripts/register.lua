-- scripts/register.lua
-- Registers an agent, records tags and metadata, and arms an expiring heartbeat.
-- ARGV[1]: prefix (e.g. "a2a:")
-- ARGV[2]: agent name (e.g. "alice")
-- ARGV[3]: tags comma-separated (e.g. "backend,ticket-104")
-- ARGV[4]: heartbeat TTL seconds (default 150)

local prefix = ARGV[1]
local name = ARGV[2]
local tags_csv = ARGV[3] or ""
local ttl = tonumber(ARGV[4]) or 150
local now = redis.call('TIME')[1]

-- 1. Refresh Heartbeat
redis.call('SET', prefix .. 'heartbeat:' .. name, '1', 'EX', ttl)

-- 2. Add to active roster and store metadata
redis.call('SADD', prefix .. 'active_agents', name)
redis.call('HSET', prefix .. 'agent:' .. name, 'tags', tags_csv, 'last_seen', now)

-- 3. Index tags
for tag in string.gmatch(tags_csv, "([^,]+)") do
    local trimmed = string.match(tag, "^%s*(.-)%s*$")
    if trimmed ~= "" then
        redis.call('SADD', prefix .. 'tag:' .. trimmed, name)
    end
end
return "OK"
