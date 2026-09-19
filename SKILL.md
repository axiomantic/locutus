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

# 3. Resolve scripts directory
if [ -z "$A2A_SCRIPTS_DIR" ]; then
  if [ -d "./scripts" ] && [ -f "./scripts/register.lua" ]; then
    export A2A_SCRIPTS_DIR="$(pwd)/scripts"
  elif [ -d "$HOME/.gemini/config/skills/redis-a2a/scripts" ]; then
    export A2A_SCRIPTS_DIR="$HOME/.gemini/config/skills/redis-a2a/scripts"
  elif [ -d ".claude/skills/redis-a2a/scripts" ]; then
    export A2A_SCRIPTS_DIR="$(pwd)/.claude/skills/redis-a2a/scripts"
  else
    export A2A_SCRIPTS_DIR="$(pwd)/scripts"
  fi
fi

# 4. Docker Auto-Start Check:
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

# 5. Host CLI check: If redis-cli is not installed on the host, route via Docker
if ! command -v redis-cli >/dev/null 2>&1; then
  if command -v docker >/dev/null 2>&1 && [ "$(docker ps -q -f name=^/${A2A_CONTAINER}$)" ]; then
    redis-cli() {
      docker exec -i "$A2A_CONTAINER" redis-cli "$@"
    }
  fi
fi
```

---

## 3. Lua Script Inventory (`scripts/`)

All server-side coordination is executed atomically via Lua scripts located in `$A2A_SCRIPTS_DIR`.
Execute them cleanly using:
```bash
redis-cli -u "${A2A_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$A2A_SCRIPTS_DIR/<script>.lua")" 0 [args...]
```

| Script File | Purpose | Parameters (ARGV) |
| :--- | :--- | :--- |
| [`register.lua`](scripts/register.lua) | Register identity, index tags, and arm heartbeat | `ARGV[1]: prefix`, `ARGV[2]: name`, `ARGV[3]: tags_csv`, `ARGV[4]: ttl_sec` |
| [`send_o2o.lua`](scripts/send_o2o.lua) | Direct message to recipient inbox with queue TTL | `ARGV[1]: prefix`, `ARGV[2]: recipient`, `ARGV[3]: msg_json`, `ARGV[4]: inbox_ttl` |
| [`multicast.lua`](scripts/multicast.lua) | Fan-out to tag or `*` with dead-agent pruning | `ARGV[1]: prefix`, `ARGV[2]: target_tag`, `ARGV[3]: msg_json`, `ARGV[4]: inbox_ttl` |
| [`drain.lua`](scripts/drain.lua) | Atomically pop up to N pending messages from inbox | `ARGV[1]: prefix`, `ARGV[2]: name`, `ARGV[3]: max_count` |
| [`directory.lua`](scripts/directory.lua) | List all active agents, heartbeat status, and tags | `ARGV[1]: prefix` |
| [`unregister.lua`](scripts/unregister.lua) | Clean logout, remove from roster and indexed tags | `ARGV[1]: prefix`, `ARGV[2]: name` |

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
redis-cli -u "${A2A_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$A2A_SCRIPTS_DIR/register.lua")" 0 "$A2A_REDIS_PREFIX" "$MY_NAME" "$MY_TAGS" 150

# Catch-up / Drain any existing backlog
redis-cli -u "${A2A_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$A2A_SCRIPTS_DIR/drain.lua")" 0 "$A2A_REDIS_PREFIX" "$MY_NAME" 50
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

redis-cli -u "${A2A_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$A2A_SCRIPTS_DIR/send_o2o.lua")" 0 "$A2A_REDIS_PREFIX" "bob" "$MSG_JSON" 604800
```

### Sending Multicast (O2M by Tag or `*`)
```bash
# Send to all agents with tag "qa" (or "*" for all agents)
redis-cli -u "${A2A_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$A2A_SCRIPTS_DIR/multicast.lua")" 0 "$A2A_REDIS_PREFIX" "qa" "$MSG_JSON" 604800
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

redis-cli -u "${A2A_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$A2A_SCRIPTS_DIR/send_o2o.lua")" 0 "$A2A_REDIS_PREFIX" "$ORIGINAL_FROM" "$REPLY_JSON" 604800
```

---

## 7. Discovering Peers (`who`)

To see who is online and what tags they handle:

```bash
redis-cli -u "${A2A_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$A2A_SCRIPTS_DIR/directory.lua")" 0 "$A2A_REDIS_PREFIX"
```

---

## 8. Graceful Exit / Retirement

When the operator ends the session or retires your agent:

```bash
redis-cli -u "${A2A_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$A2A_SCRIPTS_DIR/unregister.lua")" 0 "$A2A_REDIS_PREFIX" "$MY_NAME"
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
