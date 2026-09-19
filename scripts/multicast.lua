-- scripts/multicast.lua
-- Fans out a message to all agents with target_tag (or "*" for all active).
-- Automatically checks heartbeats and prunes expired dead agents.
-- ARGV[1]: prefix (e.g. "a2a:")
-- ARGV[2]: target tag (e.g. "qa", or "*" for all)
-- ARGV[3]: message JSON string
-- ARGV[4]: inbox TTL seconds (default 604800)

local prefix = ARGV[1]
local target_tag = ARGV[2]
local msg_json = ARGV[3]
local inbox_ttl = tonumber(ARGV[4]) or 604800
local targets = {}

if target_tag == "*" then
    targets = redis.call('SMEMBERS', prefix .. 'active_agents')
else
    targets = redis.call('SMEMBERS', prefix .. 'tag:' .. target_tag)
end

local delivered = 0
for _, agent in ipairs(targets) do
    if redis.call('EXISTS', prefix .. 'heartbeat:' .. agent) == 1 then
        redis.call('LPUSH', prefix .. 'inbox:' .. agent, msg_json)
        redis.call('EXPIRE', prefix .. 'inbox:' .. agent, inbox_ttl)
        delivered = delivered + 1
    else
        -- Prune inactive agent from registry
        if target_tag == "*" then
            redis.call('SREM', prefix .. 'active_agents', agent)
        else
            redis.call('SREM', prefix .. 'tag:' .. target_tag, agent)
        end
    end
end
return delivered
