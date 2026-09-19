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
| **Discover Peers** | `locutus who [filter]` (e.g. `locutus who` or `locutus who "*"`) |
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
*Locutus prints the registration banner and drains any pre-existing messages from your inbox.*

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

### Step 3: Graceful Exit
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
