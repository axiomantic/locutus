<div align="center">

# Locutus

**Fast, simple message exchange between AI coding assistants over Redis.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/axiomantic/locutus/actions/workflows/ci.yml/badge.svg)](https://github.com/axiomantic/locutus/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/Tests-46%20Passing-success.svg)](tests/)
[![Redis](https://img.shields.io/badge/Redis-6.2%2B-red.svg)](https://redis.io)
[![Nim](https://img.shields.io/badge/Nim-2.0%2B-yellow.svg)](https://nim-lang.org)
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Linux%20%7C%20Windows-blue.svg)](README.md)

*Connect multiple AI coding assistants across terminals, editors, and machines with a single command-line tool. No background services or daemons required.*

</div>

---

## Table of Contents

- [What is Locutus?](#what-is-locutus)
- [30-Second Quickstart](#30-second-quickstart)
- [How it Works with Redis](#how-it-works-with-redis)
- [Comparison](#comparison)
- [How Messages Flow](#how-messages-flow)
- [Installation & Setup](#installation--setup)
  - [Option 1: Unified One-Line Installer (Recommended)](#option-1-unified-one-line-installer-recommended)
  - [Option 2: Install AI Agent Skill (Using Skill Tools)](#option-2-install-the-ai-agent-skill-using-skill-tools)
  - [Option 3: Install Native Engine (Binary / Package Managers)](#option-3-install-the-native-engine-binary--package-managers)
- [Uninstallation](#uninstallation)
- [CLI Reference](#cli-reference)
- [Configuration Architecture & Profiles](#configuration-architecture--profiles)
- [Security & Prompt Firewall](#security--prompt-injection-firewall)
- [Redis Cluster Support](#redis-cluster-support-hash-tags)
- [Assistant Integration](#assistant-integration-skill--slash-commands)
- [Performance & Benchmarks](#performance--benchmarks)
- [Testing & Verification](#testing--verification)
- [License](#license)

---

## What is Locutus?

**Locutus** is an inter-agent communication bus that lets AI coding assistants (such as Claude Code, Cursor, Windsurf, Antigravity, and Ollama) exchange tasks and messages across terminals, editors, and machines.

Instead of running a complex background server, Locutus routes and queues messages directly through **Redis**.

---

## 30-Second Quickstart

### 1. Install (Engine + AI Agent Skills)

```bash
# macOS & Linux: Installs native binary and equips detected coding assistants
curl -fsSL https://raw.githubusercontent.com/axiomantic/locutus/main/scripts/install.sh | bash

# Or on macOS via Homebrew:
# brew install axiomantic/tap/locutus

# Windows (PowerShell):
# irm https://raw.githubusercontent.com/axiomantic/locutus/main/scripts/install.ps1 | iex
```

### 2. Try it in Your Terminals

**Terminal A (Worker 1):**
```bash
locutus open worker-1 "backend,qa"
locutus listen 90
```
*Registers `worker-1` and waits for incoming tasks with zero CPU and zero token consumption.*

**Terminal B (Worker 2):**
```bash
locutus open worker-2 "frontend,qa"
locutus listen 90
```
*Registers `worker-2` and waits on its own inbox.*

**Terminal C (Coordinator / Sender):**
```bash
# 1-to-1 Direct Task (O2O):
locutus send --to worker-1 --subject "Run Tests" --body "pytest tests/auth"

# 1-to-Many Group Broadcast (O2M):
locutus broadcast --tags "qa" --subject "Deploy Staging" --body "Verify build v1.2"
```
*Terminal A receives the direct task; both Terminal A and Terminal B receive the multicast broadcast instantly.*

### 3. Multi-Assistant Chat Coordination (Orchestrator & Workers)

You can coordinate multiple coding assistants across different terminal windows or editors using natural language:

**Terminal 1 — The Orchestrator (Lead Assistant):**
> *"You are the coordinator for this project. Connect to Locutus as lead. Check who is online with `/locutus who`, broadcast the test plan to the 'qa' group, and assign API work to 'backend'."*
- The lead registers (`locutus open lead "orchestrator"`), inspects the active roster (`locutus who`), and broadcasts work:
  ```bash
  locutus broadcast --tags "qa" --subject "Test Plan" --body "Validate auth endpoints on staging"
  locutus broadcast --tags "backend" --subject "API Task" --body "Implement POST /api/v1/login"
  ```

**Terminal 2 — Backend Worker Assistant (e.g. Claude Code or Cursor):**
> *"Connect to Locutus as worker-backend with tag 'backend'. Listen for tasks, implement them, and send replies back to lead."*
- The worker registers (`locutus open worker-backend "backend"`), blocks on `locutus listen 90` (consuming **0 CPU** and **0 tokens** while waiting), receives the task, implements the code, and replies:
  ```bash
  locutus send --to lead --type reply --subject "Re: API Task" --body "Login endpoint implemented in src/auth.py. Tests green."
  ```

**Terminal 3 — QA Worker Assistant (e.g. Antigravity or Windsurf):**
> *"Connect to Locutus as worker-qa with tag 'qa'. Listen for incoming test requests."*
- The QA worker automatically receives the broadcast sent to `@qa` and begins running validation tests in parallel.

---

## How it Works with Redis

Locutus has **no background daemon or server process**. It is a single compiled binary that runs atomic commands directly against Redis (`locutus send`, `locutus listen`). Redis manages the queues and delivers messages when assistants request them.

Locutus maps communication directly onto standard Redis data structures:

1. **Zero-Token, Zero-CPU Inboxes (Redis Lists)**:
   - Each assistant has an inbox list (`locutus:inbox:<agent>`).
   - Senders push messages with `LPUSH`.
   - Receivers wait for messages with `BRPOP`. This blocking wait happens entirely inside the Redis server, so idle listeners consume **zero CPU** and **zero AI tokens** while waiting.

2. **Roster and Tags (Redis Sets)**:
   - Active assistants and their role tags (like `backend`, `frontend`, `qa`) are saved in Redis sets.
   - You can see who is online instantly with `locutus who`.

3. **Group Multicast Messaging (Set Intersection)**:
   - When sending to a group (for example, `locutus broadcast --tags "qa"`), Redis finds matching assistants directly on the server using set intersection (`SINTER`).

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

## Installation & Setup

Locutus consists of two components:
1. **The Native Engine (CLI Binary)**: High-speed, compiled binary (`locutus`) that communicates directly with Redis.
2. **The AI Agent Skill**: Instructions (`SKILL.md`) that teach your AI assistants (Claude Code, Antigravity, OpenCode, Codex, Cursor) how to use Locutus.

You can install them together in one step, or install each component separately:

### Option 1: Unified One-Line Installer (Recommended)

Installs the native binary **and** automatically configures skills across all detected AI assistants:

```bash
# macOS & Linux:
curl -fsSL https://raw.githubusercontent.com/axiomantic/locutus/main/scripts/install.sh | bash

# Windows (PowerShell):
irm https://raw.githubusercontent.com/axiomantic/locutus/main/scripts/install.ps1 | iex
```

### Option 2: Install the AI Agent Skill (Using Skill Tools)

If you already have the binary, or prefer to manage skills through standard AI package managers:

#### Via skills.sh (Vercel Labs)
```bash
# Install globally for all detected AI assistants:
npx -y skills add axiomantic/locutus -g -a '*' -y

# Or install for a specific project / assistant:
npx skills add axiomantic/locutus --agent claude-code
```

#### Via skilz (Spillwave Solutions)
```bash
# Install globally across 30+ supported agent runtimes:
skilz install https://github.com/axiomantic/locutus

# Or install for a specific project:
skilz install https://github.com/axiomantic/locutus --project
```

### Option 3: Install the Native Engine (Binary / Package Managers)

If you manage command-line tools with your system package manager:

#### Homebrew (macOS & Linux)
```bash
brew install axiomantic/tap/locutus

# Equip your coding assistants:
npx skills add axiomantic/locutus -g
# Or using skilz:
skilz install https://github.com/axiomantic/locutus
# Or offline from local Homebrew files:
npx skills add $(brew --prefix)/share/locutus/skills/locutus -g
```

#### Debian / Ubuntu APT Repository
```bash
# 1. Add official APT repository
echo "deb [trusted=yes] https://axiomantic.github.io/locutus/apt/ ./" | sudo tee /etc/apt/sources.list.d/locutus.list

# 2. Update and install
sudo apt-get update
sudo apt-get install -y locutus

# 3. Equip your coding assistants:
npx skills add /usr/share/locutus/skills/locutus -g
# Or using skilz:
skilz install -f /usr/share/locutus/skills/locutus
```

#### Windows Scoop
```powershell
scoop install https://raw.githubusercontent.com/axiomantic/locutus/main/packaging/scoop/locutus.json

# Scoop automatically runs post-install hooks to equip your skills.
# You can also manually equip or reconfigure at any time:
npx skills add "$dir\skills\locutus" -g
# Or using skilz:
skilz install -f "$dir\skills\locutus"
```

#### Standalone Pre-Compiled Binaries
Pre-built archives and Debian packages are attached to every [GitHub Release](https://github.com/axiomantic/locutus/releases):

| Operating System | Architecture | Package Archive |
| :--- | :--- | :--- |
| **macOS** | Apple Silicon (M1/M2/M3/M4) | `locutus-darwin-arm64.tar.gz` |
| **macOS** | Intel x86_64 | `locutus-darwin-amd64.tar.gz` |
| **Linux** | x86_64 (amd64) | `locutus-linux-amd64.tar.gz` / `.deb` |
| **Linux** | ARM64 (aarch64) | `locutus-linux-arm64.tar.gz` / `.deb` |
| **Windows** | x86_64 (amd64) | `locutus-windows-amd64.zip` |

---

## Uninstallation

You can remove the skill, the binary, or both:

### 1. Remove the AI Agent Skill (Using Skill Tools)

```bash
# Using skills.sh (npx):
npx skills remove locutus -g

# Using skilz:
skilz remove locutus
```

### 2. Remove the Binary / System Package

```bash
brew uninstall locutus          # Homebrew
sudo apt remove locutus         # Debian / Ubuntu (or sudo dpkg -r locutus)
scoop uninstall locutus         # Windows Scoop
```

### 3. Or Clean Unified Uninstaller (Removes Both Binary & Skills)

```bash
# macOS & Linux:
curl -fsSL https://raw.githubusercontent.com/axiomantic/locutus/main/scripts/install.sh | bash -s -- --uninstall

# Windows (PowerShell):
irm https://raw.githubusercontent.com/axiomantic/locutus/main/scripts/install.ps1 | iex -ArgumentList "-Uninstall"
```

*Note: Configuration files in `~/.config/locutus` are preserved. To completely purge configurations and secret keys, run `rm -rf ~/.config/locutus`.*

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


## Security & Prompt Injection Firewall

Locutus protects coding assistants from prompt injection, forged messages, and unauthorized execution:

```
Redis Inbox Payload ──► [Host: locutus listen] ──► HMAC-SHA256 Check ──► Delivered to LLM Stdout
                                                        │
                                                        └── (Forged/Tampered) ──► Dropped to Stderr
```

1. **Host-Level Verification**: Messages are cryptographically validated by `locutus listen` on your local host machine before reaching standard output.
2. **Untrusted Payloads Dropped**: Forged or unauthenticated messages are rejected immediately. They never enter the assistant's context window.
3. **Local Secret**: The secret key (`~/.config/locutus/secret`, `0600` permissions) stays on your machine. It never enters prompts, Git commits, or Redis keys.
4. **Optional End-to-End Encryption (E2EE)**: Set `LOCUTUS_ENCRYPT=1` to encrypt message bodies with AES-256-CBC PBKDF2, ensuring plain text is never stored in Redis.

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

- **Skill Specification**: [`SKILL.md`](SKILL.md) (streamlined to 98 lines for minimal context overhead)
- **Slash Commands**: `/locutus open`, `/locutus send`, `/locutus broadcast`, `/locutus who`, `/locutus tag`, `/locutus close`
- **Wire Specification**: [`references/wire_spec.md`](references/wire_spec.md)
- **Validation Schema**: [`tests/schema.py`](tests/schema.py) (strict Pydantic envelope model)

---

## Performance & Benchmarks

Empirically measured end-to-end wall-clock timings on Apple Silicon against local Redis 7.2 via [`tests/benchmark.py`](tests/benchmark.py):

| Metric | Measurement | Description |
| :--- | :--- | :--- |
| **Binary Size** | `~313 KB` | Standalone static binary (stripped), zero runtime dependencies |
| **Cold Process Startup** | `~5.3 ms` | Full process spawn, arg parsing, OpenSSL bindings |
| **End-to-End Send Dispatch** | `~12.8 ms` | CLI invocation, HMAC-SHA256 signature, JSON encode, EVALSHA |
| **Optional E2EE 150KB Send + Listen** | `~39.7 ms` | Full roundtrip: AES-256 PBKDF2 (10k iter) encrypt + Redis + decrypt |
| **Idle Token Consumption** | `0 tokens` | Blocking `BRPOP` listener consumes zero LLM tokens while waiting |

---

## Testing & Verification

Locutus includes a 100% automated, marked `pytest` suite:

```bash
# Run all hermetic unit tests (Protocol, Security, Native Binary, Cross-Platform Installer)
pytest -v -m "not llm"

# Run live LLM integration tests (Local Ollama)
RUN_LLM_TESTS=1 pytest -v -m llm

# Run live LLM integration tests (OpenRouter / Cloud API)
RUN_LLM_TESTS=1 LLM_API_KEY="sk-or-..." LLM_MODEL="deepseek/deepseek-chat:free" pytest -v -m llm
```

Continuous Integration (GitHub Actions) runs:
- **CI Workflow (`ci.yml`)**: Executes hermetic unit tests (`pytest -v -m "not llm"`) against live Redis services on Ubuntu Linux, macOS, and Windows on every push and pull request.
- **LLM CI Workflow (`llm-ci.yml`)**: Runs live model integration tests against OpenRouter (defaulting to free models such as `deepseek/deepseek-chat:free` or `nvidia/nemotron-3-nano-omni:free`) on release tags (`v*`), merges to `main`, and maintainer-reviewed pull requests.

All tests execute against live Redis and validate payloads strictly against formal Pydantic schemas.


---

## License

Locutus is open-source software licensed under the [MIT License](LICENSE).
Copyright (c) 2026 Axiomantic.
