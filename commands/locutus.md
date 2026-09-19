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
1. All coordination is performed directly via the native `locutus` binary.
2. For `/locutus open`:
   ```bash
   locutus open "<name>" "<tags>"
   ```
   Then dispatch background listener:
   ```bash
   locutus listen "<name>" 90
   ```
3. For `/locutus send`:
   ```bash
   locutus send --to "<to>" --type "task" --subject "<subject>" --body "<body>"
   ```
4. For `/locutus broadcast`:
   ```bash
   locutus broadcast --tags "${tags:-$LOCUTUS_PROJECT}" --subject "<subject>" --body "<body>"
   ```
5. For `/locutus who`:
   ```bash
   locutus who [filter]
   ```
6. For `/locutus tag`:
   ```bash
   locutus tag <add|remove|set> <tags>
   ```
7. For `/locutus unregister` (or `/locutus close`):
   ```bash
   locutus close "<name>"
   ```

