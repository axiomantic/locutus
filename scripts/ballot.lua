-- ballot.lua
-- Blind Voting & Consensus Primitives for Roundtables and Multi-Agent Panels
-- Keys:
--   ballot_meta: prefix .. "ballot:" .. ballot_id (Hash)
--   ballot_votes: prefix .. "ballot:votes:" .. ballot_id (Hash: voter -> choice)

local prefix = ARGV[1]
if not prefix or prefix == "" then
    return redis.error_reply("ERR: Missing prefix")
end

local action = ARGV[2]
if not action or action == "" then
    return redis.error_reply("ERR: Missing action")
end

local ballot_id = ARGV[3]
if not ballot_id or ballot_id == "" then
    return redis.error_reply("ERR: Missing ballot_id")
end

local meta_key = prefix .. "ballot:{" .. ballot_id .. "}"
local votes_key = prefix .. "ballot:votes:{" .. ballot_id .. "}"

local function split(str, sep)
    local t = {}
    for match in string.gmatch(str, "([^" .. sep .. "]+)") do
        local trimmed = match:match("^%s*(.-)%s*$")
        if trimmed ~= "" then
            table.insert(t, trimmed)
        end
    end
    return t
end

if action == "open" then
    local options_raw = ARGV[4] or ""
    local voters_raw = ARGV[5] or "*"
    local ttl = tonumber(ARGV[6]) or 3600
    local ts = (ARGV[7] and ARGV[7] ~= "") and ARGV[7] or tostring(redis.call("TIME")[1])

    if options_raw == "" then
        return redis.error_reply("ERR: Missing options for ballot")
    end

    redis.call("HSET", meta_key,
        "options", options_raw,
        "voters", voters_raw,
        "status", "open",
        "created_at", ts
    )
    redis.call("EXPIRE", meta_key, ttl)
    redis.call("EXPIRE", votes_key, ttl)

    return "OPEN"

elseif action == "cast" then
    local voter = ARGV[4]
    local choice = ARGV[5]

    if not voter or voter == "" then
        return redis.error_reply("ERR: Missing voter")
    end
    if not choice or choice == "" then
        return redis.error_reply("ERR: Missing vote choice")
    end

    local status = redis.call("HGET", meta_key, "status")
    if not status or status ~= "open" then
        return redis.error_reply("ERR: Ballot is not open")
    end

    local options_raw = redis.call("HGET", meta_key, "options") or ""
    local opts = split(options_raw, ",")
    local valid_opt = false
    for _, opt in ipairs(opts) do
        if opt == choice then
            valid_opt = true
            break
        end
    end
    if not valid_opt then
        return redis.error_reply("ERR: Invalid vote choice '" .. choice .. "'")
    end

    local voters_raw = redis.call("HGET", meta_key, "voters") or "*"
    if voters_raw ~= "*" and voters_raw ~= "" then
        local eligible = split(voters_raw, ",")
        local is_eligible = false
        for _, v in ipairs(eligible) do
            if v == voter then
                is_eligible = true
                break
            end
        end
        if not is_eligible then
            return redis.error_reply("ERR: Voter '" .. voter .. "' is not eligible for this ballot")
        end
    end

    local sig = (ARGV[6] and ARGV[6] ~= "") and ARGV[6] or ""
    local ts = (ARGV[7] and ARGV[7] ~= "") and ARGV[7] or tostring(redis.call("TIME")[1])
    redis.call("HSET", votes_key, voter, choice)
    local sigs_key = prefix .. "ballot:votes_sig:{" .. ballot_id .. "}"
    if sig ~= "" then
        redis.call("HSET", sigs_key, voter, sig .. "|" .. ts)
        local ttl = redis.call("TTL", meta_key)
        if ttl > 0 then
            redis.call("EXPIRE", sigs_key, ttl)
        end
    end
    return "VOTED"

elseif action == "tally" then
    local close_flag = ARGV[4] or ""
    local status = redis.call("HGET", meta_key, "status")
    if not status then
        return redis.error_reply("ERR: Ballot not found")
    end

    if close_flag == "close" or close_flag == "true" or close_flag == "1" then
        redis.call("HSET", meta_key, "status", "closed")
        status = "closed"
    end

    local options_raw = redis.call("HGET", meta_key, "options") or ""
    local opts = split(options_raw, ",")
    local tally = {}
    for _, opt in ipairs(opts) do
        tally[opt] = 0
    end

    local sigs_key = prefix .. "ballot:votes_sig:{" .. ballot_id .. "}"
    local raw_votes = redis.call("HGETALL", votes_key)
    local total_votes = 0
    local vote_entries = {}
    for i = 1, #raw_votes, 2 do
        local voter = raw_votes[i]
        local choice = raw_votes[i + 1]
        local sig_entry = redis.call("HGET", sigs_key, voter) or ""
        vote_entries[voter] = { choice = choice, sig_entry = sig_entry }
        tally[choice] = (tally[choice] or 0) + 1
        total_votes = total_votes + 1
    end

    local max_count = -1
    local winner = ""
    for _, opt in ipairs(opts) do
        local cnt = tally[opt] or 0
        if cnt > max_count then
            max_count = cnt
            winner = opt
        end
    end

    local res = {
        ballot_id = ballot_id,
        status = status,
        total_votes = total_votes,
        tally = tally,
        winner = winner,
        votes = vote_entries
    }
    return cjson.encode(res)

elseif action == "status" then
    local status = redis.call("HGET", meta_key, "status")
    if not status then
        return redis.error_reply("ERR: Ballot not found")
    end
    local options_raw = redis.call("HGET", meta_key, "options") or ""
    local voters_raw = redis.call("HGET", meta_key, "voters") or "*"
    local total_votes = redis.call("HLEN", votes_key)

    local res = {
        ballot_id = ballot_id,
        status = status,
        options = split(options_raw, ","),
        voters = voters_raw,
        voted_count = total_votes
    }
    return cjson.encode(res)

else
    return redis.error_reply("ERR: Unknown ballot action '" .. tostring(action) .. "'")
end
