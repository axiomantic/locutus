---
name: redis-a2a
description: "Zero-glue cross-assistant communication bus over Redis. Enables any coding assistant (Claude Code, Antigravity, Cursor, Windsurf, Aider, Copilot) to register, discover peers, send direct (O2O) and multicast (O2M) work messages, receive backlogged offline messages, and maintain an event-driven background listener using embedded Redis Lua scripts."
---

# Redis Agent-to-Agent (A2A) Communication Bus

A 100% prompt-based protocol and instruction set that turns any Redis instance into a zero-token-idle, multi-agent coordination bus without requiring any external JavaScript, Python packages, or background daemons.

---

## 1. Quick Reference & Core Invariants

1. **Zero External Glue**: You do not write or execute helper scripts from disk. You execute standard `redis-cli` commands with embedded Lua scripts provided directly in this document.
2. **Namespace Isolation**: All keys are prefixed with `$A2A_REDIS_PREFIX` (default: `a2a:`). Never touch or query keys outside this prefix to avoid clobbering existing Redis usage.
3. **Queue Architecture (Fan-Out on Send)**:
   - Every agent listens to exactly **one** primitive: `${A2A_REDIS_PREFIX}inbox:<my_name>`.
   - **Direct (O2O)**: Pushed straight to the recipient's inbox.
   - **Multicast (O2M)**: The sender's Lua script queries the tag roster, prunes expired agents, and fans out directly into each recipient's inbox.
   - **Offline Queuing**: Because messages are kept in the recipient's inbox list, any offline or disconnected agent receives its full backlog upon startup/reconnect.
4. **Heartbeat Cadence via Listener Timeout**:
   - The background listener runs `BRPOP ${A2A_REDIS_PREFIX}inbox:<my_name> 90`.
   - When idle, it exits every 90 seconds. The assistant refreshes its heartbeat (`EX 150`) and immediately re-arms `BRPOP`.
   - When a message arrives, `BRPOP` exits immediately with the payload.
   - **Result**: Zero CPU, zero polling tokens while idle, with an automatic liveness heartbeat.
5. **Payload Boundaries & Loop Prevention**:
   - Messages are trusted and contain work.
   - Keep messages under ~10KB. If transferring large diffs, test logs, or files, pass absolute file paths or git commit SHAs instead of raw dumps.
   - **Reply Storm Prevention**: Replies must ALWAYS be unicast (O2O) to the original `from` sender, never sent back to a group tag or `*`.

---

## 2. Configuration & Connection Discovery

Before executing any commands, resolve your Redis connection string and configuration:

