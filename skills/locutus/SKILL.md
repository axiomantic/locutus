---
name: locutus
description: "Locutus: Daemonless cross-assistant communication bus over Redis. Enables multiple coding assistants across terminals, projects, or machines to register, discover teams, send direct (O2O) and multicast (O2M) work messages, receive backlogged offline messages, and maintain an event-driven background listener using embedded Redis Lua scripts."
---

# Locutus: Redis Inter-Assistant Communication Bus

Locutus is a daemonless, high-performance inter-assistant communication protocol and CLI engine over Redis. It provides cryptographic HMAC-SHA256 authentication, air-gapped prompt-injection defense, and Redis `EVALSHA` caching with sub-millisecond execution.

---

## 1. Quick Reference & Core Invariants

1. **Native Single-Binary Engine**: All coordination is executed via the high-speed `locutus` binary (built in Nim with compile-time embedded Lua and EVALSHA caching).
2. **Namespace & Team Isolation**:
   - Keys use `$LOCUTUS_REDIS_PREFIX` (default: `locutus:`).
   - Agents are tagged with their project (`$LOCUTUS_PROJECT`).
   - Multicasts are **AND filters** across tags (`project,tag`). Global broadcasts use `*` or `@all`.
3. **Queue Architecture (Single Inbox per Agent)**:
   - Every agent listens to `${LOCUTUS_REDIS_PREFIX}inbox:<my_name>`.
   - Offline messages queue in Redis (7-day default TTL) and are delivered upon reconnect.
4. **Air-Gap Prompt-Injection Firewall**:
   - All messages require valid HMAC-SHA256 signatures derived from `~/.config/locutus/secret` (0600 mode).
   - `locutus listen` drops unauthenticated, forged, or tampered payloads at the process boundary before reaching stdout. The assistant never receives malicious prompts into its context window.
   - Optional E2EE: Setting `LOCUTUS_ENCRYPT=1` encrypts task bodies via OpenSSL AES-256-CBC PBKDF2 across Redis.
5. **Drain-Execute-Rearm Discipline**:
   - `locutus listen 90` blocks until a message arrives or 90s expires.
   - On timeout: `locutus` outputs `(nil)`. Refresh heartbeat and re-arm.
   - On message: execute task, send reply, and re-arm `locutus listen 90` in the *same turn*.
6. **Agent Identity & Host Isolation**:
   - Multiple assistants on the same computer are isolated via process environment (`export LOCUTUS_AGENT_NAME=<name>`) and workspace directory (`.locutus.agent`).
   - `locutus listen` requires an identifiable agent name (explicit argument, `LOCUTUS_AGENT_NAME`, or workspace `.locutus.agent`).
   - Active listeners that attach via `locutus listen <name>` are automatically registered into the live directory.
   - `locutus who` automatically prunes dead/expired agents upon query, returning only truly active agents.

---

## 2. CLI Command Reference

Locutus auto-discovers Redis configuration from `LOCUTUS_REDIS_URL`, `AGENTS.md`, `.env`, or local defaults.

| Action | Command |
| :--- | :--- |
| **Register & Announce** | `locutus open [name] [tags]` |
| **Arm Background Listener** | `locutus listen [name] [timeout_sec]` |
| **Send Direct Task (O2O)** | `locutus send --to <recipient> --subject "<subj>" --body "<body>"` |
| **Send Reply** | `locutus send --to <sender> --type reply --subject "Re: <subj>" --body "<body>" --reply-to <msg_id>` |
| **Broadcast (O2M)** | `locutus broadcast --tags "<tags>" --subject "<subj>" --body "<body>"` |
| **Synchronous RPC** | `locutus request --to <recipient> --subject "<subj>" --body "<body>" [--timeout 30] [--raw]` |
| **Produce to Work Queue** | `locutus enqueue <queue_name> --subject "<subj>" --body "<body>"` |
| **Consume from Work Queue** | `locutus work <queue_name> [timeout_sec]` |
| **Set Status & Activity** | `locutus status <idle\|busy\|error> [activity_text]` |
| **Distributed Mutex Lock** | `locutus lock <lock_name> [ttl_sec]` |
| **Distributed Mutex Unlock** | `locutus unlock <lock_name>` |
| **Ephemeral Pub/Sub Send** | `locutus pub <channel> "<message>"` |
| **Ephemeral Pub/Sub Recv** | `locutus sub <channel> [timeout_sec]` |
| **Discover Peers** | `locutus who [-a\|--all] [--json] [tag]` (e.g. `locutus who`, `locutus who -a`, `locutus who --json`) |
| **Dynamic Tags** | `locutus tag <add\|remove\|set> <tags>` |
| **Drain Backlog** | `locutus drain [count]` |
| **Unregister / Close** | `locutus close` |


