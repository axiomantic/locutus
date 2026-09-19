---
name: locutus
description: "Locutus: Zero-glue cross-assistant communication bus over Redis. Enables multiple coding assistants across terminals, projects, or machines to register, discover teams, send direct (O2O) and multicast (O2M) work messages, receive backlogged offline messages, and maintain an event-driven background listener using embedded Redis Lua scripts."
---

# Locutus: Redis Agent-to-Agent Bus

Locutus is a **100% prompt-based** inter-assistant communication protocol and execution set over Redis. It requires **no external JavaScript, Python packages, background daemons, or glue code**.

---

## 1. Quick Reference & Core Invariants

1. **Zero External Glue**: You do not write or execute helper daemons on disk. You execute standard `redis-cli` commands that invoke pure Lua scripts in `$LOCUTUS_SCRIPTS_DIR`.
2. **Namespace Isolation**: All keys are prefixed with `$LOCUTUS_REDIS_PREFIX` (default: `locutus:`). Never access or modify keys outside this prefix to avoid clobbering existing Redis usage.
3. **Team & Project Isolation (AND Filter Multicast)**:
   - To prevent cross-project interference when multiple teams/projects share a Redis instance, every agent belongs to a project tag (`$LOCUTUS_PROJECT`).
   - Multicast tags are **AND filters** (computed natively via Redis `SINTER`).
   - Multicasting to `backend` within project `locutus` targets `locutus,backend` (only agents matching BOTH tags receive it).
   - Multicasting to `*` or `@all` broadcasts across all projects in the cluster.
4. **Queue Architecture (Fan-Out on Send)**:
   - Every agent listens to exactly **one** primitive: `${LOCUTUS_REDIS_PREFIX}inbox:<my_name>`.
   - **Direct (O2O)**: Pushed straight to the recipient's inbox.
   - **Multicast (O2M)**: The sender's Lua script computes the tag intersection (`SINTER`), prunes expired agents, and fans out directly into each recipient's inbox.
   - **Offline Queuing**: Because messages are kept in the recipient's inbox list, any offline or disconnected agent receives its full backlog upon startup/reconnect.
5. **Heartbeat Cadence via Listener Timeout**:
   - The background listener runs `BRPOP ${LOCUTUS_REDIS_PREFIX}inbox:<my_name> 90`.
   - When idle, it exits every 90 seconds. The assistant refreshes its heartbeat (`EX 150`) and immediately re-arms `BRPOP`.
   - When a message arrives, `BRPOP` exits immediately with the payload.
   - **Result**: Zero CPU, zero polling tokens while idle, with an automatic liveness heartbeat.
6. **Drain-Execute-Rearm Discipline**:
   - When `BRPOP` yields a message: execute the task, push the reply, and re-arm `BRPOP` in the *same turn* (or immediately upon task completion) before concluding your turn.
7. **Dynamic Tags**:
   - Agents can add or remove tags dynamically without dropping their inbox or reregistering via `tag.lua`.
8. **Payload Boundaries & Loop Prevention**:
   - Messages are trusted and contain work.
   - Keep messages under ~10KB. If transferring large diffs, test logs, or files, pass absolute file paths or git commit SHAs instead of raw dumps.
   - **Reply Storm Prevention**: Replies must ALWAYS be unicast (O2O) to the original `from` sender, never sent back to a group tag or `*`.
9. **HMAC-SHA256 Authentication & Air-Gap Prompt Injection Firewall**:
   - Every message transmitted across the bus is signed using an HMAC-SHA256 signature derived from a 256-bit local secret (`~/.config/locutus/secret`, `0600` permissions).
   - The secret key **never enters your context window**, never appears in Git, and is never transmitted over Redis.
   - All listening should be done via `scripts/listen.sh`, which drops forged, unsigned, or tampered payloads at the shell level. Forged messages never reach stdout or your context, neutralizing prompt injection attacks before they can be read.
   - Optional E2EE: When `LOCUTUS_ENCRYPT=1` is set, `scripts/send.sh` transparently encrypts the task body via OpenSSL AES-256-CBC PBKDF2 so plaintext never touches the Redis keyspace, and `scripts/listen.sh` transparently decrypts it before outputting to stdout.