```bash
# 1. Resolve A2A_REDIS_URL with precedence:
#    a. Environment variable A2A_REDIS_URL
#    b. Override in AGENTS.md (e.g., "a2a_redis_url: ..." or "A2A_REDIS_URL=...")
#    c. Local .env file (A2A_REDIS_URL=...)
#    d. Fallback to generic REDIS_URL if set
#    e. ~/.redis_a2a_env
#    f. Default to local/Docker container: redis://127.0.0.1:6379
if [ -z "$A2A_REDIS_URL" ]; then
  if [ -f AGENTS.md ] && grep -Ei '^(a2a_redis_url|A2A_REDIS_URL)[:=]' AGENTS.md >/dev/null 2>&1; then
    export A2A_REDIS_URL=$(grep -Ei '^(a2a_redis_url|A2A_REDIS_URL)[:=]' AGENTS.md | head -n 1 | sed -E 's/^[^:=]+[:=][[:space:]]*//' | tr -d '"' | tr -d "'")
  elif [ -f .env ] && grep -q '^A2A_REDIS_URL=' .env; then
    export A2A_REDIS_URL=$(grep '^A2A_REDIS_URL=' .env | cut -d '=' -f2- | tr -d '"' | tr -d "'")
  elif [ -n "$REDIS_URL" ]; then
    export A2A_REDIS_URL="$REDIS_URL"
  elif [ -f ~/.redis_a2a_env ]; then
    source ~/.redis_a2a_env
    export A2A_REDIS_URL="${A2A_REDIS_URL:-$REDIS_URL}"
  fi
fi
# 2. Key prefix (defaults to "a2a:")
if [ -z "$A2A_REDIS_PREFIX" ]; then
  if [ -f AGENTS.md ] && grep -Ei '^(a2a_redis_prefix|A2A_REDIS_PREFIX)[:=]' AGENTS.md >/dev/null 2>&1; then
    export A2A_REDIS_PREFIX=$(grep -Ei '^(a2a_redis_prefix|A2A_REDIS_PREFIX)[:=]' AGENTS.md | head -n 1 | sed -E 's/^[^:=]+[:=][[:space:]]*//' | tr -d '"' | tr -d "'")
  elif [ -f .env ] && grep -q '^A2A_REDIS_PREFIX=' .env; then
    export A2A_REDIS_PREFIX=$(grep '^A2A_REDIS_PREFIX=' .env | cut -d '=' -f2- | tr -d '"' | tr -d "'")
  elif [ -n "$A2A_PREFIX" ]; then
    export A2A_REDIS_PREFIX="$A2A_PREFIX"
  fi
fi
export A2A_REDIS_PREFIX="${A2A_REDIS_PREFIX:-a2a:}"

# 3. Docker Auto-Start Check:
# If connecting to localhost and Redis is not responding, ensure the a2a-redis container is running
export A2A_CONTAINER="${A2A_CONTAINER:-a2a-redis}"
if [[ "$A2A_REDIS_URL" == *"127.0.0.1"* || "$A2A_REDIS_URL" == *"localhost"* ]]; then
  if command -v docker >/dev/null 2>&1; then
    if [ -z "$(docker ps -q -f name=^/${A2A_CONTAINER}$)" ]; then
      if [ "$(docker ps -aq -f name=^/${A2A_CONTAINER}$)" ]; then
        echo "Starting existing ${A2A_CONTAINER} container..."
        docker start "$A2A_CONTAINER"
      else
        echo "Launching new ${A2A_CONTAINER} container..."
        docker run -d --name "$A2A_CONTAINER" -p 6379:6379 redis:alpine
      fi
      sleep 1
    fi
  fi
fi

# 4. Host CLI check: If redis-cli is not installed on the host, route via Docker
if ! command -v redis-cli >/dev/null 2>&1; then
  if command -v docker >/dev/null 2>&1 && [ "$(docker ps -q -f name=^/${A2A_CONTAINER}$)" ]; then
    redis-cli() {
      docker exec -i "$A2A_CONTAINER" redis-cli "$@"
    }
  fi
fi
```

---

## 3. The Embedded Lua Scripts

Use these exact Lua snippets inside `redis-cli -u "${A2A_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "<LUA>" 0 ...`.

### Script A: Registration & Heartbeat Refresh (`LUA_REGISTER`)
Registers agent name, assigns comma-separated tags, records metadata, and sets an expiring heartbeat.

```lua
local prefix = ARGV[1]
local name = ARGV[2]
local tags_csv = ARGV[3] or ""
local ttl = tonumber(ARGV[4]) or 150
local now = redis.call('TIME')[1]

-- 1. Refresh Heartbeat
redis.call('SET', prefix .. 'heartbeat:' .. name, '1', 'EX', ttl)

-- 2. Add to active roster and store metadata
redis.call('SADD', prefix .. 'active_agents', name)
redis.call('HSET', prefix .. 'agent:' .. name, 'tags', tags_csv, 'last_seen', now)

-- 3. Index tags
for tag in string.gmatch(tags_csv, "([^,]+)") do
    local trimmed = string.match(tag, "^%s*(.-)%s*$")
    if trimmed ~= "" then
        redis.call('SADD', prefix .. 'tag:' .. trimmed, name)
    end
end
return "OK"
```

### Script B: Direct O2O Send (`LUA_SEND_O2O`)
Appends a message to the target agent's persistent inbox and refreshes inbox TTL (default 7 days) to prevent orphaned key accumulation.

```lua
local prefix = ARGV[1]
local recipient = ARGV[2]
local msg_json = ARGV[3]
local inbox_ttl = tonumber(ARGV[4]) or 604800

redis.call('LPUSH', prefix .. 'inbox:' .. recipient, msg_json)
redis.call('EXPIRE', prefix .. 'inbox:' .. recipient, inbox_ttl)
return "OK"
```

### Script C: Multicast O2M Send with Auto-Pruning (`LUA_MULTICAST`)
Fans out to all agents matching a tag (or `*` for all). Automatically prunes dead agents whose heartbeats expired, preventing phantom inbox accumulation.

