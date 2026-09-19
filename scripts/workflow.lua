-- workflow.lua
-- Directed Acyclic Graph (DAG) Workflow Engine for Multi-Agent Task Pipelines
-- Keys:
--   workflow_key: prefix .. "workflow:" .. flow_id (JSON string with TTL)
--   workflow_chan: prefix .. "channel:workflow:" .. flow_id (PubSub)

local prefix = ARGV[1]
local action = ARGV[2]
local flow_id = ARGV[3]

if not flow_id or flow_id == "" then
    return redis.error_reply("ERR: Missing flow_id")
end

local flow_key = prefix .. "workflow:{" .. flow_id .. "}"
local flow_chan = prefix .. "channel:workflow:{" .. flow_id .. "}"

local function plain_split(input, sep)
    local t = {}
    if not input or input == "" then return t end
    local start = 1
    local sep_len = string.len(sep)
    while true do
        local pos = string.find(input, sep, start, true)
        if not pos then
            local part = string.sub(input, start)
            part = part:match("^%s*(.-)%s*$")
            if part ~= "" then table.insert(t, part) end
            break
        end
        local part = string.sub(input, start, pos - 1)
        part = part:match("^%s*(.-)%s*$")
        if part ~= "" then table.insert(t, part) end
        start = pos + sep_len
    end
    return t
end

local function encode_array(arr)
    if not arr or #arr == 0 then
        return "[]"
    end
    return cjson.encode(arr)
end

local function encode_flow(flow)
    local order_json = encode_array(flow.step_order)
    local steps_parts = {}
    for _, name in ipairs(flow.step_order) do
        local st = flow.steps[name]
        local deps_json = encode_array(st.deps)
        local err_part = ""
        if st.error then
            err_part = string.format(',"error":%s', cjson.encode(st.error))
        end
        local st_json = string.format('{"name":%s,"status":%s,"deps":%s,"output":%s,"completed_at":%s%s}',
            cjson.encode(st.name),
            cjson.encode(st.status),
            deps_json,
            cjson.encode(st.output or ""),
            cjson.encode(st.completed_at or ""),
            err_part
        )
        table.insert(steps_parts, string.format('%s:%s', cjson.encode(name), st_json))
    end
    local steps_obj = "{" .. table.concat(steps_parts, ",") .. "}"
    return string.format('{"flow_id":%s,"status":%s,"created_at":%s,"ttl":%d,"step_order":%s,"steps":%s}',
        cjson.encode(flow.flow_id),
        cjson.encode(flow.status),
        cjson.encode(flow.created_at),
        flow.ttl or 86400,
        order_json,
        steps_obj
    )
end

if action == "define" then
    local steps_str = ARGV[4]
    local deps_str = ARGV[5]
    local ttl = tonumber(ARGV[6]) or 86400
    local ts = (ARGV[7] and ARGV[7] ~= "") and ARGV[7] or tostring(redis.call("TIME")[1])

    if not steps_str or steps_str == "" then
        return redis.error_reply("ERR: Missing steps")
    end

    if redis.call("EXISTS", flow_key) == 1 then
        return redis.error_reply("ERR: Workflow already exists")
    end

    local step_names = plain_split(steps_str, ",")
    local steps = {}
    local step_order = {}

    for _, name in ipairs(step_names) do
        steps[name] = {
            name = name,
            status = "ready",
            deps = {},
            output = "",
            completed_at = ""
        }
        table.insert(step_order, name)
    end

    if deps_str and deps_str ~= "" then
        local dep_clauses = plain_split(deps_str, ";")
        for _, clause in ipairs(dep_clauses) do
            local parts = plain_split(clause, ":")
            if #parts == 2 then
                local child = parts[1]
                local parents = plain_split(parts[2], ",")
                if not steps[child] then
                    return redis.error_reply("ERR: Unknown dependency step '" .. child .. "'")
                end
                for _, p in ipairs(parents) do
                    if not steps[p] then
                        return redis.error_reply("ERR: Unknown dependency step '" .. p .. "'")
                    end
                    if p == child then
                        return redis.error_reply("ERR: Cycle detected in workflow dependencies")
                    end
                    table.insert(steps[child].deps, p)
                end
                if #steps[child].deps > 0 then
                    steps[child].status = "pending"
                end
            end
        end
    end

    -- Cycle detection via DFS topological traversal
    local visited = {}
    local rec_stack = {}

    local function check_cycle(node)
        visited[node] = true
        rec_stack[node] = true

        if steps[node] and steps[node].deps then
            for _, parent in ipairs(steps[node].deps) do
                if not visited[parent] then
                    if check_cycle(parent) then
                        return true
                    end
                elseif rec_stack[parent] then
                    return true
                end
            end
        end

        rec_stack[node] = false
        return false
    end

    for _, name in ipairs(step_order) do
        if not visited[name] then
            if check_cycle(name) then
                return redis.error_reply("ERR: Cycle detected in workflow dependencies")
            end
        end
    end

    local flow = {
        flow_id = flow_id,
        status = "running",
        created_at = ts,
        ttl = ttl,
        step_order = step_order,
        steps = steps
    }

    local payload = encode_flow(flow)
    redis.call("SET", flow_key, payload, "EX", ttl)
    redis.call("PUBLISH", flow_chan, cjson.encode({ event = "defined", flow_id = flow_id }))
    return "DEFINED"