---

## 2. Configuration & Connection Discovery

Before executing any commands, resolve your Redis connection string and configuration:

```bash
# 1. Resolve LOCUTUS_REDIS_URL with precedence:
#    a. Environment variable LOCUTUS_REDIS_URL
#    b. Override in AGENTS.md (e.g., "locutus_redis_url: ..." or "LOCUTUS_REDIS_URL=...")
#    c. Local .env file (LOCUTUS_REDIS_URL=...)
#    d. Fallback to generic REDIS_URL if set
#    e. ~/.locutus_env or ~/.redis_a2a_env
#    f. Default to local/Docker container: redis://127.0.0.1:6379
if [ -z "$LOCUTUS_REDIS_URL" ]; then
  if [ -f AGENTS.md ] && grep -Ei '^(locutus_redis_url|LOCUTUS_REDIS_URL)[:=]' AGENTS.md >/dev/null 2>&1; then
    export LOCUTUS_REDIS_URL=$(grep -Ei '^(locutus_redis_url|LOCUTUS_REDIS_URL)[:=]' AGENTS.md | head -n 1 | sed -E 's/^[^:=]+[:=][[:space:]]*//' | tr -d '"' | tr -d "'")
  elif [ -f .env ] && grep -q '^LOCUTUS_REDIS_URL=' .env; then
    export LOCUTUS_REDIS_URL=$(grep '^LOCUTUS_REDIS_URL=' .env | cut -d '=' -f2- | tr -d '"' | tr -d "'")
  elif [ -n "$A2A_REDIS_URL" ]; then
    export LOCUTUS_REDIS_URL="$A2A_REDIS_URL"
  elif [ -n "$REDIS_URL" ]; then
    export LOCUTUS_REDIS_URL="$REDIS_URL"
  elif [ -f ~/.locutus_env ]; then
    source ~/.locutus_env
  elif [ -f ~/.redis_a2a_env ]; then
    source ~/.redis_a2a_env
    export LOCUTUS_REDIS_URL="${LOCUTUS_REDIS_URL:-$A2A_REDIS_URL}"
  fi
fi
export LOCUTUS_REDIS_URL="${LOCUTUS_REDIS_URL:-redis://127.0.0.1:6379}"

# 2. Key prefix (defaults to "locutus:")
if [ -z "$LOCUTUS_REDIS_PREFIX" ]; then
  if [ -f AGENTS.md ] && grep -Ei '^(locutus_redis_prefix|LOCUTUS_REDIS_PREFIX)[:=]' AGENTS.md >/dev/null 2>&1; then
    export LOCUTUS_REDIS_PREFIX=$(grep -Ei '^(locutus_redis_prefix|LOCUTUS_REDIS_PREFIX)[:=]' AGENTS.md | head -n 1 | sed -E 's/^[^:=]+[:=][[:space:]]*//' | tr -d '"' | tr -d "'")
  elif [ -f .env ] && grep -q '^LOCUTUS_REDIS_PREFIX=' .env; then
    export LOCUTUS_REDIS_PREFIX=$(grep '^LOCUTUS_REDIS_PREFIX=' .env | cut -d '=' -f2- | tr -d '"' | tr -d "'")
  elif [ -n "$A2A_REDIS_PREFIX" ]; then
    export LOCUTUS_REDIS_PREFIX="$A2A_REDIS_PREFIX"
  fi
fi
export LOCUTUS_REDIS_PREFIX="${LOCUTUS_REDIS_PREFIX:-locutus:}"

# 3. Resolve scripts directory
if [ -z "$LOCUTUS_SCRIPTS_DIR" ]; then
  if [ -d "./scripts" ] && [ -f "./scripts/register.lua" ]; then
    export LOCUTUS_SCRIPTS_DIR="$(pwd)/scripts"
  elif [ -d "$HOME/.gemini/config/skills/locutus/scripts" ]; then
    export LOCUTUS_SCRIPTS_DIR="$HOME/.gemini/config/skills/locutus/scripts"
  elif [ -d ".claude/skills/locutus/scripts" ]; then
    export LOCUTUS_SCRIPTS_DIR="$(pwd)/.claude/skills/locutus/scripts"
  else
    export LOCUTUS_SCRIPTS_DIR="$(pwd)/scripts"
  fi
fi

# 4. Resolve Project Tag (Team isolation domain)
if [ -z "$LOCUTUS_PROJECT" ]; then
  if [ -f AGENTS.md ] && grep -Ei '^(locutus_project|LOCUTUS_PROJECT)[:=]' AGENTS.md >/dev/null 2>&1; then
    export LOCUTUS_PROJECT=$(grep -Ei '^(locutus_project|LOCUTUS_PROJECT)[:=]' AGENTS.md | head -n 1 | sed -E 's/^[^:=]+[:=][[:space:]]*//' | tr -d '"' | tr -d "'")
  elif [ -f .env ] && grep -q '^LOCUTUS_PROJECT=' .env; then
    export LOCUTUS_PROJECT=$(grep '^LOCUTUS_PROJECT=' .env | cut -d '=' -f2- | tr -d '"' | tr -d "'")
  elif [ -n "$A2A_PROJECT" ]; then
    export LOCUTUS_PROJECT="$A2A_PROJECT"
  else
    export LOCUTUS_PROJECT="$(basename "$PWD")"
  fi
fi

# 5. Docker Auto-Start Check:
export LOCUTUS_CONTAINER="${LOCUTUS_CONTAINER:-a2a-redis}"
if [[ "$LOCUTUS_REDIS_URL" == *"127.0.0.1"* || "$LOCUTUS_REDIS_URL" == *"localhost"* ]]; then
  if command -v docker >/dev/null 2>&1; then
    if [ -z "$(docker ps -q -f name=^/${LOCUTUS_CONTAINER}$)" ]; then
      if [ "$(docker ps -aq -f name=^/${LOCUTUS_CONTAINER}$)" ]; then
        echo "Starting existing ${LOCUTUS_CONTAINER} container..."
        docker start "$LOCUTUS_CONTAINER"
      else
        echo "Launching new ${LOCUTUS_CONTAINER} container..."
        docker run -d --name "$LOCUTUS_CONTAINER" -p 6379:6379 redis:alpine
      fi
      sleep 1
    fi
  fi
fi

# 6. Host CLI check: If redis-cli is not installed on the host, route via Docker
if ! command -v redis-cli >/dev/null 2>&1; then
  if command -v docker >/dev/null 2>&1 && [ "$(docker ps -q -f name=^/${LOCUTUS_CONTAINER}$)" ]; then
    redis-cli() {
      docker exec -i "$LOCUTUS_CONTAINER" redis-cli "$@"
    }
  fi
fi
```