```lua
local prefix = ARGV[1]
local target_tag = ARGV[2]
local msg_json = ARGV[3]
local targets = {}

if target_tag == "*" then
    targets = redis.call('SMEMBERS', prefix .. 'active_agents')
else
    targets = redis.call('SMEMBERS', prefix .. 'tag:' .. target_tag)
end

local delivered = 0
for _, agent in ipairs(targets) do
    if redis.call('EXISTS', prefix .. 'heartbeat:' .. agent) == 1 then
        redis.call('LPUSH', prefix .. 'inbox:' .. agent, msg_json)
        redis.call('EXPIRE', prefix .. 'inbox:' .. agent, 604800)
        delivered = delivered + 1
    else
        -- Prune inactive agent from registry
        if target_tag == "*" then
            redis.call('SREM', prefix .. 'active_agents', agent)
        else
            redis.call('SREM', prefix .. 'tag:' .. target_tag, agent)
        end
    end
end
return delivered
```

### Script D: Catch-Up Batch Drain (`LUA_DRAIN`)
Atomically drains up to $N$ backlogged messages from an inbox. Used during boot, reconnection, or turn start.

```lua
local prefix = ARGV[1]
local name = ARGV[2]
local count = tonumber(ARGV[3]) or 50
local messages = {}

for i = 1, count do
    local msg = redis.call('RPOP', prefix .. 'inbox:' .. name)
    if not msg then break end
    table.insert(messages, msg)
end
return messages
```

### Script E: Directory & Peer Discovery (`LUA_DIRECTORY`)
Lists all registered agents, their active heartbeat status (`1` or `0`), and their tags.

```lua
local prefix = ARGV[1]
local agents = redis.call('SMEMBERS', prefix .. 'active_agents')
local result = {}

for _, agent in ipairs(agents) do
    local alive = redis.call('EXISTS', prefix .. 'heartbeat:' .. agent)
    local tags = redis.call('HGET', prefix .. 'agent:' .. agent, 'tags') or ""
    table.insert(result, agent .. "|" .. tostring(alive) .. "|" .. tags)
end
return result
```

### Script F: Clean Unregister / Shutdown (`LUA_UNREGISTER`)
Removes an agent from the active directory, its indexed tags, and deletes its heartbeat.

```lua
local prefix = ARGV[1]
local name = ARGV[2]
local tags_csv = redis.call('HGET', prefix .. 'agent:' .. name, 'tags') or ""

redis.call('DEL', prefix .. 'heartbeat:' .. name)
redis.call('SREM', prefix .. 'active_agents', name)
redis.call('DEL', prefix .. 'agent:' .. name)

for tag in string.gmatch(tags_csv, "([^,]+)") do
    local trimmed = string.match(tag, "^%s*(.-)%s*$")
    if trimmed ~= "" then
        redis.call('SREM', prefix .. 'tag:' .. trimmed, name)
    end
end
return "OK"
```

---

## 4. Wire Protocol (JSON Envelope)

All messages pushed to `a2a:inbox:*` must adhere strictly to this schema:

```json
{
  "id": "msg_1710789000_alice_9f8a",
  "from": "alice",
  "to": "bob",
  "type": "task",
  "reply_to": null,
  "tags": ["ticket-104"],
  "subject": "Review auth parser changes",
  "body": "Please inspect src/auth.ts and verify if token expiry handles leap years.",
  "timestamp": "2026-09-18T23:35:00Z"
}
```

### Message Types
| Type | Purpose | Expected Response |
| :--- | :--- | :--- |
| `task` | Actionable work (write code, run tests, research) | Execute work $\rightarrow$ reply with `reply` |
| `query` | Question or request for status/info | Answer question $\rightarrow$ reply with `reply` |
| `reply` | Direct answer to a previous `id` | Threaded response (uses `reply_to: "<id>"`) |
| `status` | State announcement (e.g. "task completed", "idle") | Informational, typically no reply |

---

## 5. Step-by-Step Lifecycle Guide for Assistants

### Step 1: Initialize Identity
Choose a unique ephemeral name and gather relevant tags:
- Name format: `<role>-<random_hex>` or `<project>-<branch>` (e.g., `worker-3a1b`, `reviewer-main`)
- Tags: Responsibilities, active tickets, or capabilities (e.g., `qa,ticket-42,linux`)

### Step 2: Register & Catch Up on Backlog
Execute Script A to claim your name, and Script D to drain any pre-existing messages:

