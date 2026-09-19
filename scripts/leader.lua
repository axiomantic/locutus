-- leader.lua
-- Leader Election via Lease Preemption for Self-Healing Meshes and Orchestrators
-- Keys:
--   leader_key: prefix .. "leader:" .. role (JSON string with TTL)
--   leader_chan: prefix .. "channel:leader:" .. role (PubSub)

local prefix = ARGV[1]
if not prefix or prefix == "" then
    return redis.error_reply("ERR: Missing prefix")
end

local action = ARGV[2]
if not action or action == "" then
    return redis.error_reply("ERR: Missing action")
end

local role = ARGV[3]
if not role or role == "" then
    return redis.error_reply("ERR: Missing role")
end

local leader_key = prefix .. "leader:{" .. role .. "}"
local leader_chan = prefix .. "channel:leader:{" .. role .. "}"

if action == "acquire" then
    local agent = ARGV[4]
    local lease_sec = tonumber(ARGV[5]) or 30
    local ts = (ARGV[6] and ARGV[6] ~= "") and ARGV[6] or tostring(redis.call("TIME")[1])
    local sig = (ARGV[7] and ARGV[7] ~= "") and ARGV[7] or ""
    local force = ARGV[8] or ""

    if not agent or agent == "" then
        return redis.error_reply("ERR: Missing agent")
    end

    local existing = redis.call("GET", leader_key)
    if existing and force ~= "force" then
        local current_leader = ""
        local ok, data = pcall(cjson.decode, existing)
        if ok and data and data.leader then
            current_leader = data.leader
        else
            current_leader = tostring(existing)
        end

        if current_leader == agent then
            local obj = {
                role = role,
                leader = agent,
                acquired_at = ts,
                lease_sec = lease_sec,
                sig = sig
            }
            local payload = cjson.encode(obj)
            redis.call("SET", leader_key, payload, "EX", lease_sec)
            return "ELECTED"
        else
            return "HELD:" .. current_leader
        end
    end

    local obj = {
        role = role,
        leader = agent,
        acquired_at = ts,
        lease_sec = lease_sec,
        sig = sig
    }
    local payload = cjson.encode(obj)
    redis.call("SET", leader_key, payload, "EX", lease_sec)
    redis.call("PUBLISH", leader_chan, payload)
    return "ELECTED"

elseif action == "renew" then
    local agent = ARGV[4]
    local lease_sec = tonumber(ARGV[5]) or 30
    local ts = (ARGV[6] and ARGV[6] ~= "") and ARGV[6] or tostring(redis.call("TIME")[1])
    local sig = (ARGV[7] and ARGV[7] ~= "") and ARGV[7] or ""

    if not agent or agent == "" then
        return redis.error_reply("ERR: Missing agent")
    end

    local existing = redis.call("GET", leader_key)
    if not existing then
        return redis.error_reply("ERR: No active leader")
    end

    local current_leader = ""
    local ok, data = pcall(cjson.decode, existing)
    if ok and data and data.leader then
        current_leader = data.leader
    else
        current_leader = tostring(existing)
    end

    if current_leader ~= agent then
        return redis.error_reply("ERR: Not leader")
    end

    local obj = {
        role = role,
        leader = agent,
        acquired_at = ts,
        lease_sec = lease_sec,
        sig = sig
    }
    local payload = cjson.encode(obj)
    redis.call("SET", leader_key, payload, "EX", lease_sec)
    return "RENEWED"

elseif action == "resign" then
    local agent = ARGV[4]

    local existing = redis.call("GET", leader_key)
    if not existing then
        return "RESIGNED"
    end

    local current_leader = ""
    local ok, data = pcall(cjson.decode, existing)
    if ok and data and data.leader then
        current_leader = data.leader
    else
        current_leader = tostring(existing)
    end

    if current_leader == agent then
        redis.call("DEL", leader_key)
        local event = {
            role = role,
            event = "resigned",
            by = agent
        }
        redis.call("PUBLISH", leader_chan, cjson.encode(event))
        return "RESIGNED"
    else
        return redis.error_reply("ERR: Not leader")
    end

elseif action == "status" then
    local existing = redis.call("GET", leader_key)
    if not existing then
        local res = {
            role = role,
            leader = "",
            status = "vacant",
            ttl = 0
        }
        return cjson.encode(res)
    end

    local ttl = redis.call("TTL", leader_key)
    local ok, data = pcall(cjson.decode, existing)
    if ok and data then
        data.ttl = ttl
        data.status = "active"
        return cjson.encode(data)
    else
        local res = {
            role = role,
            leader = tostring(existing),
            status = "active",
            ttl = ttl
        }
        return cjson.encode(res)
    end

else
    return redis.error_reply("ERR: Unknown leader action '" .. tostring(action) .. "'")
end