---

## 3. Lua Script Inventory (`scripts/`)

All server-side coordination is executed atomically via Lua scripts located in `$LOCUTUS_SCRIPTS_DIR`.
Execute them cleanly using:
```bash
redis-cli -u "${LOCUTUS_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$LOCUTUS_SCRIPTS_DIR/<script>.lua")" 0 [args...]
```

| Script File | Purpose | Parameters (ARGV) |
| :--- | :--- | :--- |
| [`register.lua`](scripts/register.lua) | Register identity, index tags, and arm heartbeat | `ARGV[1]: prefix`, `ARGV[2]: name`, `ARGV[3]: tags_csv`, `ARGV[4]: ttl_sec` |
| [`send_o2o.lua`](scripts/send_o2o.lua) | Direct message to recipient inbox with queue TTL | `ARGV[1]: prefix`, `ARGV[2]: recipient`, `ARGV[3]: msg_json`, `ARGV[4]: inbox_ttl` |
| [`multicast.lua`](scripts/multicast.lua) | Fan-out to tag(s) via AND filter (`SINTER`) or `*` | `ARGV[1]: prefix`, `ARGV[2]: target_tags_csv`, `ARGV[3]: msg_json`, `ARGV[4]: inbox_ttl` |
| [`tag.lua`](scripts/tag.lua) | Dynamically add/remove/set tags without reregistering | `ARGV[1]: prefix`, `ARGV[2]: name`, `ARGV[3]: action (add/remove/set)`, `ARGV[4]: tags_csv` |
| [`drain.lua`](scripts/drain.lua) | Atomically pop up to N pending messages from inbox | `ARGV[1]: prefix`, `ARGV[2]: name`, `ARGV[3]: max_count` |
| [`directory.lua`](scripts/directory.lua) | List active agents (optional team/project filter) | `ARGV[1]: prefix`, `ARGV[2]: optional filter_tag (e.g. project name)` |
| [`unregister.lua`](scripts/unregister.lua) | Clean logout, remove from roster and indexed tags | `ARGV[1]: prefix`, `ARGV[2]: name` |
| [`security.sh`](scripts/security.sh) | HMAC-SHA256 signing, verification, and AES encryption | `get-secret`, `sign`, `verify`, `encrypt`, `decrypt` |
| [`send.sh`](scripts/send.sh) | Authenticated dispatcher: signs and routes to Lua scripts | `--to / --broadcast`, `--type`, `--subject`, `--body` |
| [`listen.sh`](scripts/listen.sh) | Air-gapped prompt-firewall background listener | `[agent_name] [timeout_sec]` |

