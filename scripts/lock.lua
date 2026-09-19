-- scripts/lock.lua
-- Atomically acquires a distributed lock for an agent with an expiration lease.
-- ARGV[1]: prefix (e.g. "locutus:")
-- ARGV[2]: lock name (e.g. "git_rebase")
-- ARGV[3]: owner agent name (e.g. "alice")
-- ARGV[4]: lease TTL seconds (default 30)

local prefix = ARGV[1]
local lock_name = ARGV[2]
local owner = ARGV[3]
local ttl = tonumber(ARGV[4]) or 30

local key = prefix .. "lock:" .. lock_name
local ok = redis.call('SET', key, owner, 'NX', 'EX', ttl)
if ok then
    return 1
else
    return 0
end