---

## 3. Step-by-Step Lifecycle Guide for Assistants

### Step 1: Open Connection & Register
```bash
locutus open
# Or with specific identity:
locutus open my-agent-1 "backend,qa"
```
*Locutus prints the registration banner, isolates the agent in `.locutus.agent`, and drains any pre-existing messages from your inbox.*

> **Tip for Multi-Agent Host Isolation**:
> When running multiple agents across terminal tabs on the same computer, export your agent name in the shell to ensure complete process-level isolation:
> ```bash
> export LOCUTUS_AGENT_NAME="my-agent-1"
> ```

### Step 2: Arm the Secure Background Listener
Launch `locutus listen` as a background command:
```bash
locutus listen 90
```
- In assistants with background task notifications (Claude Code, Antigravity): stop calling tools and wait for wakeup notification.
- **Handling Listener Output**:
  - **Output is `(nil)` (90s Timeout)**:
    Re-arm immediately:
    ```bash
    locutus listen 90
    ```
  - **Output contains JSON message**:
    1. Parse JSON payload (`id`, `from`, `subject`, `body`).
    2. Perform requested work (run tests, edit files, research).
    3. Send unicast reply back to `from`:
       ```bash
       locutus send --to "<from>" --type reply --subject "Re: <subject>" --body "<result>" --reply-to "<id>"
       ```
    4. **Re-arm the listener in the same turn** before concluding:
       ```bash
       locutus listen 90
       ```

### Step 3: Advanced Coordination Protocols

#### A. Synchronous RPC (`locutus request`)
When you need an immediate answer or calculation from a specific peer before proceeding:
```bash
locutus request --to <peer> --subject "<query>" --body "<input>" [--timeout 30] [--raw]
```
- Dispatches the task with a dedicated ephemeral reply key (`reply:<req_id>`).
- Blocks on Redis `BRPOP` until the response arrives or timeout occurs.
- `--raw` flag outputs the bare decrypted body directly for shell piping.

#### B. Competing-Consumers Task Queues (`locutus enqueue` / `locutus work`)
When coordinating independent tasks across a pool of worker agents:
- **Producer**:
  ```bash
  locutus enqueue <queue_name> --subject "<title>" --body "<payload>"
  ```
- **Worker (Consumer)**:
  ```bash
  locutus work <queue_name> 60
  ```
  Guarantees exactly-once consumption across all competing workers.

#### C. Distributed Mutex Locking (`locutus lock` / `locutus unlock`)
When executing critical sections that must not run concurrently across agents (e.g. git rebase, running migrations, deploying staging):
```bash
# Acquire lease (returns 0 on success, 1 on conflict):
locutus lock <lock_name> 30

# Perform critical operation...

# Release lease (guarantees only owner can unlock):
locutus unlock <lock_name>
```

#### D. Agent Operational State & Activity Tracking (`locutus status`)
Keep teammates and coordinators informed of your current focus:
```bash
locutus status busy "Running full regression suite"
# When ready for new work:
locutus status idle "Awaiting next task"
```
Inspect peer states and activities cluster-wide via `locutus who "*"`.

#### E. Ephemeral Pub/Sub Streaming (`locutus pub` / `locutus sub`)
Broadcast transient announcements where persistence/queue backlog is unnecessary:
```bash
# Subscriber:
locutus sub alerts 10

# Publisher:
locutus pub alerts "Build completed"
```

### Step 4: Graceful Exit
When the session ends or user asks to disconnect:
```bash
locutus close
```


---

## 4. Fallback Modes

If the compiled `locutus` binary is not in PATH:
1. **Run via Nim directly**: `nim r src/locutus.nim [args...]`
2. **Build binary**: `nim c -d:release -o:~/.local/bin/locutus src/locutus.nim`
3. **Raw Lua Scripts**: Execute `redis-cli -u "$LOCUTUS_REDIS_URL" EVAL "$(cat "scripts/<script>.lua")" ...`
