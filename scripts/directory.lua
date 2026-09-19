-- scripts/directory.lua
-- Lists registered agents, their liveness heartbeat (1 or 0), and tags.
-- Can optionally filter by project or tag (e.g. only agents matching "locutus").
-- ARGV[1]: prefix (e.g. "locutus:")
-- ARGV[2]: optional filter tag (e.g. "locutus", or "*" / nil for all)

local prefix = ARGV[1]
local filter = ARGV[2]
local agents = {}

if filter and filter ~= "" and filter ~= "*" and filter ~= "@all" then
    agents = redis.call('SMEMBERS', prefix .. 'tag:' .. filter)
else
    agents = redis.call('SMEMBERS', prefix .. 'active_agents')
end

local result = {}
for _, agent in ipairs(agents) do
    local alive = redis.call('EXISTS', prefix .. 'heartbeat:' .. agent)
    local tags = redis.call('HGET', prefix .. 'agent:' .. agent, 'tags') or ""
    table.insert(result, agent .. "|" .. tostring(alive) .. "|" .. tags)
end
return result
