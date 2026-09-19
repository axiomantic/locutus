<div align="center">

# Locutus

**Fast, simple message exchange between AI coding assistants over Redis.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/axiomantic/locutus/actions/workflows/ci.yml/badge.svg)](https://github.com/axiomantic/locutus/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/Tests-30%20Passing-success.svg)](tests/)
[![Redis](https://img.shields.io/badge/Redis-6.2%2B-red.svg)](https://redis.io)
[![Nim](https://img.shields.io/badge/Nim-2.0%2B-yellow.svg)](https://nim-lang.org)
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Linux%20%7C%20Windows-blue.svg)](README.md)

*Connect multiple AI coding assistants across terminals, editors, and machines with a single command-line tool. No background services or daemons required.*

</div>

---

## What is Locutus?

**Locutus** is a command-line tool that lets AI coding assistants (such as Claude Code, Cursor, Windsurf, Antigravity, and Ollama) send messages to each other.

Instead of running a complex background service, Locutus stores queues and routes messages directly through **Redis**.

### Why no background service?

Most multi-agent frameworks require running a background server process (like a Python web server or message broker). That creates extra operational work:
- You must start, monitor, and restart server processes.
- You must configure network ports, firewall rules, and connections.
- Background processes use CPU and memory continuously, even when idle.

**Locutus has no background daemon.** It is a single compiled binary that runs commands directly against Redis (`locutus send`, `locutus listen`). Redis manages the queues and delivers messages when assistants request them.

### How it works with Redis

Locutus maps communication directly onto standard Redis data structures:

1. **Inboxes (Redis Lists)**:
   - Each assistant has an inbox list (`locutus:inbox:<agent>`).
   - Senders push messages to the list with `LPUSH`.
   - Receivers wait for messages with `BRPOP`. This blocking wait happens inside Redis, so idle listeners consume **zero CPU** and **zero AI tokens** while waiting.

2. **Roster and Tags (Redis Sets)**:
   - Active assistants and their role tags (like `backend`, `frontend`, `qa`) are saved in Redis sets.
   - You can see who is online instantly with `locutus who`.

3. **Group Messaging (Set Intersection)**:
   - When sending to a group (for example, `locutus broadcast --tags "qa"`), Redis finds matching assistants directly on the server using set intersection.

4. **Automatic Cleanup (Expiration)**:
   - **Heartbeats**: Active assistants refresh a 150-second key. If an assistant exits or crashes, it is automatically removed from the active roster.
   - **Inboxes**: Inboxes have a 7-day expiration that refreshes with every new message, automatically cleaning up abandoned queues.

5. **Redis Cluster Support**:
   - In a Redis Cluster, Locutus groups project keys using hash tags (such as `{locutus:project}:inbox:<name>`). This ensures all keys for a project live on the same cluster node, preventing multi-key errors.

### Comparison

| Traditional Agent Frameworks | Locutus Architecture |
| :--- | :--- |
| ❌ Heavy Python/Node background server daemons | ⚡ **Daemonless**: Single CLI tool; direct Redis calls |
| ❌ Complex WebSocket/HTTP setup requiring open ports | ⚡ **Standard Redis**: Works with local or hosted Redis (AWS, Upstash, Redis Cluster) |
| ❌ High memory usage and slow startup | ⚡ **Fast and lightweight**: Single small native binary with instant startup |
| ❌ Vulnerable to prompt injection from untrusted messages | ⚡ **Built-in Authentication**: Drops unauthenticated or tampered messages automatically |
| ❌ Idle listeners consume continuous AI tokens | ⚡ **Zero-token idle**: Blocking wait consumes 0 AI tokens while waiting for work |

---

## How Messages Flow


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
*Or tap directly:*
```bash
brew tap axiomantic/tap
brew install locutus
```

### Method B: Debian / Ubuntu APT Repository
```bash
# Add the official APT repository
echo "deb [trusted=yes] https://axiomantic.github.io/locutus/apt/ ./" | sudo tee /etc/apt/sources.list.d/locutus.list

# Update package index and install
sudo apt-get update
sudo apt-get install -y locutus
```

### Method C: Universal One-Line Installer (macOS & Linux)
```bash
curl -fsSL https://raw.githubusercontent.com/axiomantic/locutus/main/scripts/install.sh | bash
```
*Intelligently prefers native package managers (`brew` / `dpkg`), falls back to pre-compiled binary tarballs with SHA256 checksum verification, and automatically compiles from source (installing Nim if needed) if no pre-built binary matches the architecture.*

### Method D: Windows PowerShell Installer
Run in PowerShell (Admin not required):
```powershell
irm https://raw.githubusercontent.com/axiomantic/locutus/main/scripts/install.ps1 | iex
```
*Installs via Scoop if available, falls back to pre-compiled `.zip` archive into `%LOCALAPPDATA%\Programs\locutus`, configures User `PATH`, and falls back to building via Nim if required.*

### Method E: Scoop (Windows)
```powershell
scoop install https://raw.githubusercontent.com/axiomantic/locutus/main/packaging/scoop/locutus.json
```

### Method F: Standalone Binaries & Manual .deb Download
Pre-compiled standalone archives and `.deb` files are attached to every [GitHub Release](https://github.com/axiomantic/locutus/releases):

| OS | Architecture | Package |
| :--- | :--- | :--- |
| **macOS** | Apple Silicon (M1/M2/M3/M4) | `locutus-darwin-arm64.tar.gz` |
| **macOS** | Intel x86_64 | `locutus-darwin-amd64.tar.gz` |
| **Linux** | x86_64 (amd64) | `locutus-linux-amd64.tar.gz` / `.deb` |
| **Linux** | ARM64 (aarch64) | `locutus-linux-arm64.tar.gz` / `.deb` |
| **Windows** | x86_64 (amd64) | `locutus-windows-amd64.zip` |

---

## Easy Uninstallation

Uninstalling Locutus is clean and takes a single command:

```bash
# macOS / Linux Universal Uninstaller:
curl -fsSL https://raw.githubusercontent.com/axiomantic/locutus/main/scripts/install.sh | bash -s -- --uninstall

# Windows PowerShell Uninstaller:
irm https://raw.githubusercontent.com/axiomantic/locutus/main/scripts/install.ps1 | iex -ArgumentList "-Uninstall"

# Or using your system package manager:
brew uninstall locutus          # Homebrew
sudo dpkg -r locutus            # Debian / Ubuntu
scoop uninstall locutus         # Windows Scoop
```
*Note: Configuration files in `~/.config/locutus` are preserved. To completely purge configurations and secret keys, run `rm -rf ~/.config/locutus`.*

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


## Security

Locutus protects coding assistants from prompt injection, forged messages, and unauthorized commands:

```text
Incoming Redis Data ──► [Host: locutus listen]
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
           Processes Safe Task           (Attacker Blocked)
```

1. **Host-Level Verification**: Messages are checked by `locutus listen` on your computer before reaching standard output.
2. **Untrusted Messages Dropped**: Forged or unauthenticated messages are rejected immediately. They never enter the assistant's context window.
3. **Local Secret**: The secret key (`~/.config/locutus/secret`, `0600` permissions) stays on your machine. It never enters prompts, Git commits, or Redis keys.
4. **Optional Encryption**: Set `LOCUTUS_ENCRYPT=1` to encrypt message bodies with AES-256-CBC, ensuring plain text is never stored in Redis.

---

## Message and Assistant Lifecycles

Locutus cleans up keys automatically so Redis memory does not grow unbounded:

- **Inboxes**: Inboxes expire after **7 days** of inactivity. Every new message resets the 7-day timer.
- **Heartbeats**: Active listeners refresh a **150-second heartbeat**. If an assistant exits or crashes, its heartbeat expires.
- **Automatic Cleanup**: When messages or queries are routed, assistants with expired heartbeats are automatically removed from the roster and tag groups.

---

## Redis Cluster Support (Hash Tags)

In a Redis Cluster, keys are distributed across multiple shards. Multi-key operations (`SINTER`, `SMEMBERS`) require that related keys live on the same shard.

Locutus supports Redis Cluster hash tags automatically:
- Set `LOCUTUS_CLUSTER=1` (or `cluster = true` in config).
- Locutus wraps the project prefix in curly brackets: `{locutus:<project>}:inbox:<name>`.
- Redis hashes only the text inside `{...}`, guaranteeing that **all keys for the same project live on the exact same cluster shard**.
- You can also specify custom hash tags directly in `prefix` (for example, `prefix = "{team-alpha}:"`).


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
