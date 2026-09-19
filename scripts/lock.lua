-- scripts/lock.lua
-- Atomically acquires a distributed lock for an agent with an expiration lease.
-- ARGV[1]: prefix (e.g. "locutus:")
-- ARGV[2]: lock name (e.g. "git_rebase")
-- ARGV[3]: owner agent name (e.g. "alice")
-- ARGV[4]: lease TTL seconds (default 30)
-- ARGV[5]: with_fencing ("1" or "0", optional)

local prefix = ARGV[1]
if not prefix or prefix == "" then
    return redis.error_reply("ERR: Missing prefix")
end

local lock_name = ARGV[2]
if not lock_name or lock_name == "" then
    return redis.error_reply("ERR: Missing lock name")
end

local owner = ARGV[3]
if not owner or owner == "" then
    return redis.error_reply("ERR: Missing lock owner")
end

local ttl = tonumber(ARGV[4]) or 30
local with_fencing = ARGV[5] == "1"

local key = prefix .. "lock:{" .. lock_name .. "}"
local fencing_key = prefix .. "lock:fencing:{" .. lock_name .. "}"

local ok = redis.call('SET', key, owner, 'NX', 'EX', ttl)
if ok then
    local token = redis.call('INCR', fencing_key)
    if with_fencing then
        return tostring(token)
    else
        return 1
    end
else
    return 0
end
