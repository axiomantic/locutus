-- scripts/floor.lua
-- Exclusive speaker lease & floor control ring for roundtable brainstorming.
-- ARGV[1]: prefix (e.g. "locutus:")
-- ARGV[2]: action ("request", "enqueue_waiter", "yield", "pass", "status")
-- ARGV[3]: room (e.g. "roundtable_1")
-- ARGV[4]: agent name
-- ARGV[5]: lease_sec (request) / target_agent (pass) / force (yield)
-- ARGV[6]: lease_sec (pass / yield next waiter)
-- ARGV[7]: force (pass)

local prefix = ARGV[1]
local action = ARGV[2]
local room = ARGV[3] or "default"
local agent = ARGV[4] or ""

local holder_key = prefix .. "floor:" .. room .. ":holder"
local waiters_key = prefix .. "floor:" .. room .. ":waiters"
local channel = prefix .. "channel:floor:" .. room

if action == "request" then
    local lease_sec = tonumber(ARGV[5]) or 60
    local holder = redis.call('GET', holder_key)
    if not holder or holder == "" or holder == agent then
        redis.call('SET', holder_key, agent, 'EX', lease_sec)
        redis.call('LREM', waiters_key, 0, agent)
        redis.call('PUBLISH', channel, "ACQUIRED:" .. agent)
        return "ACQUIRED"
    else
        return "BUSY:" .. holder
    end

elseif action == "enqueue_waiter" then
    local existing = redis.call('LRANGE', waiters_key, 0, -1)
    local found = false
    for _, w in ipairs(existing) do
        if w == agent then found = true; break end
    end
    if not found then
        redis.call('RPUSH', waiters_key, agent)
    end
    return "QUEUED"

elseif action == "yield" then
    local holder = redis.call('GET', holder_key)
    if holder and holder ~= agent and ARGV[5] ~= "force" then
        return "ERR: Floor is held by " .. holder
    end
    redis.call('DEL', holder_key)

    local next_waiter = redis.call('LPOP', waiters_key)
    if next_waiter then
        local lease_sec = tonumber(ARGV[6]) or tonumber(ARGV[5]) or 60
        redis.call('SET', holder_key, next_waiter, 'EX', lease_sec)
        redis.call('PUBLISH', channel, "PASSED:" .. next_waiter)
        return "PASSED:" .. next_waiter
    else
        redis.call('PUBLISH', channel, "FREE")
        return "YIELDED"
    end

elseif action == "pass" then
    local holder = redis.call('GET', holder_key)
    local target = ARGV[5] or ""
    local lease_sec = tonumber(ARGV[6]) or 60
    if holder and holder ~= agent and ARGV[7] ~= "force" then
        return "ERR: Floor is held by " .. holder
    end
    redis.call('SET', holder_key, target, 'EX', lease_sec)
    redis.call('LREM', waiters_key, 0, target)
    redis.call('PUBLISH', channel, "PASSED:" .. target)
    return "PASSED:" .. target

elseif action == "status" then
    local holder = redis.call('GET', holder_key) or ""
    local ttl = redis.call('TTL', holder_key)
    if ttl < 0 then ttl = 0 end
    local waiters = redis.call('LRANGE', waiters_key, 0, -1)
    local result = {
        room = room,
        holder = holder,
        ttl = ttl,
        waiters = waiters
    }
    local encoded = cjson.encode(result)
    encoded = string.gsub(encoded, '"waiters":{}', '"waiters":[]')
    return encoded
end

return nil
