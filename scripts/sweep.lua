-- sweep.lua
-- Cluster Health Watchdog & Sweeper for Self-Healing Meshes
-- Keys:
--   active_agents: prefix .. "active_agents" (Set)
--   agents: prefix .. "agents" (Hash)
--   agent_tags: prefix .. "agent_tags" (Hash)
--   agent_projects: prefix .. "agent_projects" (Hash)
--   heartbeat: prefix .. "heartbeat:" .. agent (String with TTL)
--   listeners: prefix .. "listener:*" (String)

local prefix = ARGV[1]
if not prefix or prefix == "" then
    return redis.error_reply("ERR: Missing prefix")
end

local action = ARGV[2] or "audit"
local dry_run = ARGV[3] == "1" or action == "audit"

local function encode_array(arr)
    if not arr or #arr == 0 then
        return "[]"
    end
    return cjson.encode(arr)
end

local dead_agents = {}
local active_agents = redis.call("SMEMBERS", prefix .. "active_agents")

for _, agent in ipairs(active_agents) do
    local hb = redis.call("EXISTS", prefix .. "heartbeat:" .. agent)
    if hb == 0 then
        table.insert(dead_agents, agent)
        if not dry_run then
            redis.call("SREM", prefix .. "active_agents", agent)
            local agent_tags = redis.call("HGET", prefix .. "agent:" .. agent, "tags") or ""
            for t in string.gmatch(agent_tags, "([^,]+)") do
                local tr = string.match(t, "^%s*(.-)%s*$")
                if tr ~= "" then redis.call("SREM", prefix .. "tag:" .. tr, agent) end
            end
            redis.call("DEL", prefix .. "agent:" .. agent)
        end
    end
end

local cursor = "0"
local listeners = {}
repeat
    local scan_res = redis.call("SCAN", cursor, "MATCH", prefix .. "listener:*", "COUNT", 100)
    cursor = scan_res[1]
    local keys = scan_res[2]
    for _, lk in ipairs(keys) do
        local agent = string.sub(lk, string.len(prefix .. "listener:") + 1)
        local val = redis.call("GET", lk)
        table.insert(listeners, { agent = agent, key = lk, data = val or "" })
    end
until cursor == "0"

local listeners_parts = {}
for _, l in ipairs(listeners) do
    local l_json = string.format('{"agent":%s,"key":%s,"data":%s}',
        cjson.encode(l.agent),
        cjson.encode(l.key),
        cjson.encode(l.data)
    )
    table.insert(listeners_parts, l_json)
end

local listeners_json = "[" .. table.concat(listeners_parts, ",") .. "]"

local res = string.format('{"dead_agents":%s,"listeners":%s,"dry_run":%s}',
    encode_array(dead_agents),
    listeners_json,
    dry_run and "true" or "false"
)

return res
