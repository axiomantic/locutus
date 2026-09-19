---
name: locutus
description: "Locutus: Zero-Glue Redis Inter-Assistant command dispatcher. Subcommands: open, send, broadcast, who, tag, unregister/close."
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
| `/locutus unregister` (or `/locutus close`) | Deregisters from active roster, clears tag sets, and deletes heartbeat | `/locutus unregister` |

## Execution Protocol for Assistants

When the user invokes `/locutus <subcommand>`:
1. Ensure `LOCUTUS_REDIS_URL`, `LOCUTUS_REDIS_PREFIX`, `LOCUTUS_PROJECT`, and `LOCUTUS_SCRIPTS_DIR` are resolved per the `locutus` skill.
2. For `/locutus open`:
   - Pick `<name>` if omitted: `"${LOCUTUS_PROJECT}-worker-$RANDOM"`.
   - Tags: `"${LOCUTUS_PROJECT},${tags}"`.
   - Run `register.lua` and `drain.lua`.
   - **Print the registration banner**:
     ```text
     ====================================================
     [LOCUTUS BUS] Registered Successfully
     - Agent Name : <name>
     - Project    : <LOCUTUS_PROJECT>
     - Tags       : <tags>
     - Redis URL  : <LOCUTUS_REDIS_URL> (prefix: <LOCUTUS_REDIS_PREFIX>)
     - Security   : HMAC-SHA256 authenticated (Air-Gap Prompt Firewall)
     - Status     : Active & Listening on inbox
     ====================================================
     ```
   - Dispatch background listener using the secure firewall wrapper:
     ```bash
     "$LOCUTUS_SCRIPTS_DIR/listen.sh" "<name>" 90
     ```
3. For `/locutus send`:
   - Execute secure authenticated dispatcher:
     ```bash
     "$LOCUTUS_SCRIPTS_DIR/send.sh" --to "<to>" --type "task" --subject "<subject>" --body "<body>"
     ```
4. For `/locutus broadcast`:
   - If tags not specified, scope to `"${LOCUTUS_PROJECT}"`. If specified, scope to `"${LOCUTUS_PROJECT},${tags}"` unless `*` or `@all` is passed.
   - Execute secure broadcast:
     ```bash
     "$LOCUTUS_SCRIPTS_DIR/send.sh" --broadcast --tags "${tags:-$LOCUTUS_PROJECT}" --subject "<subject>" --body "<body>"
     ```
5. For `/locutus who`:
   - Execute `directory.lua` passing filter (defaults to `$LOCUTUS_PROJECT`).
6. For `/locutus tag`:
   - Execute `tag.lua` passing action and tags.
7. For `/locutus unregister` (or `/locutus close`):
   - Execute `unregister.lua`: `redis-cli -u "$LOCUTUS_REDIS_URL" EVAL "$(cat "$LOCUTUS_SCRIPTS_DIR/unregister.lua")" 0 "$LOCUTUS_REDIS_PREFIX" "<name>"`.

