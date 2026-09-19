# Locutus: Zero-Glue Redis Inter-Assistant Communication Bus

Locutus provides a **100% prompt-based** inter-assistant communication protocol and execution engine over Redis. It requires **no external JavaScript, Python daemons, background processes, or glue code**.

It enables multiple coding assistants (Claude Code, Antigravity, Cursor, Windsurf, Aider, etc.) across different terminals, projects, or machines to register, discover peers, coordinate work, and exchange tasks in real time over standard Redis primitives.

---

## Key Features

- **100% Prompt-Driven with Embedded Lua**: Assistants execute standard `redis-cli` commands that invoke pure, atomic Lua scripts in `scripts/`. No client-side glue code or state machines.
- **Configurable Namespace Isolation**: All Redis keys use `$LOCUTUS_REDIS_PREFIX` (defaults to `locutus:`), preventing any conflict with existing Redis usage.
- **Project Isolation & Native AND-Filter Multicast**:
  - Every agent belongs to a project tag (`$LOCUTUS_PROJECT`, automatically derived from working directory basename).
  - Multicasting across multiple tags uses native Redis `SINTER` (set intersection in C) as an **AND-filter** (e.g. `locutus,qa` delivers only to agents matching BOTH tags).
  - Cluster-wide broadcast is explicitly reserved for `*` or `@all`.
- **Zero-Token Idle Watcher**:
  - Assistants block on `BRPOP ${LOCUTUS_REDIS_PREFIX}inbox:<my_name> 90`.
  - Idle sessions consume **0 CPU and 0 LLM tokens** while waiting for work.
  - The 90s listener timeout cadence doubles as the liveness heartbeat refresher (`SET ... EX 150`).
- **Dynamic Tag Management**: Add, remove, or set agent tags on the fly without unregistering or dropping queued inbox messages (`scripts/tag.lua`).
- **Offline Backlog Delivery**: Tasks sent to offline or disconnected agents queue safely in Redis (7-day default TTL) and are delivered in FIFO order via `scripts/drain.lua` upon reconnect.
- **Automatic Dead-Agent Pruning**: Expired agent heartbeats are automatically pruned during multicast fan-out, eliminating dead-queue bloat.
- **Dual-Mode Structured Sending**: Messages can be sent either as raw JSON or via structured arguments where Redis automatically generates the envelope using native `cjson.encode`.
- **Cryptographic Security & Prompt-Injection Firewall**:
  - Out-of-band secret key management (`~/.config/locutus/secret`, `0600` permissions) with zero exposure to LLM context, Git, or Redis.
  - Mandatory HMAC-SHA256 authentication: unauthenticated or forged messages are dropped at the host shell level by `scripts/listen.sh` before entering the assistant's context window.
  - Optional End-to-End Encryption (E2EE): `LOCUTUS_ENCRYPT=1` transparently encrypts task bodies via OpenSSL AES-256-CBC PBKDF2.
- **Operator Slash Command**: Built-in human-facing slash command `/locutus` (`open`, `send`, `broadcast`, `who`, `tag`, `close`).
- **Rigorous Test Suite**: Deterministic unit tests, HMAC security & prompt injection firewall tests, single-agent Ollama tool-calling tests, and multi-agent autonomous ping-pong tests with Pydantic schema validation.


---

## Architecture & Wire Format

Every agent listens to exactly **one** primitive list: `${LOCUTUS_REDIS_PREFIX}inbox:<name>`.

```
                    ┌─────────────────────────┐
                    │      Sender Agent       │
                    └───────────┬─────────────┘
                                │
                 ┌──────────────┴──────────────┐
                 │                             │
          Direct (O2O)                  Multicast (O2M)
          send_o2o.lua                   multicast.lua
                 │                             │
                 │                    SINTER Tag Intersection
                 │                    (AND-Filter by Project)
                 │                             │
                 ▼                             ▼
        ┌─────────────────┐           ┌─────────────────┐
        │  inbox:<bob>    │           │  inbox:<alice>  │
        └────────┬────────┘           └────────┬────────┘
                 │                             │
                 ▼                             ▼
           BRPOP Worker                  BRPOP Worker
```

### Standard Message Envelope

All messages adhere to the formal [`LocutusMessage`](tests/schema.py) schema:

```json
{
  "id": "msg_1789777056_alice_83728",
  "from": "alice",
  "to": "bob",
  "type": "task",
  "reply_to": null,
  "tags": ["locutus", "calc"],
  "subject": "Compute Product",
  "body": "Please compute 15 * 15",
  "timestamp": "2026-09-19T00:17:36Z"
}
```

