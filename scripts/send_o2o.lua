-- scripts/send_o2o.lua
-- Pushes a direct message to a recipient's inbox and sets/refreshes inbox TTL.
-- ARGV[1]: prefix (e.g. "locutus:")
-- ARGV[2]: recipient agent name (e.g. "bob")
-- Dual Mode:
-- Mode A (Raw JSON string):
--   ARGV[3]: message JSON string
--   ARGV[4]: inbox TTL seconds (default 604800)
-- Mode B (Field parameters - auto-encodes JSON via cjson):
--   ARGV[3]: type ("task", "query", "reply", "status")
--   ARGV[4]: from (e.g. "alice")
--   ARGV[5]: subject (e.g. "Math Task")
--   ARGV[6]: body (e.g. "Please compute 25 * 4.")
--   ARGV[7]: tags_csv (optional, e.g. "locutus")
--   ARGV[8]: reply_to (optional, or "")
--   ARGV[9]: id (optional, auto-generated if empty)
--   ARGV[10]: timestamp (ISO timestamp string, e.g. from $(date -u +"%Y-%m-%dT%H:%M:%SZ"))
--   ARGV[11]: inbox TTL seconds (default 604800)

local prefix = ARGV[1]
if not prefix or prefix == "" then
    return redis.error_reply("ERR: Missing prefix")
end

local recipient = ARGV[2]
if not recipient or recipient == "" then
    return redis.error_reply("ERR: Missing recipient")
end

local arg3 = ARGV[3]
if not arg3 or arg3 == "" then
    return redis.error_reply("ERR: Missing message payload")
end

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

    -- If caller passed timestamp in msg_id slot (common when omitting msg_id):
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
        ["to"] = recipient,
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

redis.call('LPUSH', prefix .. 'inbox:' .. recipient, msg_json)
redis.call('EXPIRE', prefix .. 'inbox:' .. recipient, inbox_ttl)
return "OK"
