-- scripts/directory.lua
-- Lists all registered agents, their liveness heartbeat (1 or 0), and tags.
-- ARGV[1]: prefix (e.g. "a2a:")

local prefix = ARGV[1]
local agents = redis.call('SMEMBERS', prefix .. 'active_agents')
local result = {}

for _, agent in ipairs(agents) do
    local alive = redis.call('EXISTS', prefix .. 'heartbeat:' .. agent)
    local tags = redis.call('HGET', prefix .. 'agent:' .. agent, 'tags') or ""
    table.insert(result, agent .. "|" .. tostring(alive) .. "|" .. tags)
end
return result
