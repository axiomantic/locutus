-- scripts/blackboard.lua
-- Atomic shared scratchpad / blackboard key-value and append memory in Redis.
-- ARGV[1]: prefix (e.g. "locutus:")
-- ARGV[2]: action ("set", "get", "append", "snapshot", "delete", "clear")
-- ARGV[3]: room (e.g. "architecture_roundtable")
-- ARGV[4]: key (or list_key)
-- ARGV[5]: value (JSON string or raw text)
-- ARGV[6]: ttl (default 604800 = 7 days)

local prefix = ARGV[1]
if not prefix or prefix == "" then
    return redis.error_reply("ERR: Missing prefix")
end

local action = ARGV[2]
if not action or action == "" then
    return redis.error_reply("ERR: Missing action")
end

local room = ARGV[3] or "default"
local key = ARGV[4] or ""
local val = ARGV[5] or ""
local ttl = tonumber(ARGV[6]) or 604800
local expected_rev = ARGV[7]

local kv_key = prefix .. "blackboard:{" .. room .. "}:kv"
local lists_index = prefix .. "blackboard:{" .. room .. "}:lists"
local rev_key = prefix .. "blackboard:{" .. room .. "}:rev"

if action == "set" then
    if not key or key == "" then
        return redis.error_reply("ERR: Missing key")
    end
    if expected_rev and expected_rev ~= "" then
        local current_rev = tonumber(redis.call('HGET', rev_key, key) or "0")
        if tonumber(expected_rev) ~= current_rev then
            return redis.error_reply("ERR: OCC revision mismatch: expected " .. tostring(expected_rev) .. " but current is " .. tostring(current_rev))
        end
    end
    redis.call('HSET', kv_key, key, val)
    redis.call('HINCRBY', rev_key, key, 1)
    if ttl > 0 then
        redis.call('EXPIRE', kv_key, ttl)
        redis.call('EXPIRE', rev_key, ttl)
    end
    return "OK"

elseif action == "rev" or action == "revision" then
    if not key or key == "" then
        return redis.error_reply("ERR: Missing key")
    end
    return redis.call('HGET', rev_key, key) or "0"

elseif action == "get" then
    local v = redis.call('HGET', kv_key, key)
    if v then
        return v
    end
    local list_key = prefix .. "blackboard:{" .. room .. "}:list:" .. key
    if redis.call('EXISTS', list_key) == 1 then
        local items = redis.call('LRANGE', list_key, 0, -1)
        return cjson.encode(items)
    end
    return nil

elseif action == "append" then
    if not key or key == "" then
        return redis.error_reply("ERR: Missing key")
    end
    local list_key = prefix .. "blackboard:{" .. room .. "}:list:" .. key
    local len = redis.call('RPUSH', list_key, val)
    if ttl > 0 then
        redis.call('EXPIRE', list_key, ttl)
    end
    redis.call('SADD', lists_index, key)
    if ttl > 0 then
        redis.call('EXPIRE', lists_index, ttl)
    end
    return tostring(len)

elseif action == "delete" or action == "del" then
    redis.call('HDEL', kv_key, key)
    redis.call('HDEL', rev_key, key)
    local list_key = prefix .. "blackboard:{" .. room .. "}:list:" .. key
    redis.call('UNLINK', list_key)
    redis.call('SREM', lists_index, key)
    return "OK"

elseif action == "clear" then
    local list_keys = redis.call('SMEMBERS', lists_index)
    for _, lk in ipairs(list_keys) do
        redis.call('UNLINK', prefix .. "blackboard:{" .. room .. "}:list:" .. lk)
    end
    redis.call('UNLINK', lists_index)
    redis.call('UNLINK', kv_key)
    redis.call('UNLINK', rev_key)
    return "OK"

elseif action == "snapshot" then
    local kv_raw = redis.call('HGETALL', kv_key)
    local kv_table = {}
    for i = 1, #kv_raw, 2 do
        kv_table[kv_raw[i]] = kv_raw[i+1]
    end

    local lists_table = {}
    local list_keys = redis.call('SMEMBERS', lists_index)
    for _, lk in ipairs(list_keys) do
        local items = redis.call('LRANGE', prefix .. "blackboard:{" .. room .. "}:list:" .. lk, 0, -1)
        lists_table[lk] = items
    end

    local result = {
        room = room,
        kv = kv_table,
        lists = lists_table
    }
    local encoded = cjson.encode(result)
    encoded = string.gsub(encoded, '"kv":%[%]', '"kv":{}')
    encoded = string.gsub(encoded, '"lists":%[%]', '"lists":{}')
    return encoded

elseif action == "load" or action == "restore" then
    if not val or val == "" then
        return redis.error_reply("ERR: Missing snapshot payload for load")
    end
    local ok, snapshot = pcall(cjson.decode, val)
    if not ok or type(snapshot) ~= "table" then
        return redis.error_reply("ERR: Invalid JSON snapshot for blackboard load")
    end
    if snapshot.kv and type(snapshot.kv) == "table" then
        for k, v in pairs(snapshot.kv) do
            redis.call('HSET', kv_key, k, tostring(v))
            redis.call('HINCRBY', rev_key, k, 1)
        end
    end
    if snapshot.lists and type(snapshot.lists) == "table" then
        for lk, items in pairs(snapshot.lists) do
            if type(items) == "table" then
                local list_key = prefix .. "blackboard:{" .. room .. "}:list:" .. lk
                redis.call('UNLINK', list_key)
                for _, item in ipairs(items) do
                    redis.call('RPUSH', list_key, tostring(item))
                end
                redis.call('SADD', lists_index, lk)
            end
        end
    end
    if ttl > 0 then
        redis.call('EXPIRE', kv_key, ttl)
        redis.call('EXPIRE', rev_key, ttl)
        redis.call('EXPIRE', lists_index, ttl)
    end
    return "OK"
end

return nil
