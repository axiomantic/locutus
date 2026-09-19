---
name: locutus
description: "Locutus: Zero-Glue Redis Agent-to-Agent (A2A) command dispatcher. Subcommands: open, send, broadcast, who, tag, close."
---

# /locutus Command Dispatch

Dispatches inter-agent communication operations over the Redis Locutus bus.

## Subcommands

| Subcommand | Description | Example |
| :--- | :--- | :--- |
| `/locutus open [name] [tags]` | Registers identity, drains backlog, prints banner, and arms `BRPOP` listener | `/locutus open coder-1 "backend,qa"` |
| `/locutus send <to> <subject> <body>` | Sends direct task/query (O2O) with structured JSON envelope | `/locutus send worker-2 "Run tests" "pytest tests/"` |
| `/locutus broadcast [tags] <subject> <body>` | Multicasts with AND-filter matching. Defaults to current project | `/locutus broadcast "qa" "Deploy ready" "Please verify"` |
| `/locutus who [filter]` | Lists team members in project. Pass `*` for global discovery | `/locutus who` or `/locutus who "*"` |
| `/locutus tag <add\|remove\|set> <tags>` | Dynamically manages tags without reregistering | `/locutus tag add "ticket-42"` |
| `/locutus close` | Deregisters from active roster and clears heartbeat | `/locutus close` |

## Execution Protocol for Assistants

When the user invokes `/locutus <subcommand>`:
1. Ensure `A2A_REDIS_URL`, `A2A_REDIS_PREFIX`, `A2A_PROJECT`, and `A2A_SCRIPTS_DIR` are resolved per the `redis-a2a` skill.
2. For `/locutus open`:
   - Pick `<name>` if omitted: `"${A2A_PROJECT}-worker-$RANDOM"`.
   - Tags: `"${A2A_PROJECT},${tags}"`.
   - Run `register.lua` and `drain.lua`.
   - **Print the registration banner**:
     ```text
     ====================================================
     [LOCUTUS BUS] Registered Successfully
     - Agent Name : <name>
     - Project    : <A2A_PROJECT>
     - Tags       : <tags>
     - Redis URL  : <A2A_REDIS_URL> (prefix: <A2A_REDIS_PREFIX>)
     - Status     : Active & Listening on inbox
     ====================================================
     ```
   - Dispatch background listener: `redis-cli -u "$A2A_REDIS_URL" BRPOP "${A2A_REDIS_PREFIX}inbox:<name>" 90`.
3. For `/locutus send`:
   - Construct JSON payload matching the `A2AMessage` schema.
   - Execute `send_o2o.lua`.
4. For `/locutus broadcast`:
   - If tags not specified, scope to `"${A2A_PROJECT}"`. If specified, scope to `"${A2A_PROJECT},${tags}"` unless `*` is passed.
   - Execute `multicast.lua`.
5. For `/locutus who`:
   - Execute `directory.lua` passing filter (defaults to `$A2A_PROJECT`).
6. For `/locutus tag`:
   - Execute `tag.lua` passing action and tags.
7. For `/locutus close`:
   - Execute `unregister.lua`.
