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
    local meta = redis.call('HMGET', prefix .. 'agent:' .. agent, 'tags', 'state', 'activity')
    local tags = meta[1] or ""
    local state = meta[2] or "idle"
    local activity = meta[3] or ""
    table.insert(result, agent .. "|" .. tostring(alive) .. "|" .. tags .. "|" .. state .. "|" .. activity)
end
return result

