<div align="center">

# Locutus

**Zero-Glue Cross-Assistant Communication Bus over Redis**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-32%20Passing-success.svg)](tests/)
[![Redis](https://img.shields.io/badge/Redis-6.2%2B-red.svg)](https://redis.io)
[![Nim](https://img.shields.io/badge/Nim-2.0%2B-yellow.svg)](https://nim-lang.org)
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Linux-lightgrey.svg)](README.md)

*Sub-millisecond inter-agent coordination across terminals, projects, and machines with zero background daemons and a cryptographic prompt-injection firewall.*

</div>

---

## What is Locutus?

**Locutus** connects multiple AI coding assistants (Claude Code, Antigravity, Cursor, Windsurf, Aider, Ollama) across different terminals, projects, or servers using pure Redis primitives and an ultra-fast compiled binary.

### Why Locutus?

| Traditional Agent Frameworks | Locutus Architecture |
| :--- | :--- |
| ❌ Heavy Python/Node background server daemons | ⚡ **Zero background processes**: Pure atomic Redis lists + sets |
| ❌ Fragile WebSocket / HTTP bridges requiring open ports | ⚡ **Standard Redis**: Works over local or hosted Redis (AWS, Upstash) |
| ❌ 500ms+ startup latency & high RAM overhead | ⚡ **Sub-millisecond latency**: 289 KB standalone Nim binary (1ms cold start) |
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

### 2. Clone & Build Locutus
```bash
# Clone
git clone https://github.com/axiomantic/locutus.git
cd locutus

# Compile standalone native binary (under 1 second)
nim c -d:release -o:bin/locutus src/locutus.nim
mkdir -p ~/.local/bin && cp bin/locutus ~/.local/bin/locutus
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
2. **Untrusted Data Dropped**: Unauthenticated payloads are completely dropped before they can enter an LLM's context window.
3. **Zero Secret Leakage**: The cluster secret (`~/.config/locutus/secret`, `0600`) never enters LLM prompts, Git commits, or Redis keys.
4. **End-to-End Encryption (E2EE)**: Set `LOCUTUS_ENCRYPT=1` to encrypt task bodies with OpenSSL AES-256-CBC PBKDF2 (10,000 iterations). Raw plaintext never touches Redis memory or persistence.

---

## Assistant Integration (Skill & Slash Commands)

Locutus is packaged as an assistant skill for Claude Code, Antigravity, and other coding assistants:

- **Skill Specification**: [`SKILL.md`](SKILL.md) (streamlined to 99 lines for minimal context overhead)
- **Slash Commands**: `/locutus open`, `/locutus send`, `/locutus broadcast`, `/locutus who`, `/locutus tag`, `/locutus close`
- **Wire Specification**: [`references/wire_spec.md`](references/wire_spec.md)
- **Validation Schema**: [`tests/schema.py`](tests/schema.py) (strict Pydantic envelope model)

---

## Performance & Benchmarks

Measured on Apple Silicon (M-series) against local Redis 7.2:

| Metric | Measurement |
| :--- | :--- |
| **Binary Size** | `289 KB` (Standalone static binary, zero runtime dependencies) |
| **Cold Startup Time** | `< 1.5 ms` |
| **Redis Command Execution** | `0.4 ms` (Cached `EVALSHA` roundtrip) |
| **E2EE 150KB Payload Roundtrip** | `< 4 ms` (AES-256-CBC encryption + HMAC + decryption) |
| **Idle Token Consumption** | `0 tokens` (Blocking `BRPOP` listener) |

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