```bash
# Register
redis-cli -u "${A2A_REDIS_URL:-redis://127.0.0.1:6379}" EVAL '
local p,n,t,ttl,now = ARGV[1],ARGV[2],ARGV[3] or "",tonumber(ARGV[4]) or 150,redis.call("TIME")[1]
redis.call("SET", p.."heartbeat:"..n, "1", "EX", ttl)
redis.call("SADD", p.."active_agents", n)
redis.call("HSET", p.."agent:"..n, "tags", t, "last_seen", now)
for tag in string.gmatch(t, "([^,]+)") do
    local tr = string.match(tag, "^%s*(.-)%s*$")
    if tr ~= "" then redis.call("SADD", p.."tag:"..tr, n) end
end
return "OK"
' 0 "$A2A_REDIS_PREFIX" "$MY_NAME" "$MY_TAGS" 150

# Catch-up / Drain any existing backlog
redis-cli -u "${A2A_REDIS_URL:-redis://127.0.0.1:6379}" EVAL '
local p,n,c = ARGV[1],ARGV[2],tonumber(ARGV[3]) or 50
local res = {}
for i=1,c do
    local m = redis.call("RPOP", p.."inbox:"..n)
    if not m then break end
    table.insert(res, m)
end
return res
' 0 "$A2A_REDIS_PREFIX" "$MY_NAME" 50
```

*If any messages are returned from the drain command, process them immediately!*

### Step 3: Arm the Background Listener
Launch a background command that blocks until a message arrives or timeout expires:

```bash
redis-cli -u "${A2A_REDIS_URL:-redis://127.0.0.1:6379}" BRPOP "${A2A_REDIS_PREFIX}inbox:${MY_NAME}" 90
```

- **In assistants with background completion notifications (e.g., Claude Code, Antigravity)**:
  - Do NOT poll in a loop. Stop calling tools and wait for the notification.
  - When the background command finishes, inspect the output.
- **Handling Listener Output**:
  - **Case A: Output is `(nil)` (90s Timeout)**:
    - Refresh heartbeat:
      ```bash
      redis-cli -u "${A2A_REDIS_URL:-redis://127.0.0.1:6379}" SET "${A2A_REDIS_PREFIX}heartbeat:${MY_NAME}" 1 EX 150
      ```
    - Immediately re-arm:
      ```bash
      redis-cli -u "${A2A_REDIS_URL:-redis://127.0.0.1:6379}" BRPOP "${A2A_REDIS_PREFIX}inbox:${MY_NAME}" 90
      ```
  - **Case B: Output contains a message**:
    - `BRPOP` returns two lines: `1) "a2a:inbox:<name>"` and `2) "{\"id\": ...}"`.
    - Parse the JSON payload.
    - Process the instructions (run tests, edit files, research).
    - If `type` is `task` or `query`, send a reply back to `from`.
    - Re-arm the listener immediately:
      ```bash
      redis-cli -u "${A2A_REDIS_URL:-redis://127.0.0.1:6379}" BRPOP "${A2A_REDIS_PREFIX}inbox:${MY_NAME}" 90
      ```

---

## 6. Sending Messages

### Sending Direct (O2O)
```bash
MSG_JSON='{
  "id": "msg_'$(date +%s)'_'${MY_NAME}'_'$RANDOM'",
  "from": "'${MY_NAME}'",
  "to": "bob",
  "type": "task",
  "reply_to": null,
  "tags": ["code-review"],
  "subject": "Review PR 12",
  "body": "Can you check the latest commit on branch fix-redis?",
  "timestamp": "'$(date -u +"%Y-%m-%dT%H:%M:%SZ")'"
}'

redis-cli -u "${A2A_REDIS_URL:-redis://127.0.0.1:6379}" EVAL '
local p,r,m,ttl = ARGV[1],ARGV[2],ARGV[3],tonumber(ARGV[4]) or 604800
redis.call("LPUSH", p.."inbox:"..r, m)
redis.call("EXPIRE", p.."inbox:"..r, ttl)
return "OK"
' 0 "$A2A_REDIS_PREFIX" "bob" "$MSG_JSON" 604800
```

