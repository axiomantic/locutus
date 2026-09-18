# Locutus: Zero-Glue Redis Agent-to-Agent (A2A) Bus

Locutus provides a **100% prompt-based** inter-assistant communication protocol over Redis. It requires **no external JavaScript, Python packages, background daemons, or glue code**.

It enables multiple coding assistants (Claude Code, Antigravity, Cursor, Windsurf, Aider, Copilot, etc.) across different terminals, workspaces, or machines to coordinate work in real time.

---

## Features

- **100% Prompt-Driven**: Self-contained instructions with embedded Lua scripts executed directly via standard `redis-cli`.
- **Namespaced & Non-Destructive**: All keys use a configurable prefix (default `a2a:`). Will not collide with existing Redis caches or databases.
- **Zero-Token Idle Watcher**: Uses OS-level blocking `BRPOP` exit-chains. Idle sessions consume **0 CPU and 0 tokens** while waiting for work.
- **Direct (O2O) & Multicast (O2M)**: Point-to-point tasks and group fan-out by tags (e.g. `@qa`, `@backend`, `@all`).
- **Offline Backlog Delivery**: Messages waiting for offline or unregistered agents are queued in Redis and delivered as a batch upon startup.
- **Automatic Dead-Agent Pruning**: Expired agent heartbeats are automatically pruned during multicast fan-out, preventing phantom queue memory leaks.
- **Built-in Reply Threading**: Native `id`, `reply_to`, and `timestamp` fields for structured conversation flows.

---

## Quick Installation Across Assistants

### 1. Claude Code & Antigravity (Skill)
Copy `SKILL.md` to your skills directory:
```bash
# Global skill directory
mkdir -p ~/.gemini/config/skills/redis-a2a
cp SKILL.md ~/.gemini/config/skills/redis-a2a/SKILL.md

# Or project-level Claude skills
mkdir -p .claude/skills/redis-a2a
cp SKILL.md .claude/skills/redis-a2a/SKILL.md
```

### 2. Cursor / Windsurf
Append the content of `SKILL.md` to:
- `.cursorrules` (in project root)
- Or Cursor **System Prompts / Custom Rules**

### 3. Aider / CLI Assistants
Add to your custom instructions file or invoke with:
```bash
aider --read SKILL.md
```

---

## Getting Started

### 1. Set Redis URL
Ensure `REDIS_URL` is set in your shell, `.env`, or `~/.redis_a2a_env`:
```bash
export REDIS_URL="redis://127.0.0.1:6379"
```

### 2. Instruct Your Assistant
Tell any assistant session:
> *"Load the redis-a2a skill. Register as `coder-1` with tags `backend,ticket-104`, and arm your listener."*

In another assistant session:
> *"Load the redis-a2a skill. Check who is online and send a task to `coder-1` to run tests and report back."*

---

## File Structure

- [`SKILL.md`](./SKILL.md): The canonical prompt and protocol specification containing all embedded Lua scripts, JSON schemas, and command templates.
