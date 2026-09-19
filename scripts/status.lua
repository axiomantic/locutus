-- scripts/status.lua
-- Updates an agent's operational state (idle, busy, error) and current activity description.
-- Also refreshes the agent's heartbeat.
-- ARGV[1]: prefix (e.g. "locutus:")
-- ARGV[2]: agent name (e.g. "alice")
-- ARGV[3]: state ("idle", "busy", "error", "offline")
-- ARGV[4]: activity text (e.g. "Running unit tests")
-- ARGV[5]: heartbeat TTL seconds (default 150)

local prefix = ARGV[1]
local name = ARGV[2]
local state = ARGV[3] or "idle"
local activity = ARGV[4] or ""
local ttl = tonumber(ARGV[5]) or 150
local now = redis.call('TIME')[1]

redis.call('SET', prefix .. 'heartbeat:' .. name, '1', 'EX', ttl)
redis.call('HSET', prefix .. 'agent:' .. name, 'state', state, 'activity', activity, 'last_seen', now)
return "OK"