elseif action == "next" then
    local raw = redis.call("GET", flow_key)
    if not raw then
        return redis.error_reply("ERR: Workflow not found")
    end

    local flow = cjson.decode(raw)
    local ready = {}
    local pending = {}
    local completed = {}

    for _, name in ipairs(flow.step_order) do
        local st = flow.steps[name]
        if st.status == "ready" then
            table.insert(ready, name)
        elseif st.status == "pending" then
            table.insert(pending, name)
        elseif st.status == "completed" then
            table.insert(completed, name)
        end
    end

    local r_json = encode_array(ready)
    local p_json = encode_array(pending)
    local c_json = encode_array(completed)
    return string.format('{"flow_id":%s,"status":%s,"ready":%s,"pending":%s,"completed":%s}',
        cjson.encode(flow.flow_id),
        cjson.encode(flow.status),
        r_json,
        p_json,
        c_json
    )

elseif action == "resolve" then
    local step_name = ARGV[4]
    local output = ARGV[5] or ""
    local ts = (ARGV[6] and ARGV[6] ~= "") and ARGV[6] or tostring(redis.call("TIME")[1])

    if not step_name or step_name == "" then
        return redis.error_reply("ERR: Missing step name")
    end

    local raw = redis.call("GET", flow_key)
    if not raw then
        return redis.error_reply("ERR: Workflow not found")
    end

    local flow = cjson.decode(raw)
    if not flow.steps[step_name] then
        return redis.error_reply("ERR: Step not found in workflow")
    end

    flow.steps[step_name].status = "completed"
    flow.steps[step_name].output = output
    flow.steps[step_name].completed_at = ts

    local unlocked = {}
    for _, name in ipairs(flow.step_order) do
        local st = flow.steps[name]
        if st.status == "pending" then
            local all_deps_met = true
            -- In decoded JSON, st.deps might be an empty table or list
            if st.deps then
                for _, dep in ipairs(st.deps) do
                    if not flow.steps[dep] or flow.steps[dep].status ~= "completed" then
                        all_deps_met = false
                        break
                    end
                end
            end
            if all_deps_met then
                st.status = "ready"
                table.insert(unlocked, name)
            end
        end
    end

    local all_done = true
    for _, name in ipairs(flow.step_order) do
        if flow.steps[name].status ~= "completed" then
            all_done = false
            break
        end
    end

    if flow.status ~= "failed" then
        if all_done then
            flow.status = "completed"
        else
            flow.status = "running"
        end
    end

    local ttl = redis.call("TTL", flow_key)
    local payload = encode_flow(flow)
    if ttl and ttl > 0 then
        redis.call("SET", flow_key, payload, "EX", ttl)
    else
        redis.call("SET", flow_key, payload)
    end

    local u_json = encode_array(unlocked)
    local evt = string.format('{"event":"step_resolved","flow_id":%s,"step":%s,"status":%s,"unlocked":%s}',
        cjson.encode(flow_id),
        cjson.encode(step_name),
        cjson.encode(flow.status),
        u_json
    )
    redis.call("PUBLISH", flow_chan, evt)

    local res = string.format('{"flow_id":%s,"status":%s,"step":%s,"unlocked":%s}',
        cjson.encode(flow.flow_id),
        cjson.encode(flow.status),
        cjson.encode(step_name),
        u_json
    )
    return res

elseif action == "fail" then
    local step_name = ARGV[4]
    local reason = ARGV[5] or "failed"

    local raw = redis.call("GET", flow_key)
    if not raw then
        return redis.error_reply("ERR: Workflow not found")
    end

    local flow = cjson.decode(raw)
    if step_name and flow.steps[step_name] then
        flow.steps[step_name].status = "failed"
        flow.steps[step_name].error = reason
    end
    flow.status = "failed"

    local ttl = redis.call("TTL", flow_key)
    local payload = encode_flow(flow)
    if ttl and ttl > 0 then
        redis.call("SET", flow_key, payload, "EX", ttl)
    else
        redis.call("SET", flow_key, payload)
    end

    local evt = {
        event = "failed",
        flow_id = flow_id,
        step = step_name,
        error = reason
    }
    redis.call("PUBLISH", flow_chan, cjson.encode(evt))

    return cjson.encode({ flow_id = flow_id, status = "failed", step = step_name, error = reason })

elseif action == "status" then
    local raw = redis.call("GET", flow_key)
    if not raw then
        return redis.error_reply("ERR: Workflow not found")
    end
    return raw

else
    return redis.error_reply("ERR: Unknown workflow action '" .. tostring(action) .. "'")
end
