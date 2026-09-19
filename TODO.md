# Locutus Master Task & TO DO List

## Itemized Tasks Discussed & Final Status: 100% Complete

### 1. Configurable Prefix & Namespace Isolation
- [x] Support `LOCUTUS_REDIS_PREFIX` (defaulting to `locutus:`) with fallback to `A2A_REDIS_PREFIX` / `A2A_PREFIX`.
- [x] Prefix all keys (`${PREFIX}agent:<name>`, `${PREFIX}inbox:<name>`, `${PREFIX}heartbeat:<name>`, `${PREFIX}tag:<tag>`, `${PREFIX}active_agents`).
- [x] Update test suites to run against isolated test prefix (`locutus_test:`) so running tests never touches production keys.

### 2. Decouple Lua Scripts from Prompts and Test Code (Single Source of Truth)
- [x] Create standalone Lua scripts in `scripts/` to eliminate prompt token bloat and prevent bash quoting errors:
  - [x] `scripts/register.lua`: Atomic registration, tag indexing, and TTL heartbeat initialization.
  - [x] `scripts/send_o2o.lua`: Direct point-to-point inbox message queuing with TTL (dual-mode raw JSON or structured fields).
  - [x] `scripts/multicast.lua`: Multi-tag AND-filtering (`SINTER`), heartbeat pruning, and dual-mode parameter encoding.
  - [x] `scripts/tag.lua`: Dynamic add/remove/set tags without dropping inbox messages.
  - [x] `scripts/drain.lua`: Atomic batch RPOP for backlog draining.
  - [x] `scripts/directory.lua`: Roster listing with project/tag filtering.
  - [x] `scripts/unregister.lua`: Clean logout and set cleanup.
- [x] Load Lua scripts directly from disk in both Python test files and assistant shell commands (`cat "$LOCUTUS_SCRIPTS_DIR/<script>.lua"`).

### 3. Modern Data Validation in Python
- [x] Implement formal Pydantic model (`LocutusMessage` with alias `A2AMessage`) in `tests/schema.py`.
- [x] Enforce required envelope fields (`id`, `from`, `to`, `type`, `subject`, `body`, `timestamp`).
- [x] Validate strict ISO-8601 timestamp formats via Pydantic field validator.
- [x] Enforce protocol message types (`task`, `query`, `reply`, `status`).
- [x] Flexible pre-validators for tags coercion (lists, sets, comma-separated strings).

### 4. Protocol & Resilience Unit Tests (`tests/test_protocol.py`)
- [x] `test_01_registration_and_directory`: Registration, tag sets, heartbeat, and directory lookup.
- [x] `test_02_multicast_multi_agent_with_content_verification`: Tag fan-out with deep payload verification.
- [x] `test_03_broadcast_to_all_active_agents`: Global broadcast (`*`) to all active agents.
- [x] `test_04_offline_queuing_and_ordered_backlog`: Offline message queuing and FIFO backlog drain.
- [x] `test_05_disconnect_pruning_and_reconnect_recovery`: Heartbeat expiry, pruning during multicast, and reconnect recovery.
- [x] `test_06_roundtrip_request_reply_threading`: End-to-end request-reply correlation (`id` -> `reply_to`).
- [x] `test_07_inbox_ttl_hygiene`: Inbox expiration TTL enforcement to prevent RAM leaks.
- [x] `test_08_unregister_and_cleanup`: Graceful logout and keyspace cleanup.
- [x] `test_09_multi_tag_and_filtering_with_project_isolation`: Multi-project isolation via `SINTER` AND-filtering.
- [x] `test_10_dynamic_tag_management`: Dynamic add/remove/set tags without dropping queued messages.
- [x] `test_11_team_directory_project_filtering`: Filter team directory by project tag vs cluster-wide query.
- [x] `test_12_structured_field_invocation_and_cjson_encoding`: Structured field parameter invocation with server-side `cjson.encode`.
- [x] All 12/12 tests passing against live Redis.

### 5. Multi-Project Isolation & Tag-Based Team Discovery
- [x] Derive `$LOCUTUS_PROJECT` automatically from directory basename with AGENTS.md / .env overrides.
- [x] Enforce that all assistants register with their project tag.
- [x] Native Redis `SINTER` AND-filtering in `scripts/multicast.lua` so multicasting to `backend` within project `locutus` targets `locutus,backend` (both tags required).
- [x] Restrict cluster-wide broadcasts across all projects exclusively to `*` or `@all`.
- [x] Dynamic tagging (`scripts/tag.lua`) allowing agents to add/remove specialized tags on the fly.
- [x] Project filtering in `scripts/directory.lua`.
- [x] Print human-readable registration banner to the human operator upon registration in `SKILL.md`.

### 6. Prompt Optimization & Skill Decomposition (Progressive Disclosure)
- [x] Split architecture: core instructions in `SKILL.md`, wire format in `references/wire_spec.md`, scripts in `scripts/*.lua`.
- [x] Safe structured parameters and unquoted heredoc message templates in `SKILL.md` to avoid bash escaping issues.
- [x] Create operator slash command specifications in `commands/locutus.md` and `.claude/commands/locutus.md` (`/locutus open`, `/locutus send`, `/locutus broadcast`, `/locutus who`, `/locutus tag`, `/locutus close`).
- [x] Zero-token background listener discipline (`BRPOP ... 90` doubles as heartbeat refresher).
- [x] Reply loop prevention (replies must always be unicast O2O).

### 7. Locutus Rebranding & Spellbook Migration Compatibility
- [x] Renamed repository and code from `a2a` to `locutus`.
- [x] Preserved fallback aliases (`A2AMessage`, `A2A_*` env vars) to prepare for clean migration of `~/Development/spellbook/a2a`.

### 8. Semantic Content & Tool-Execution Single-Agent Test (`tests/test_ollama_agent.py`)
- [x] Updated `tests/test_ollama_agent.py` to use `LOCUTUS_*` env vars and `locutus:` keyspace.
- [x] Added explicit tool-calling system prompt instructions for `gemma4:e4b` so it invokes the `execute_bash` tool instead of simulating commands in text.
- [x] Verified agent registers, sends structured message via `send_o2o.lua`, and validates payload content with `LocutusMessage`.
- [x] Test passing 100% against live Ollama model.

### 9. Multi-Agent Ping-Pong Autonomous Integration Test (`tests/test_multi_agent_pingpong.py`)
- [x] Built end-to-end 2-agent conversation test with live Ollama model (`gemma4:e4b`):
  - [x] Agent A ("Alice", requester) in project `locutus` registers and sends a math task (`Compute 15 * 15`).
  - [x] Agent B ("Bob", worker, running autonomously via Ollama) drains inbox, computes `225`, and replies using `send_o2o.lua` with `reply_to: task_id`.
  - [x] Alice receives Bob's reply from her inbox.
  - [x] Validated reply using `LocutusMessage`, asserted `reply_to == task_id`, and verified body contains `225`.
- [x] Test passing 100% end-to-end.

### 10. Documentation & Final Polish
- [x] Updated `README.md` with complete Locutus architecture, project isolation, slash commands, Lua scripts, and test commands.
- [x] Synced `SKILL.md`, `scripts/`, `commands/`, and `references/` into `~/.gemini/config/skills/locutus/`.
- [x] Maintained `TODO.md` in workspace.
- [x] Git committed all changes cleanly.
