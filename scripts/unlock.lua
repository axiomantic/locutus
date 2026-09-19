-- scripts/unlock.lua
-- Atomically releases a distributed lock only if held by the requesting owner.
-- ARGV[1]: prefix (e.g. "locutus:")
-- ARGV[2]: lock name (e.g. "git_rebase")
-- ARGV[3]: owner agent name (e.g. "alice")

local prefix = ARGV[1]
local lock_name = ARGV[2]
local owner = ARGV[3]

local key = prefix .. "lock:{" .. lock_name .. "}"
local current_owner = redis.call('GET', key)
if current_owner == owner then
    return redis.call('DEL', key)
else
    return 0
end