---

## 4. Wire Protocol (JSON Envelope)

All messages pushed to `${LOCUTUS_REDIS_PREFIX}inbox:*` must adhere strictly to the `LocutusMessage` schema (see [`references/wire_spec.md`](references/wire_spec.md) for full specification):

```json
{
  "id": "msg_1710789000_alice_9f8a",
  "from": "alice",
  "to": "bob",
  "type": "task",
  "reply_to": null,
  "tags": ["locutus", "ticket-104"],
  "subject": "Review auth parser changes",
  "body": "Please inspect src/auth.ts and verify if token expiry handles leap years.",
  "timestamp": "2026-09-18T23:35:00Z",
  "sig": "9f8a3c4b12...64hex",
  "encrypted": false
}
```

---

## 5. Step-by-Step Lifecycle Guide for Assistants

### Step 1: Initialize Identity & Tags
Choose a unique ephemeral name. All agents MUST include their project tag (`$LOCUTUS_PROJECT`) plus any role or ticket tags:
```bash
MY_NAME="worker-$RANDOM"
MY_TAGS="${LOCUTUS_PROJECT},qa,ticket-104"
```

### Step 2: Register, Announce, and Catch Up on Backlog
1. Execute registration and drain pre-existing messages:
```bash
# Register
redis-cli -u "${LOCUTUS_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$LOCUTUS_SCRIPTS_DIR/register.lua")" 0 "$LOCUTUS_REDIS_PREFIX" "$MY_NAME" "$MY_TAGS" 150

# Catch-up / Drain any existing backlog
redis-cli -u "${LOCUTUS_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$LOCUTUS_SCRIPTS_DIR/drain.lua")" 0 "$LOCUTUS_REDIS_PREFIX" "$MY_NAME" 50
```

2. **Operator Announcement Invariant**:
   Whenever registering, you MUST print a clear, human-readable registration banner to the operator:
   ```text
   ====================================================
   [LOCUTUS BUS] Registered Successfully
   - Agent Name : <MY_NAME>
   - Project    : <LOCUTUS_PROJECT>
   - Tags       : <MY_TAGS>
   - Redis URL  : <LOCUTUS_REDIS_URL> (prefix: <LOCUTUS_REDIS_PREFIX>)
   - Status     : Active & Listening on inbox
   ====================================================
   ```

*If any messages are returned from the drain command, process them immediately!*