Envelope types:
- `task`: Action request expecting a reply.
- `query`: Read-only data request.
- `reply`: Unicast response threaded to `reply_to: <task_id>`.
- `status`: Informational status broadcast.

*See [`references/wire_spec.md`](references/wire_spec.md) for full protocol specifications.*

---

## Lua Scripts (`scripts/`)

All server-side coordination is executed atomically via Lua scripts located in `scripts/`:

| Script | Description | Primary Arguments |
| :--- | :--- | :--- |
| [`register.lua`](scripts/register.lua) | Atomically registers agent identity, indexes tags, sets heartbeat TTL. | `prefix, name, tags_csv, ttl_sec` |
| [`send_o2o.lua`](scripts/send_o2o.lua) | Direct point-to-point inbox queuing with TTL. Supports raw JSON or structured parameters. | `prefix, recipient, type/json, from, subject, body, [tags], [reply_to], [id], [ts]` |
| [`multicast.lua`](scripts/multicast.lua) | Fans out message using Redis `SINTER` AND-filter. Prunes expired dead agents. | `prefix, target_tags_csv, type/json, ...` |
| [`tag.lua`](scripts/tag.lua) | Dynamically adds, removes, or sets tags without dropping inbox messages. | `prefix, name, action ("add"\|"remove"\|"set"), tags_csv` |
| [`drain.lua`](scripts/drain.lua) | Atomic batch RPOP to drain offline backlog on startup/reconnect. | `prefix, name, max_count` |
| [`directory.lua`](scripts/directory.lua) | Lists active agents, liveness status, and tags with optional project filter. | `prefix, [filter_tag]` |
| [`unregister.lua`](scripts/unregister.lua) | Clean logout, tag set cleanup, and heartbeat removal. | `prefix, name` |
| [`security.sh`](scripts/security.sh) | Cryptographic HMAC-SHA256 signing, verification, and AES-256 PBKDF2 encryption. | `get-secret`, `sign`, `verify`, `encrypt`, `decrypt` |
| [`send.sh`](scripts/send.sh) | Secure authenticated dispatcher: signs payload and routes to Redis Lua scripts. | `--to / --broadcast`, `--type`, `--subject`, `--body` |
| [`listen.sh`](scripts/listen.sh) | Air-gapped prompt-injection firewall background listener. Intercepts BRPOP and drops forged messages. | `[agent_name] [timeout_sec]` |

---

## Operator Slash Command (`/locutus`)

The human operator can inspect and control the bus using `/locutus`:

```bash
# Open connection and register
/locutus open name=worker-1 tags=qa,frontend

# View live agents in current project
/locutus who

# View all agents cluster-wide
/locutus who all=true

# Send a direct task
/locutus send to=coder-1 subject="Fix test" body="pytest tests/test_auth.py failed"

# Broadcast to project team with AND-filtering
/locutus broadcast tags=qa,backend subject="Deploy Sync" body="Staging updated"

# Dynamically add a tag
/locutus tag add ticket-42

# Gracefully unregister and disconnect
/locutus close
```

*See [`commands/locutus.md`](commands/locutus.md) for full command documentation.*

---

## Installation & Setup

### 1. Global Skill Installation (Antigravity & Claude Code)
```bash
mkdir -p ~/.gemini/config/skills/locutus
cp -r SKILL.md scripts references commands ~/.gemini/config/skills/locutus/
```

### 2. Environment Variables
Locutus automatically resolves connection parameters in order of precedence:
```bash
export LOCUTUS_REDIS_URL="redis://127.0.0.1:6379"
export LOCUTUS_REDIS_PREFIX="locutus:"
export LOCUTUS_PROJECT="$(basename "$PWD")"
export LOCUTUS_SCRIPTS_DIR="$(pwd)/scripts"
```
*(Backwards-compatible fallbacks `A2A_REDIS_URL`, `A2A_REDIS_PREFIX`, and `REDIS_URL` are fully supported).*

---

## Running the Test Suite

A Python virtual environment with `pydantic` is used for validation:

```bash
# 1. Run all 15 protocol unit tests (deterministic, zero-token, live Redis):
.venv/bin/python3 -m unittest tests/test_protocol.py

# 2. Run HMAC security & prompt-injection firewall tests (deterministic, live Redis):
.venv/bin/python3 -m unittest tests/test_security.py

# 3. Run single-agent autonomous Ollama test (validates LLM tool-calling + Pydantic schema):
.venv/bin/python3 tests/test_ollama_agent.py

# 4. Run multi-agent autonomous ping-pong integration test (Alice & Bob live interaction):
.venv/bin/python3 tests/test_multi_agent_pingpong.py
```

---

## License
MIT License
