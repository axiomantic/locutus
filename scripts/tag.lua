-- scripts/tag.lua
-- Dynamically add, remove, or set tags for an active agent without reregistering or clearing inbox.
-- ARGV[1]: prefix (e.g. "locutus:")
-- ARGV[2]: agent name (e.g. "alice")
-- ARGV[3]: action ("add", "remove", "set")
-- ARGV[4]: comma-separated tags to add, remove, or set

local prefix = ARGV[1]
if not prefix or prefix == "" then
    return redis.error_reply("ERR: Missing prefix")
end

local name = ARGV[2]
if not name or name == "" then
    return redis.error_reply("ERR: Missing agent name")
end

local action = ARGV[3] or "add"
if action ~= "add" and action ~= "remove" and action ~= "set" then
    return redis.error_reply("ERR: Unknown action '" .. tostring(action) .. "'")
end

local tags_arg = ARGV[4] or ""

if redis.call('SISMEMBER', prefix .. 'active_agents', name) == 0 then
    return redis.error_reply("ERR agent '" .. name .. "' is not registered")
end

local current_csv = redis.call('HGET', prefix .. 'agent:' .. name, 'tags') or ""
local tag_set = {}

for t in string.gmatch(current_csv, "([^,]+)") do
    local tr = string.match(t, "^%s*(.-)%s*$")
    if tr ~= "" then tag_set[tr] = true end
end

if action == "add" then
    for t in string.gmatch(tags_arg, "([^,]+)") do
        local tr = string.match(t, "^%s*(.-)%s*$")
        if tr ~= "" then
            tag_set[tr] = true
            redis.call('SADD', prefix .. 'tag:' .. tr, name)
        end
    end
elseif action == "remove" then
    for t in string.gmatch(tags_arg, "([^,]+)") do
        local tr = string.match(t, "^%s*(.-)%s*$")
        if tr ~= "" then
            tag_set[tr] = nil
            redis.call('SREM', prefix .. 'tag:' .. tr, name)
        end
    end
elseif action == "set" then
    for old_t, _ in pairs(tag_set) do
        redis.call('SREM', prefix .. 'tag:' .. old_t, name)
    end
    tag_set = {}
    for t in string.gmatch(tags_arg, "([^,]+)") do
        local tr = string.match(t, "^%s*(.-)%s*$")
        if tr ~= "" then
            tag_set[tr] = true
            redis.call('SADD', prefix .. 'tag:' .. tr, name)
        end
    end
end

local updated_list = {}
for t, _ in pairs(tag_set) do
    table.insert(updated_list, t)
end
table.sort(updated_list)
local new_csv = table.concat(updated_list, ",")
redis.call('HSET', prefix .. 'agent:' .. name, 'tags', new_csv)

return new_csv
