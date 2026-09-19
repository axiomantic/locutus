-- scripts/send_o2o.lua
-- Pushes a direct message to a recipient's inbox and sets/refreshes inbox TTL.
-- ARGV[1]: prefix (e.g. "a2a:")
-- ARGV[2]: recipient agent name (e.g. "bob")
-- ARGV[3]: message JSON string
-- ARGV[4]: inbox TTL seconds (default 604800 = 7 days)

local prefix = ARGV[1]
local recipient = ARGV[2]
local msg_json = ARGV[3]
local inbox_ttl = tonumber(ARGV[4]) or 604800

redis.call('LPUSH', prefix .. 'inbox:' .. recipient, msg_json)
redis.call('EXPIRE', prefix .. 'inbox:' .. recipient, inbox_ttl)
return "OK"
