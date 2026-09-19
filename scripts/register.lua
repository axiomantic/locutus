-- scripts/register.lua
-- Registers an agent, records tags and metadata, and arms an expiring heartbeat.
-- Cleans up previously indexed tags if re-registering with a new tag set.
-- ARGV[1]: prefix (e.g. "locutus:")
-- ARGV[2]: agent name (e.g. "alice")
-- ARGV[3]: tags comma-separated (e.g. "locutus,backend,ticket-104")
-- ARGV[4]: heartbeat TTL seconds (default 150)

local prefix = ARGV[1]
local name = ARGV[2]

if not prefix or prefix == "" or not name or name == "" then
    return redis.error_reply("ERR: Missing prefix or agent name")
end

local tags_csv = ARGV[3] or ""
local ttl = tonumber(ARGV[4]) or 150
local now = redis.call('TIME')[1]

-- 1. Refresh Heartbeat
redis.call('SET', prefix .. 'heartbeat:' .. name, '1', 'EX', ttl)

-- 2. Clean up any previous tags if this agent was already registered
local old_tags = redis.call('HGET', prefix .. 'agent:' .. name, 'tags')
if old_tags and old_tags ~= "" then
    for old_tag in string.gmatch(old_tags, "([^,]+)") do
        local tr = string.match(old_tag, "^%s*(.-)%s*$")
        if tr ~= "" then redis.call('SREM', prefix .. 'tag:' .. tr, name) end
    end
end

-- 3. Add to active roster and store metadata
redis.call('SADD', prefix .. 'active_agents', name)
redis.call('HSET', prefix .. 'agent:' .. name, 'tags', tags_csv, 'last_seen', now)

-- 4. Index new tags
for tag in string.gmatch(tags_csv, "([^,]+)") do
    local trimmed = string.match(tag, "^%s*(.-)%s*$")
    if trimmed ~= "" then
        redis.call('SADD', prefix .. 'tag:' .. trimmed, name)
    end
end
return "OK"
