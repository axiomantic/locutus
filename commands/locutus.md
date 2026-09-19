---
name: locutus
description: "Locutus: Zero-Glue Redis Inter-Assistant command dispatcher. Subcommands: open, send, broadcast, request, enqueue, work, status, lock, unlock, pub, sub, who, tag, unregister/close."
---

# /locutus Command Dispatch

Dispatches inter-agent communication operations over the Redis Locutus bus.

## Subcommands

| Subcommand | Description | Example |
| :--- | :--- | :--- |
| `/locutus open [name] [tags]` | Registers identity, drains backlog, prints banner, and arms `BRPOP` listener | `/locutus open coder-1 "backend,qa"` |
| `/locutus send <to> <subject> <body>` | Sends direct task/query (O2O) with structured JSON envelope | `/locutus send worker-2 "Run tests" "pytest tests/"` |
| `/locutus broadcast [tags] <subject> <body>` | Multicasts with AND-filter matching. Defaults to current project | `/locutus broadcast "qa" "Deploy ready" "Please verify"` |
| `/locutus request <to> <subject> <body>` | Synchronous RPC request blocking until response is received | `/locutus request solver "Solve" "2+2"` |
| `/locutus enqueue <queue> <subject> <body>` | Pushes task to competing-consumers worker queue | `/locutus enqueue jobs "Build" "make -j8"` |
| `/locutus work <queue> [timeout]` | Pops task from competing-consumers worker queue | `/locutus work jobs 30` |
| `/locutus status <state> [activity]` | Updates agent operational state and activity text | `/locutus status busy "Running compiler"` |
| `/locutus lock <name> [ttl]` | Acquires atomic distributed mutex lease | `/locutus lock deploy_mutex 30` |
| `/locutus unlock <name>` | Releases atomic distributed mutex lease | `/locutus unlock deploy_mutex` |
| `/locutus pub <channel> <msg>` | Ephemeral real-time broadcast to channel subscribers | `/locutus pub alerts "Release v1.2 live"` |
| `/locutus sub <channel> [timeout]` | Listens for real-time pub/sub messages on channel | `/locutus sub alerts 10` |
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
5. For `/locutus request`:
   ```bash
   locutus request --to "<to>" --subject "<subject>" --body "<body>"
   ```
6. For `/locutus enqueue` and `/locutus work`:
   ```bash
   locutus enqueue "<queue>" --subject "<subject>" --body "<body>"
   locutus work "<queue>" 60
   ```
7. For `/locutus status`:
   ```bash
   locutus status "<state>" "<activity>"
   ```
8. For `/locutus lock` and `/locutus unlock`:
   ```bash
   locutus lock "<name>" [ttl]
   locutus unlock "<name>"
   ```
9. For `/locutus pub` and `/locutus sub`:
   ```bash
   locutus pub "<channel>" "<msg>"
   locutus sub "<channel>" [timeout]
   ```
10. For `/locutus who`:
   ```bash
   locutus who [filter]
   ```
11. For `/locutus tag`:
   ```bash
   locutus tag <add|remove|set> <tags>
   ```
12. For `/locutus unregister` (or `/locutus close`):
   ```bash
   locutus close "<name>"
   ```


