-- scripts/multicast.lua
-- Fans out a message to agents matching ALL specified tags (AND filter via SINTER).
-- If target is "*" or "@all", broadcasts to all active agents across all projects.
-- Automatically checks heartbeats and prunes expired dead agents.
-- ARGV[1]: prefix (e.g. "locutus:")
-- ARGV[2]: comma-separated target tags (e.g. "locutus,backend" or "*")
-- Dual Mode:
-- Mode A (Raw JSON string):
--   ARGV[3]: message JSON string
--   ARGV[4]: inbox TTL seconds (default 604800)
-- Mode B (Field parameters - auto-encodes JSON via cjson):
--   ARGV[3]: type ("task", "query", "reply", "status")
--   ARGV[4]: from (e.g. "lead")
--   ARGV[5]: subject (e.g. "Team Sync")
--   ARGV[6]: body (e.g. "Regression test passed.")
--   ARGV[7]: tags_csv (optional)
--   ARGV[8]: reply_to (optional)
--   ARGV[9]: id (optional, auto-generated if empty)
--   ARGV[10]: timestamp (ISO timestamp string, e.g. from $(date -u +"%Y-%m-%dT%H:%M:%SZ"))
--   ARGV[11]: inbox TTL seconds (default 604800)

local prefix = ARGV[1]
local target_tags_csv = ARGV[2] or "*"
local arg3 = ARGV[3]
local msg_json
local inbox_ttl = 604800

if string.sub(arg3, 1, 1) == "{" then
    -- Mode A: Direct JSON string
    msg_json = arg3
    inbox_ttl = tonumber(ARGV[4]) or 604800
else
    -- Mode B: Structured parameters
    local msg_type = arg3
    local from_agent = ARGV[4] or "unknown"
    local subject = ARGV[5] or ""
    local body = ARGV[6] or ""
    local tags_csv = ARGV[7] or ""
    local reply_to = ARGV[8]
    local msg_id = ARGV[9]
    local ts = ARGV[10]
    inbox_ttl = tonumber(ARGV[11]) or 604800

    -- If caller passed timestamp in msg_id slot:
    if msg_id and string.match(msg_id, "^%d%d%d%d%-%d%d%-%d%d") then
        ts = msg_id
        msg_id = ""
    end

    local t = redis.call('TIME')[1]
    if not msg_id or msg_id == "" then
        msg_id = "msg_" .. t .. "_" .. from_agent .. "_" .. tostring(math.random(1000, 9999))
    end
    if not ts or ts == "" then
        ts = "2026-09-18T00:00:00Z"
    end

    local tags = {}
    for tag in string.gmatch(tags_csv, "([^,]+)") do
        local tr = string.match(tag, "^%s*(.-)%s*$")
        if tr ~= "" then table.insert(tags, tr) end
    end

    local payload = {
        id = msg_id,
        ["from"] = from_agent,
        ["to"] = "@" .. target_tags_csv,
        ["type"] = msg_type,
        tags = tags,
        subject = subject,
        body = body,
        timestamp = ts
    }
    if reply_to and reply_to ~= "" then
        payload["reply_to"] = reply_to
    else
        payload["reply_to"] = cjson.null
    end

    msg_json = cjson.encode(payload)
    -- Ensure empty tags table encodes as JSON array [] rather than {}
    msg_json = string.gsub(msg_json, '"tags":{}', '"tags":[]')
end

local targets = {}
if target_tags_csv == "*" or target_tags_csv == "@all" then
    targets = redis.call('SMEMBERS', prefix .. 'active_agents')
else
    local tag_keys = {}
    for tag in string.gmatch(target_tags_csv, "([^,]+)") do
        local trimmed = string.match(tag, "^%s*(.-)%s*$")
        if trimmed ~= "" then
            table.insert(tag_keys, prefix .. 'tag:' .. trimmed)
        end
    end

    if #tag_keys == 0 then
        return 0
    elseif #tag_keys == 1 then
        targets = redis.call('SMEMBERS', tag_keys[1])
    else
        -- Native Redis AND-filter intersection
        targets = redis.call('SINTER', unpack(tag_keys))
    end
end

local delivered = 0
for _, agent in ipairs(targets) do
    if redis.call('EXISTS', prefix .. 'heartbeat:' .. agent) == 1 then
        redis.call('LPUSH', prefix .. 'inbox:' .. agent, msg_json)
        redis.call('EXPIRE', prefix .. 'inbox:' .. agent, inbox_ttl)
        delivered = delivered + 1
    else
        -- Prune dead agent from active roster and all tag sets
        redis.call('SREM', prefix .. 'active_agents', agent)
        local agent_tags = redis.call('HGET', prefix .. 'agent:' .. agent, 'tags') or ""
        for t in string.gmatch(agent_tags, "([^,]+)") do
            local tr = string.match(t, "^%s*(.-)%s*$")
            if tr ~= "" then redis.call('SREM', prefix .. 'tag:' .. tr, agent) end
        end
        redis.call('DEL', prefix .. 'agent:' .. agent)
    end
end
return delivered
