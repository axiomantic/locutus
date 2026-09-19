-- scripts/directory.lua
-- Lists registered agents, their liveness heartbeat (1 or 0), and tags.
-- Automatically prunes dead agents whose heartbeats have expired.
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

    if alive == 1 then
        table.insert(result, agent .. "|1|" .. tags .. "|" .. state .. "|" .. activity)
    else
        -- Prune dead agent from active roster, all associated tags, and delete metadata hash
        redis.call('SREM', prefix .. 'active_agents', agent)
        if filter and filter ~= "" and filter ~= "*" and filter ~= "@all" then
            redis.call('SREM', prefix .. 'tag:' .. filter, agent)
        end
        if tags and tags ~= "" then
            for tag in string.gmatch(tags, "([^,]+)") do
                local trimmed = string.match(tag, "^%s*(.-)%s*$")
                if trimmed ~= "" then
                    redis.call('SREM', prefix .. 'tag:' .. trimmed, agent)
                end
            end
        end
        redis.call('DEL', prefix .. 'agent:' .. agent)
    end
end
return result

