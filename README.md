<div align="center">

# Locutus

**Daemonless, Cryptographically-Authenticated Inter-Process Communication (IPC) Protocol and Message Router for Heterogeneous Autonomous Coding Agents over Key-Value Datastores with Redis Cluster Hash-Slot Affinity (ISO/IEC 19514 / ISO/IEC 2382)**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/axiomantic/locutus/actions/workflows/ci.yml/badge.svg)](https://github.com/axiomantic/locutus/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/Tests-25%20Passing-success.svg)](tests/)
[![Redis](https://img.shields.io/badge/Redis-6.2%2B-red.svg)](https://redis.io)
[![Nim](https://img.shields.io/badge/Nim-2.0%2B-yellow.svg)](https://nim-lang.org)
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Linux%20%7C%20Windows-blue.svg)](README.md)

*Sub-millisecond inter-agent coordination across terminals, projects, and machines with zero background daemons and a cryptographic prompt-injection firewall.*

</div>

---

## What is Locutus?

**Locutus** connects multiple coding assistants (Claude Code, Antigravity, Cursor, Windsurf, Aider, Ollama) across terminals, workspaces, or machines using Redis data structures and a standalone CLI tool.

### Daemonless Architecture: Why No Broker Daemon?

Traditional multi-agent frameworks require running dedicated background server processes (such as custom HTTP/WebSocket servers, broker services, or polling sidecars). These add operational overhead:
- Server daemons require supervisor processes (systemd, Docker) to monitor and restart on failure.
- Daemons require configuring open network ports, host bindings, and connection handshakes.
- Daemons consume background CPU and RAM continuously, even when no agents are active.

**Locutus requires no intermediate daemon:**
There is no Locutus daemon process running on the host. Locutus is a standalone compiled CLI tool that uses Redis directly as the message broker and state store. Agents communicate by executing standard CLI commands (`locutus send`, `locutus listen`) against any local or remote Redis instance.

### System Architecture: Redis Data Structure Mapping

Locutus implements message exchange and coordination by mapping communication functions directly onto native Redis data structures:

1. **Message Queues (Redis Lists / `LPUSH` & `BRPOP`)**:
   - Each agent's inbox is stored as a Redis list (`{prefix}:inbox:<agent>`).
   - Senders push authenticated JSON messages to the head of the list with `LPUSH`.
   - Receivers wait for incoming work using blocking pop (`BRPOP`). Redis handles the blocking wait internally, allowing agents to sleep with zero CPU polling and zero token consumption until a message arrives.

2. **Agent Directory (Redis Sets / `SADD`, `SREM`, `SMEMBERS`)**:
   - Active agents and functional tag groups (e.g., `backend`, `frontend`, `qa`) are stored in Redis sets (`{prefix}:active_agents` and `{prefix}:tag:<tag>`).
   - Discovery commands (`locutus who`) read directly from these sets in $\mathcal{O}(1)$ time without keyspace scanning.

3. **Multicast Routing (Redis `SINTER`)**:
   - Multicast broadcasts (`locutus broadcast --tags "backend,qa"`) compute target recipients on the Redis server using set intersection (`SINTER`).
   - Fanout occurs entirely within Redis, avoiding client-side candidate roster transfers.

4. **Liveness & Expiration (Redis TTLs / `EXPIRE`)**:
   - **Heartbeats**: Active listeners maintain presence via an ephemeral key (`{prefix}:heartbeat:<agent>`) with a 150-second TTL. If an agent process exits or crashes, its heartbeat key expires automatically, and subsequent routing passes prune the inactive agent from the directory.
   - **Inboxes**: Each message delivery updates a 7-day sliding expiration on `{prefix}:inbox:<agent>`, preventing abandoned queues from consuming memory indefinitely.

5. **Atomic Transactions (Embedded Lua / `EVALSHA`)**:
   - State transitions requiring multiple operations (such as roster updates, dead-agent pruning, and multi-queue fanout) run inside Redis as atomic Lua scripts using cached script hashes (`EVALSHA`).

6. **Cluster Compatibility (Hash Tags / `{...}`)**:
   - In Redis Cluster environments, key names include curly bracket hash tags (e.g., `{locutus:project}:inbox:<agent>`).
   - Redis uses only the bracketed text to determine shard placement, ensuring all project keys reside on the same hash slot and preventing `CROSSSLOT` errors during multi-key commands (`SINTER`, `SMEMBERS`).

### Why Locutus?

| Traditional Agent Frameworks | Locutus Architecture |
| :--- | :--- |
| ❌ Heavy Python/Node background server daemons | ⚡ **Daemonless**: Zero background processes; direct Redis client calls |
| ❌ Fragile WebSocket / HTTP bridges requiring open ports | ⚡ **Standard Redis**: Works over local or hosted Redis (AWS, Upstash, Redis Cluster) |
| ❌ 500ms+ startup latency & high RAM overhead | ⚡ **Sub-millisecond latency**: 289 KB standalone native binary (1ms cold start) |
| ❌ Vulnerable to prompt injection from untrusted messages | ⚡ **Air-Gap Prompt Firewall**: Drops unauthenticated payloads at process boundary |
| ❌ Idle listeners consume continuous LLM tokens | ⚡ **Zero-token idle**: Blocking `BRPOP` consumes 0 LLM tokens while waiting |

---

## Architecture & Data Flow

Every agent receives tasks through a single atomic inbox: `${PREFIX}inbox:<agent_name>`.

```text
                        ┌──────────────────────────────┐
                        │      Sending Assistant       │
                        └──────────────┬───────────────┘
                                       │
                        ┌──────────────┴──────────────┐
                        ▼                             ▼
                 Direct Task (O2O)             Multicast (O2M)
                 locutus send                  locutus broadcast
                        │                             │
                        │                     SINTER Tag Intersection
                        │                     (Project-Scoped AND Filter)
                        │                             │
                        ▼                             ▼
               ┌─────────────────┐           ┌─────────────────┐
               │  inbox:<worker> │           │   inbox:<qa>    │
               └────────┬────────┘           └────────┬────────┘
                        │                             │
                        ▼                             ▼
                 [AIR-GAP FIREWALL]            [AIR-GAP FIREWALL]
                 HMAC Signature Check          HMAC Signature Check
                        │                             │
                        ▼                             ▼
                  Valid Payload                 Valid Payload
                 Delivered to LLM              Delivered to LLM
```

---

## Installation Options

Locutus is distributed as a single, static compiled binary with zero runtime dependencies. Choose your preferred installation method:

### Method A: Homebrew (macOS & Linux)
```bash
brew install axiomantic/tap/locutus
```

### Method B: Universal One-Line Installer (macOS & Linux)
```bash
curl -fsSL https://raw.githubusercontent.com/axiomantic/locutus/main/scripts/install.sh | bash
```
*Installs the verified native binary into `/usr/local/bin` or `~/.local/bin`.*

### Method C: Windows PowerShell Installer
Run in PowerShell (Admin not required):
```powershell
irm https://raw.githubusercontent.com/axiomantic/locutus/main/scripts/install.ps1 | iex
```
*Installs `locutus.exe` into `%LOCALAPPDATA%\Programs\locutus` and configures your User `PATH`.*

### Method D: Scoop (Windows)
```powershell
scoop install https://raw.githubusercontent.com/axiomantic/locutus/main/packaging/scoop/locutus.json
```

### Method E: Debian / Ubuntu (.deb Package)
Download the `.deb` package for your architecture from [Releases](https://github.com/axiomantic/locutus/releases):
```bash
# For x86_64 (amd64)
sudo dpkg -i locutus_*_amd64.deb

# For ARM64 (aarch64)
sudo dpkg -i locutus_*_arm64.deb
```

### Method F: Pre-Compiled Standalone Binaries
Standalone zero-dependency archives are available on the [GitHub Releases](https://github.com/axiomantic/locutus/releases) page:

| OS | Architecture | Package |
| :--- | :--- | :--- |
| **macOS** | Apple Silicon (M1/M2/M3/M4) | `locutus-darwin-arm64.tar.gz` |
| **macOS** | Intel x86_64 | `locutus-darwin-amd64.tar.gz` |
| **Linux** | x86_64 (amd64) | `locutus-linux-amd64.tar.gz` |
| **Linux** | ARM64 (aarch64) | `locutus-linux-arm64.tar.gz` |
| **Windows** | x86_64 (amd64) | `locutus-windows-amd64.zip` |

---

## 30-Second Quickstart

### 1. Install Prerequisites (Nim & Redis)

**macOS:**
```bash
# Install Nim compiler & Redis server via Homebrew
brew install nim redis

# Start Redis service
brew services start redis
```

**Linux (Ubuntu / Debian):**
```bash
# Install Nim compiler & Redis server
sudo apt update && sudo apt install -y nim redis-server

# Start Redis service
sudo systemctl start redis-server
```
*(Or install the official Nim toolchain universally via `curl https://nim-lang.org/choosenim/init.sh -sSf | sh`)*

**Windows:**
```powershell
# Install Nim and Redis via Scoop or Chocolatey
scoop install nim redis
# Or using Chocolatey:
# choco install nim redis-64 -y
```

### 2. Clone & Build Locutus
```bash
# Clone
git clone https://github.com/axiomantic/locutus.git
cd locutus

# Compile standalone native binary (under 1 second)
# On macOS / Linux:
nim c -d:release -o:bin/locutus src/locutus.nim
mkdir -p ~/.local/bin && cp bin/locutus ~/.local/bin/locutus

# On Windows (PowerShell):
# nim c -d:release -o:bin/locutus.exe src/locutus.nim
```

### 3. Open Connection in Terminal A (Worker)
```bash
locutus open worker-1 "backend,qa"
```
```text
====================================================
[LOCUTUS BUS] Registered Successfully
- Agent Name : worker-1
- Project    : my-project
- Tags       : my-project,backend,qa
- Redis URL  : redis://127.0.0.1:6379 (prefix: locutus:)
- Security   : HMAC-SHA256 authenticated (Air-Gap Prompt Firewall)
- Engine     : Nim Native (EVALSHA cached)
- Status     : Active & Listening on inbox
====================================================
```

### 4. Arm Background Listener in Terminal A
```bash
locutus listen 90
```
*Blocks with zero token consumption and automatically refreshes heartbeat.*

### 5. Send Task from Terminal B (Requester)
```bash
locutus send --to worker-1 --subject "Run Tests" --body "pytest tests/auth"
```
*Terminal A receives and prints the authenticated message instantly.*

---

## CLI Reference

| Command | Description | Example |
| :--- | :--- | :--- |
| `locutus open [name] [tags]` | Registers identity, sets project tags, drains offline backlog. | `locutus open coder "qa,python"` |
| `locutus listen [name] [timeout]` | Blocks on inbox, refreshes heartbeat, drops tampered messages. | `locutus listen 90` |
| `locutus send --to <target> ...` | Sends direct (O2O) message with HMAC signature. | `locutus send --to worker-1 --subject "Fix Bug" --body "src/api.py"` |
| `locutus broadcast [--tags <tags>] ...` | Multicasts to all agents matching tags within project. | `locutus broadcast --tags "qa" --subject "New Release" --body "Verify"` |
| `locutus who [filter]` | Formatted table of cluster agents and active heartbeats. | `locutus who` or `locutus who "*"` |
| `locutus tag <add\|remove\|set> <tags>` | Dynamically adjusts tags without dropping queued messages. | `locutus tag add "lead"` |
| `locutus drain [count]` | Atomically drains up to N offline messages (FIFO). | `locutus drain 10` |
| `locutus close` | Graceful deregistration, clears tags and heartbeat. | `locutus close` |
| `locutus get-secret` | Prints or initializes 256-bit cluster secret. | `locutus get-secret` |
| `locutus config <show\|get\|path\|init>` | Introspects resolved settings, provenance, and paths. | `locutus config show` or `locutus config get redis_url` |

---

## Configuration Architecture & Profiles

Locutus provides deterministic, multi-tiered cascading configuration resolution:

```
┌─────────────────────────────────────────────────────────┐
│ 1. Explicit CLI Flags (--redis-url, --project, etc.)    │
├─────────────────────────────────────────────────────────┤
│ 2. Process Environment Variables (LOCUTUS_*, REDIS_URL) │
├─────────────────────────────────────────────────────────┤
│ 3. Workspace / Project Config (.locutus.toml, .env)     │
├─────────────────────────────────────────────────────────┤
│ 4. Per-User Config (~/.config/locutus/config.toml)      │
├─────────────────────────────────────────────────────────┤
│ 5. Global / System Config (/etc/locutus/config.toml)    │
├─────────────────────────────────────────────────────────┤
│ 6. Built-in Defaults                                    │
└─────────────────────────────────────────────────────────┘
```

### Configuration Files

- **Workspace**: `.locutus.toml` or `locutus.toml` in the project root (walks upwards to `.git`).
- **User**: `~/.config/locutus/config.toml` (Linux/macOS) or `%APPDATA%\locutus\config.toml` (Windows).
- **System**: `/etc/locutus/config.toml` (Linux), `/Library/Application Support/locutus/config.toml` (macOS), or `%ProgramData%\locutus\config.toml` (Windows).

### Example `.locutus.toml`

```toml
redis_url = "redis://127.0.0.1:6379"
prefix = "locutus:"
project = "my-project"
encrypt = false
cluster = false
heartbeat_ttl = 150
message_ttl = 604800
listen_timeout = 90

# Shared secret file (avoids committing secrets into git)
secret_file = "~/.config/locutus/secret"

# Named profiles: locutus --profile staging <subcommand>
[profiles.staging]
redis_url = "rediss://staging.internal:6380"
prefix = "stg:locutus:"
encrypt = true

[profiles.prod]
redis_url = "rediss://prod-cluster.internal:6379"
cluster = true
encrypt = true
```

### Configuration CLI Commands

- `locutus config show`: Displays the resolved configuration alongside the **source provenance** of each value (CLI flag, env var, workspace config, user config, or default).
- `locutus config show --json`: Machine-readable JSON output of settings and provenance.
- `locutus config get <key>`: Script-friendly access to individual values (`locutus config get redis_url`).
- `locutus config path`: Lists candidate configuration files on the system and their existence status.
- `locutus config init [--user | --project]`: Scaffolds a starter `.locutus.toml` file.

---


## Cryptographic Security Model

Locutus implements an **Air-Gap Prompt-Injection Firewall** to safeguard coding assistants from malicious prompt injection, cluster forgery, or rogue tasks:

```text
Incoming Redis Data ──► [Host OS: locutus listen]
                                 │
                   Verify HMAC-SHA256 Signature
                  (Key: ~/.config/locutus/secret)
                                 │
                   ┌─────────────┴─────────────┐
                   ▼                           ▼
            [VALID SIGNATURE]          [FORGED / TAMPERED]
                   │                           │
         Deliver JSON to stdout         Drop to stderr only
                   │                           │
                   ▼                           ▼
          Assistant LLM Context         Context Protected!
          Processes Safe Task           (Attacker Thwarted)
```

1. **Host-Level Verification**: Messages are cryptographically validated by `locutus listen` at the process boundary *before* reaching standard output.
2. **Untrusted Data Dropped**: Unauthenticated, forged, or tampered payloads are completely dropped before they can enter an LLM's context window.
3. **Zero Secret Leakage**: The cluster secret (`~/.config/locutus/secret`, `0600`) never enters LLM prompts, Git commits, or Redis keys.
4. **Optional End-to-End Encryption (E2EE)**: While HMAC-SHA256 authentication is mandatory by default to prevent forgery and prompt injection, full payload encryption is optional. Setting `LOCUTUS_ENCRYPT=1` transparently encrypts task bodies with AES-256-CBC PBKDF2 (10,000 iterations), ensuring raw plaintext never touches Redis memory or persistence files.

---

## TTL Lifecycle & Keyspace Hygiene

Locutus enforces automatic keyspace hygiene to prevent unbounded memory growth on long-running Redis instances:

- **Inbox Lists (`inbox:<agent>`)**: Automatically armed with an expiring TTL (default: **7 days** / 604,800 seconds). Each new message pushed to an inbox atomically resets the 7-day TTL window. Offline agents that remain disconnected for more than 7 days have their stale inboxes automatically pruned by Redis.
- **Heartbeats (`heartbeat:<agent>`)**: Armed with a strict **150-second TTL** (2.5 minutes). Active listeners (`locutus listen`) continually refresh this heartbeat.
- **Dead-Agent Sweeper**: When routing multicast or directory queries, Locutus checks agent heartbeat existence. Agents whose heartbeats have expired are atomically pruned from active rosters and tag indices.

---

## Redis Cluster Compatibility (Hash Tags)

In a **Redis Cluster**, keys are automatically distributed across 16,384 hash slots across multiple shards. Multi-key operations (`SINTER`, `SMEMBERS`, `LPUSH`) and atomic Lua transactions will fail with `CROSSSLOT Keys in request don't hash to the same slot` if keys belong to different slots.

Locutus supports native **Redis Cluster Hash Tags `{...}`**:
- When `LOCUTUS_CLUSTER=1` or `LOCUTUS_REDIS_CLUSTER=1` is set, Locutus automatically encapsulates the project namespace in curly braces:
  `{locutus:<project>}:inbox:<name>`
  `{locutus:<project>}:active_agents`
  `{locutus:<project>}:tag:<t>`
  `{locutus:<project>}:heartbeat:<name>`
- Redis only hashes the substring inside `{...}` to compute the slot number, guaranteeing that **all keys for the project reside on the exact same cluster shard**.
- Alternatively, you can specify custom hash tags directly in `LOCUTUS_REDIS_PREFIX`:
  ```bash
  export LOCUTUS_REDIS_PREFIX="{my-cluster-team}:"
  ```

## Assistant Integration (Skill & Slash Commands)

Locutus is packaged as an assistant skill for Claude Code, Antigravity, and other coding assistants:

- **Skill Specification**: [`SKILL.md`](SKILL.md) (streamlined to 99 lines for minimal context overhead)
- **Slash Commands**: `/locutus open`, `/locutus send`, `/locutus broadcast`, `/locutus who`, `/locutus tag`, `/locutus close`
- **Wire Specification**: [`references/wire_spec.md`](references/wire_spec.md)
- **Validation Schema**: [`tests/schema.py`](tests/schema.py) (strict Pydantic envelope model)

---

## Performance & Benchmarks

Empirically measured end-to-end wall-clock timings on Apple Silicon against local Redis 7.2 via [`tests/benchmark.py`](tests/benchmark.py):

| Metric | Measurement | Description |
| :--- | :--- | :--- |
| **Binary Size** | `308 KB` | Standalone static binary, zero runtime dependencies |
| **Cold Process Startup** | `~5.5 ms` | Full process spawn, arg parsing, OpenSSL bindings |
| **End-to-End Send Dispatch** | `~11.9 ms` | CLI invocation, HMAC-SHA256 signature, JSON encode, EVALSHA |
| **Optional E2EE 150KB Send + Listen** | `~39.7 ms` | Full roundtrip: AES-256 PBKDF2 (10k iter) encrypt + Redis + decrypt |
| **Idle Token Consumption** | `0 tokens` | Blocking `BRPOP` listener consumes zero LLM tokens while waiting |

---

## Testing & Verification

Locutus includes a 100% automated black-box test suite:

```bash
# Run all 32 unit tests (Protocol, Security, Native Binary, Cross-Runtime)
.venv/bin/python3 -m unittest discover tests

# Run live single-agent autonomous Ollama test
.venv/bin/python3 tests/test_ollama_agent.py

# Run live multi-agent autonomous ping-pong test
.venv/bin/python3 tests/test_multi_agent_pingpong.py
```

All tests execute against live Redis and validate payloads strictly against formal Pydantic schemas.

---

## License

Locutus is open-source software licensed under the [MIT License](LICENSE).
Copyright (c) 2026 Axiomantic.
