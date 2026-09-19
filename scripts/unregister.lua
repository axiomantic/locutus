-- scripts/unregister.lua
-- Removes agent from active roster, clears tags, and deletes heartbeat.
-- ARGV[1]: prefix (e.g. "locutus:")
-- ARGV[2]: agent name (e.g. "alice")

local prefix = ARGV[1]
if not prefix or prefix == "" then
    return redis.error_reply("ERR: Missing prefix")
end

local name = ARGV[2]
if not name or name == "" then
    return redis.error_reply("ERR: Missing agent name")
end

local tags_csv = redis.call('HGET', prefix .. 'agent:' .. name, 'tags') or ""

redis.call('DEL', prefix .. 'heartbeat:' .. name)
redis.call('SREM', prefix .. 'active_agents', name)
redis.call('DEL', prefix .. 'agent:' .. name)
redis.call('DEL', prefix .. 'inbox:' .. name)

for tag in string.gmatch(tags_csv, "([^,]+)") do
    local trimmed = string.match(tag, "^%s*(.-)%s*$")
    if trimmed ~= "" then
        redis.call('SREM', prefix .. 'tag:' .. trimmed, name)
    end
end
return "OK"
