-- scripts/drain.lua
-- Atomically pops up to N messages from an agent's inbox (oldest first).
-- ARGV[1]: prefix (e.g. "locutus:")
-- ARGV[2]: agent name (e.g. "alice")
-- ARGV[3]: max count to pop (default 50)

local prefix = ARGV[1]
if not prefix or prefix == "" then
    return redis.error_reply("ERR: Missing prefix")
end

local name = ARGV[2]
if not name or name == "" then
    return redis.error_reply("ERR: Missing agent name")
end

local count = tonumber(ARGV[3]) or 50
local messages = {}

for i = 1, count do
    local msg = redis.call('RPOP', prefix .. 'inbox:' .. name)
    if not msg then break end
    table.insert(messages, msg)
end
return messages