### Step 3: Arm the Secure Background Listener (Air-Gapped Prompt Firewall)
Launch the background listener wrapper (`scripts/listen.sh`). It blocks on `BRPOP`, validates the HMAC-SHA256 signature, decrypts any encrypted payload, and silently drops forged/tampered messages before they can reach your LLM context window:

```bash
"$LOCUTUS_SCRIPTS_DIR/listen.sh" "${MY_NAME}" 90
```

- **In assistants with background completion notifications (e.g., Claude Code, Antigravity)**:
  - Do NOT poll in a loop. Stop calling tools and wait for the notification.
  - When the background command finishes, inspect the output.
- **Handling Listener Output**:
  - **Case A: Output is `(nil)` (90s Timeout)**:
    - Refresh heartbeat:
      ```bash
      redis-cli -u "${LOCUTUS_REDIS_URL:-redis://127.0.0.1:6379}" SET "${LOCUTUS_REDIS_PREFIX}heartbeat:${MY_NAME}" 1 EX 150
      ```
    - Immediately re-arm:
      ```bash
      "$LOCUTUS_SCRIPTS_DIR/listen.sh" "${MY_NAME}" 90
      ```
  - **Case B: Output contains an authenticated message**:
    - Parse the JSON payload.
    - Process the instructions (run tests, edit files, research).
    - If `type` is `task` or `query`, send a reply back to `from`.
    - **Re-arm the listener immediately in the same turn**:
      ```bash
      "$LOCUTUS_SCRIPTS_DIR/listen.sh" "${MY_NAME}" 90
      ```

---

### Sending Direct (O2O)

#### Option 1 (Recommended): Authenticated Dispatcher (`send.sh`)
`send.sh` computes the HMAC-SHA256 signature using `~/.config/locutus/secret` (and encrypts the body if `LOCUTUS_ENCRYPT=1`):
```bash
"$LOCUTUS_SCRIPTS_DIR/send.sh" --to "bob" --type "task" --from "$MY_NAME" --subject "Review PR 12" --body "Please inspect the latest commit on branch fix-redis."
```

#### Option 2: Structured Parameters via Lua (Zero Shell Quoting Issues)
`send_o2o.lua` accepts individual arguments and automatically generates the valid JSON envelope using Redis `cjson`:
```bash
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
redis-cli -u "${LOCUTUS_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$LOCUTUS_SCRIPTS_DIR/send_o2o.lua")" 0 \
  "$LOCUTUS_REDIS_PREFIX" "bob" "task" "$MY_NAME" "Review PR 12" "Please inspect the latest commit on branch fix-redis." "$LOCUTUS_PROJECT" "" "" "$TS"
```
*Parameter Order:* `prefix recipient type from subject body [tags_csv] [reply_to] [msg_id] [timestamp] [inbox_ttl]`

#### Option 2: Pre-Constructed JSON String
If you already have a formatted JSON string, pass it directly:
```bash
redis-cli -u "${LOCUTUS_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$LOCUTUS_SCRIPTS_DIR/send_o2o.lua")" 0 "$LOCUTUS_REDIS_PREFIX" "bob" "$MSG_JSON" 604800
```

### Sending Multicast (Scoped to Project with AND-Filtering)
By default, multicasts are scoped to your project (`$LOCUTUS_PROJECT`). Tags are **AND-filters**:

#### Option 1: Structured Parameters
```bash
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Target workers in this project with tag 'qa' (AND filter: project AND qa)
TARGET_TAGS="${LOCUTUS_PROJECT},qa"
redis-cli -u "${LOCUTUS_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$LOCUTUS_SCRIPTS_DIR/multicast.lua")" 0 \
  "$LOCUTUS_REDIS_PREFIX" "$TARGET_TAGS" "task" "$MY_NAME" "Run Regression" "Please run test suite." "$TARGET_TAGS" "" "" "$TS"
```