### Sending Multicast (O2M by Tag or `*`)
```bash
# Send to all agents with tag "qa" (or "*" for all agents)
redis-cli -u "${A2A_REDIS_URL:-redis://127.0.0.1:6379}" EVAL '
local p,tag,m = ARGV[1],ARGV[2],ARGV[3]
local targets = (tag == "*") and redis.call("SMEMBERS", p.."active_agents") or redis.call("SMEMBERS", p.."tag:"..tag)
local count = 0
for _, a in ipairs(targets) do
    if redis.call("EXISTS", p.."heartbeat:"..a) == 1 then
        redis.call("LPUSH", p.."inbox:"..a, m)
        redis.call("EXPIRE", p.."inbox:"..a, 604800)
        count = count + 1
    else
        if tag == "*" then redis.call("SREM", p.."active_agents", a) else redis.call("SREM", p.."tag:"..tag, a) end
    end
end
return count
' 0 "$A2A_REDIS_PREFIX" "qa" "$MSG_JSON"
```

### Replying to a Message
Always reply unicast to the sender's name (`from`), setting `reply_to` to the original message's `id`:

```bash
REPLY_JSON='{
  "id": "msg_'$(date +%s)'_'${MY_NAME}'_'$RANDOM'",
  "from": "'${MY_NAME}'",
  "to": "'${ORIGINAL_FROM}'",
  "type": "reply",
  "reply_to": "'${ORIGINAL_MSG_ID}'",
  "tags": [],
  "subject": "Re: '${ORIGINAL_SUBJECT}'",
  "body": "The tests passed successfully. No regressions found.",
  "timestamp": "'$(date -u +"%Y-%m-%dT%H:%M:%SZ")'"
}'

redis-cli -u "${A2A_REDIS_URL:-redis://127.0.0.1:6379}" EVAL '
local p,r,m = ARGV[1],ARGV[2],ARGV[3]
redis.call("LPUSH", p.."inbox:"..r, m)
redis.call("EXPIRE", p.."inbox:"..r, 604800)
return "OK"
' 0 "$A2A_REDIS_PREFIX" "$ORIGINAL_FROM" "$REPLY_JSON"
```

---

## 7. Discovering Peers (`who`)

To see who is online and what tags they handle:

```bash
redis-cli -u "${A2A_REDIS_URL:-redis://127.0.0.1:6379}" EVAL '
local p = ARGV[1]
local agents = redis.call("SMEMBERS", p.."active_agents")
local out = {}
for _, a in ipairs(agents) do
    local alive = redis.call("EXISTS", p.."heartbeat:"..a)
    local tags = redis.call("HGET", p.."agent:"..a, "tags") or ""
    table.insert(out, a.." | alive="..tostring(alive).." | tags="..tags)
end
return out
' 0 "$A2A_REDIS_PREFIX"
```

---

## 8. Graceful Exit / Retirement

When the operator ends the session or retires your agent:

```bash
redis-cli -u "${A2A_REDIS_URL:-redis://127.0.0.1:6379}" EVAL '
local p,n = ARGV[1],ARGV[2]
local t = redis.call("HGET", p.."agent:"..n, "tags") or ""
redis.call("DEL", p.."heartbeat:"..n)
redis.call("SREM", p.."active_agents", n)
redis.call("DEL", p.."agent:"..n)
for tag in string.gmatch(t, "([^,]+)") do
    local tr = string.match(tag, "^%s*(.-)%s*$")
    if tr ~= "" then redis.call("SREM", p.."tag:"..tr, n) end
end
return "OK"
' 0 "$A2A_REDIS_PREFIX" "$MY_NAME"
```

---

## 9. Fallback: Pure Python (When `redis-cli` is Missing)

If `redis-cli` is not installed, run commands using this standard library Python one-liner (zero pip packages required):

```bash
python3 -c '
import urllib.parse, socket, sys, os

url = urllib.parse.urlparse(os.environ.get("A2A_REDIS_URL", os.environ.get("REDIS_URL", "redis://127.0.0.1:6379")))
s = socket.create_connection((url.hostname or "127.0.0.1", url.port or 6379))
if url.password:
    s.sendall(f"*2\r\n$4\r\nAUTH\r\n${len(url.password)}\r\n{url.password}\r\n".encode())
    s.recv(1024)

# Example: BRPOP
key = sys.argv[1]
cmd = f"*3\r\n$5\r\nBRPOP\r\n${len(key)}\r\n{key}\r\n$2\r\n90\r\n".encode()
s.sendall(cmd)
# Blocks on socket and outputs response when data arrives
resp = s.recv(4096).decode("utf-8", errors="ignore")
print(resp)
' "a2a:inbox:my_name"
```