#### Option 2: Pre-Constructed JSON String
```bash
redis-cli -u "${LOCUTUS_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$LOCUTUS_SCRIPTS_DIR/multicast.lua")" 0 "$LOCUTUS_REDIS_PREFIX" "$TARGET_TAGS" "$MSG_JSON" 604800
```

### Dynamic Tag Management (No Reregistration Required)
```bash
# Add a tag dynamically (e.g. claimed ticket-42)
redis-cli -u "${LOCUTUS_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$LOCUTUS_SCRIPTS_DIR/tag.lua")" 0 "$LOCUTUS_REDIS_PREFIX" "$MY_NAME" "add" "ticket-42"

# Remove a tag dynamically (e.g. ticket done)
redis-cli -u "${LOCUTUS_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$LOCUTUS_SCRIPTS_DIR/tag.lua")" 0 "$LOCUTUS_REDIS_PREFIX" "$MY_NAME" "remove" "ticket-42"
```

### Replying to a Message
Always reply unicast to the original sender (`from`), setting `reply_to` to the incoming message's `id`.

#### Option 1 (Recommended): Structured Parameters
```bash
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
redis-cli -u "${LOCUTUS_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$LOCUTUS_SCRIPTS_DIR/send_o2o.lua")" 0 \
  "$LOCUTUS_REDIS_PREFIX" "$ORIGINAL_FROM" "reply" "$MY_NAME" "Re: ${ORIGINAL_SUBJECT}" "The tests passed successfully. No regressions found." "$LOCUTUS_PROJECT" "$ORIGINAL_MSG_ID" "" "$TS"
```

#### Option 2: Pre-Constructed JSON String
```bash
redis-cli -u "${LOCUTUS_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$LOCUTUS_SCRIPTS_DIR/send_o2o.lua")" 0 "$LOCUTUS_REDIS_PREFIX" "$ORIGINAL_FROM" "$REPLY_JSON" 604800
```

---

## 7. Discovering Peers (`who`)

```bash
# Discover team members in your current project:
redis-cli -u "${LOCUTUS_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$LOCUTUS_SCRIPTS_DIR/directory.lua")" 0 "$LOCUTUS_REDIS_PREFIX" "$LOCUTUS_PROJECT"

# Discover all agents across all projects on this Redis instance:
redis-cli -u "${LOCUTUS_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$LOCUTUS_SCRIPTS_DIR/directory.lua")" 0 "$LOCUTUS_REDIS_PREFIX" "*"
```

---

## 8. Graceful Exit / Retirement (`unregister`)

When the operator ends the session, retires your agent, or issues `/locutus unregister` (or `/locutus close`):

```bash
redis-cli -u "${LOCUTUS_REDIS_URL:-redis://127.0.0.1:6379}" EVAL "$(cat "$LOCUTUS_SCRIPTS_DIR/unregister.lua")" 0 "$LOCUTUS_REDIS_PREFIX" "$MY_NAME"
```
This atomically removes your agent from the active roster, deletes your heartbeat, and unindexes all your tags while preserving your inbox queue in case of future reconnection.

---

## 9. Fallback: Pure Python (When `redis-cli` is Missing)

If `redis-cli` is not installed, run commands using this standard library Python one-liner (zero pip packages required):

```bash
python3 -c '
import urllib.parse, socket, sys, os

url = urllib.parse.urlparse(os.environ.get("LOCUTUS_REDIS_URL", os.environ.get("REDIS_URL", "redis://127.0.0.1:6379")))
s = socket.create_connection((url.hostname or "127.0.0.1", url.port or 6379))
if url.password:
    s.sendall(f"*2\r\n$4\r\nAUTH\r\n${len(url.password)}\r\n{url.password}\r\n".encode())
    s.recv(1024)

# Example: BRPOP
key = sys.argv[1]
cmd = f"*3\r\n$5\r\nBRPOP\r\n${len(key)}\r\n{key}\r\n$2\r\n90\r\n".encode()
s.sendall(cmd)
resp = s.recv(4096).decode("utf-8", errors="ignore")
print(resp)
' "locutus:inbox:my_name"
```
