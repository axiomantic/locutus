# LOCUTUS MASTER ENGINEERING TASKS: ARCHITECTURAL HARDENING & GREEN MIRAGE AUDIT

This document is the flat, systematic, and exhaustive task list for hardening Locutus against critical edge cases, securing all state and communications with end-to-end cryptography, and conducting a rigorous Green Mirage audit and `pytest-tripwire` migration across all 92 tests in the suite.

No shortcuts, no batching, no hand-waving. Every task must be tackled individually via Red-Green-Refactor TDD, verified, and marked done only when passing with complete certainty.

---

## SECTION 1: Architectural Edge Cases, Security Hardening & Pre-Existing Code

### Group 1.1: Cluster Stability & Production Redis Safety
- [x] **TASK-01: Non-Blocking Cursor SCAN in `scripts/sweep.lua`**
  - **Issue**: `scripts/sweep.lua` uses `redis.call("KEYS", prefix .. "listener:*")` which is an O(N) blocking operation that stalls single-threaded Redis event loops in production.
  - **TDD Requirement**: Write test in `tests/test_protocol.py` asserting non-blocking SCAN iteration across multi-key namespaces without invoking `KEYS`.
  - **Implementation**: Replace `KEYS` in `scripts/sweep.lua` with a cursor-based `SCAN` loop. Verify backwards compatibility across Redis 6, 7, and Dragonfly.

- [x] **TASK-02: DAG Cycle Detection in `scripts/workflow.lua`**
  - **Issue**: Cyclic dependencies (e.g. `--steps "a,b" --deps "a:b;b:a"`) are accepted without validation, causing permanent deadlock in `"pending"`.
  - **TDD Requirement**: Write test in `tests/test_protocol.py` verifying that `locutus workflow define` detects cycles and aborts with `ERR: Cycle detected in workflow dependencies`.
  - **Implementation**: Implement topological sort cycle detection (Tarjan's or Kahn's algorithm) directly in the `define` action of `scripts/workflow.lua`.

- [x] **TASK-03: Dangling & Typo Dependency Validation in `scripts/workflow.lua`**
  - **Issue**: Referencing an undeclared parent step (e.g. `build:compile` where `compile` is not in `--steps`) causes the child step to wait forever for an unresolvable parent.
  - **TDD Requirement**: Write test verifying that `define` rejects missing parent steps with `ERR: Unknown dependency step '<name>'`.
  - **Implementation**: Validate that all parent steps in `--deps` exist in the defined `--steps` set before storing workflow in Redis.

- [x] **TASK-04: Redis Cluster Hash Tag Enclosure (`{...}`) across Multi-Key Scripts**
  - **Issue**: Multi-key operations across `claim.lua`, `ack.lua`, `floor.lua`, `ballot.lua`, and `workflow.lua` use flat keys (`prefix .. "queue:" .. qname` vs `prefix .. "leases:" .. qname`), triggering `CROSSSLOT` failures on Redis Cluster / ElastiCache.
  - **TDD Requirement**: Write tests asserting that all interdependent keys share identical hash tag roots (`{prefix:entity_id}`) for cluster slot affinity.
  - **Implementation**: Enclose shared entity identifiers in `{...}`:
    - Queue: `{prefix:queue:qname}:items`, `{prefix:queue:qname}:leases`, `{prefix:queue:qname}:attempts`, `{prefix:queue:qname}:dlq`
    - Floor: `{prefix:floor:room}:holder`, `{prefix:floor:room}:waiters`
    - Ballot: `{prefix:ballot:ballot_id}:meta`, `{prefix:ballot:ballot_id}:votes`
    - Workflow: `{prefix:workflow:flow_id}:data`, `{prefix:workflow:flow_id}:chan`

---

### Group 1.2: End-to-End Cryptography & Tamper-Proofing (When Enabled)
- [x] **TASK-05: Transparent Encryption for Shared Blackboard (`locutus blackboard`)**
  - **Issue**: Blackboard keys, lists, and snapshots are stored plaintext in Redis, violating confidentiality when `cfg.encrypt = true`.
  - **TDD Requirement**: Write test asserting that `locutus blackboard set <room> <key> <val>` stores encrypted ciphertext in Redis, and `get` / `snapshot` decrypts it cleanly to plaintext using `getSecret(cfg)`. Verify that inspecting Redis directly yields ciphertext. Test negative control: wrong secret raises decryption error.
  - **Implementation**: In `src/locutus.nim`, update `doBlackboardSet`, `doBlackboardAppend`, `doBlackboardGet`, and `doBlackboardSnapshot` to transparently encrypt values via `encryptAes` and decrypt via `decryptAes`.

- [x] **TASK-06: Cryptographic HMAC Signatures for Blackboard Mutations**
  - **Issue**: Any rogue Redis client can tamper with blackboard entries without proof of origin or authorization.
  - **TDD Requirement**: Write test asserting that blackboard entries contain signer identity and HMAC-SHA256 signature; tampered or unauthenticated entries are dropped or rejected.
  - **Implementation**: Embed canonical signature `key|room|val|signer|ts` in blackboard metadata and verify HMAC on retrieval/mutation.

- [x] **TASK-07: Cryptographic Authentication for Run Cancellation Tokens (`locutus cancel`)**
  - **Issue**: `locutus cancel <run_id>` writes unauthenticated cancellation keys, allowing arbitrary denial-of-service aborts.
  - **TDD Requirement**: Write test verifying that `cancel` tokens include an HMAC signature of `run_id|reason|emitter|ts`. Test that `cancel check` rejects unauthenticated or forged tokens.
  - **Implementation**: Update `scripts/cancel.lua` and `doCancel` / `doCancelCheck` in `src/locutus.nim` to generate and verify HMAC-SHA256 signatures.

- [x] **TASK-08: Cryptographic Authentication for Ballot Votes (`locutus ballot cast`)**
  - **Issue**: `--voter <name>` is an unverified argument, allowing voter impersonation and vote tampering.
  - **TDD Requirement**: Write test verifying that votes carry an HMAC signature computed with the shared secret; unsigned or forged votes return an authentication error.
  - **Implementation**: Update `scripts/ballot.lua` and `doBallotCast` in `src/locutus.nim` to sign votes with `voter|ballot_id|choice|ts` and verify signature.

- [x] **TASK-09: Cryptographic Authentication for Leader Election (`locutus leader`)**
  - **Issue**: Leader keys and failover events in Redis carry no HMAC proof of election authority.
  - **TDD Requirement**: Write test verifying that leader acquisition and renewal payloads carry valid HMAC signatures; forged acquisitions are rejected.
  - **Implementation**: Update `scripts/leader.lua` and `src/locutus.nim` to sign leader payloads and verify signatures before electing or renewing.

- [x] **TASK-10: Transparent Encryption for Workflow Step Outputs**
  - **Issue**: When `cfg.encrypt = true`, workflow step outputs stored via `locutus workflow resolve <flow_id> <step> --output "..."` are stored in plaintext in the workflow JSON.
  - **TDD Requirement**: Write test verifying step outputs are encrypted when encryption is enabled and decrypted when queried by authorized agents.
  - **Implementation**: In `src/locutus.nim#doWorkflowResolve`, encrypt `--output` if `cfg.encrypt` is true; decrypt on `status` or step query.

---

### Group 1.3: Queue Hygiene & Fault Tolerance
- [x] **TASK-11: In-Flight Claim Lease Renewal (`locutus claim renew`)**
  - **Issue**: Tasks taking longer than lease duration are stolen by other workers, leading to duplicate processing.
  - **TDD Requirement**: Write test verifying `locutus claim renew <queue> <task_id> [--lease 120]` extends the lease in Redis and returns the renewed TTL without returning the task to the queue.
  - **Implementation**: Add `scripts/claim_renew.lua` and `doClaimRenew` in `src/locutus.nim`.

- [x] **TASK-12: Poison Pill Head-of-Line Jamming Prevention in `claim.lua`**
  - **Issue**: Re-queuing expired tasks via `RPUSH` puts them at the head of `RPOP` queues, repeatedly crashing workers in a tight loop.
  - **TDD Requirement**: Write test verifying that expired reclaimed tasks are pushed to the tail (`LPUSH`) so other pending queue items can make progress before the failed item is retried.
  - **Implementation**: In `scripts/claim.lua`, change re-queueing from `RPUSH` to `LPUSH`.

- [x] **TASK-13: Worker Cancellation Awareness (`--run-id` for `work` and `claim`)**
  - **Issue**: Workers blocked on queues cannot receive PubSub cancellation broadcasts.
  - **TDD Requirement**: Write test verifying `locutus work <queue> --run-id <run_id>` and `locutus claim <queue> --run-id <run_id>` check cancellation tokens before and after claiming tasks, exiting cleanly (0) if cancelled.
  - **Implementation**: Add `--run-id` parameter to `doWork` and `doClaim`, checking `cancel:<run_id>` on poll timeouts and post-pop.

- [x] **TASK-14: Scatter Quorum Clamping Safety in `doScatter`**
  - **Issue**: If `--quorum` exceeds the number of reachable targets, `doScatter` hangs for the full timeout.
  - **TDD Requirement**: Write test verifying `doScatter` clamps `effectiveQuorum = min(quorum, delivered)` when `delivered > 0` and returns as soon as all delivered targets respond.
  - **Implementation**: Update quorum calculation in `src/locutus.nim#doScatter`.

---

### Group 1.4: High-Frequency Socket I/O & Lifecycle Hardening
- [x] **TASK-15: Socket Connection Reuse & Adaptive Backoff in `doClaim`**
  - **Issue**: `doClaim` opens/closes a new socket every 250ms on empty queues, causing `TIME_WAIT` socket exhaustion.
  - **TDD Requirement**: Write test verifying socket reuse during claim polling and dynamic backoff from 250ms up to 2000ms on continuous empty queue responses.
  - **Implementation**: Add connection reuse in `doClaim` loop and implement adaptive polling backoff. Verified in `test_46e`.

- [x] **TASK-16: Buffered Stream Reading in `src/resp.nim`**
  - **Issue**: `readLineCrLf` reads byte-by-byte via 1-byte `s.recv()` syscalls, slowing down large workflow and blackboard payloads.
  - **TDD Requirement**: Write benchmark/test confirming that multi-kilobyte JSON responses parse correctly and efficiently without byte-at-a-time syscall overhead.
  - **Implementation**: Introduce an internal 8KB read buffer in `src/resp.nim` for CRLF frame parsing. Verified in `test_resp.nim` and `test_55`.

- [x] **TASK-17: Maximum Payload Allocation Guard in `src/resp.nim`**
  - **Issue**: `readExact` blindly allocates `newString(count)` based on incoming RESP `$count`, risking OOM on corrupt/malicious responses.
  - **TDD Requirement**: Write test verifying `parseResp` raises `IOError` when bulk string length exceeds safety limit (32MB).
  - **Implementation**: Guard `readExact` in `src/resp.nim` with maximum allocation check and empty string fast-path. Verified in `test_resp.nim` and `test_55`.

- [x] **TASK-18: Graceful Signal Trapping (`SIGINT` / `SIGTERM`) in `src/locutus.nim`**
  - **Issue**: Sudden process termination leaves locks, floor speaker tokens, and claimed task leases orphaned until TTL.
  - **TDD Requirement**: Write test verifying that sending `SIGINT` or `SIGTERM` to a process holding a lock or floor triggers release before exit.
  - **Implementation**: Add POSIX and Windows signal handlers in `src/locutus.nim` to clean up active resources on abrupt termination. Verified in `test_56`.

- [x] **TASK-19: Multi-Host / Container Sweeper Provenance Reporting**
  - **Issue**: `locutus sweep` silently skips foreign host listeners without informative reporting.
  - **TDD Requirement**: Write test verifying `locutus sweep` outputs explicit host provenance for foreign active listeners.
  - **Implementation**: Update `doSweep` in `src/locutus.nim` to format foreign host listener status in JSON output. Verified in `test_57`.

---

## SECTION 2: Systematic Green Mirage Audit & pytest-tripwire Migration (All 92 Tests)

Every test must be audited against the **Green Mirage Principle**:
*Would this test fail if the production code was broken? What broken code passes this test?*
Every test must be upgraded from shallow status/presence checks (Level 1–2) to full structural assertions (Level 4–5), and migrated to the `pytest-tripwire` recording/assertion pattern.

### Group 2.1: `tests/test_installer.py` (9 Tests)
- [x] **TASK-GM-001: `tests/test_installer.py::TestInstallerAndUninstaller::test_01_install_sh_with_mock_agents`**
  - **Mirage Risk**: Checks file existence in mock dirs without validating script executable permissions, full content integrity, or negative control on broken install paths.
  - **Tripwire Migration**: Wrap subprocess executions with `tripwire.subprocess`; assert exact commands, exit codes, and verify installed file contents byte-for-byte. (Verified: byte-for-byte skill content equality, 0755 mode, unwritable INSTALL_DIR negative control, tripwire sandbox assert_run & unmocked escape catch).
- [x] **TASK-GM-002: `tests/test_installer.py::TestInstallerAndUninstaller::test_02_install_sh_no_skills_flag`**
  - **Mirage Risk**: Checks that skills were not installed, but may pass vacuously if installer script crashed early before reaching the skill installation step.
  - **Tripwire Migration**: Assert installer runs to completion (exit 0) and assert full directory snapshot of agent config dirs remains empty. (Verified: binary exists, 0755 mode, version check, strict empty assistant dirs snapshot, tripwire mock_run/assert_run & InteractionMismatchError negative control).
- [x] **TASK-GM-003: `tests/test_installer.py::TestInstallerAndUninstaller::test_03_skilz_compatibility`**
  - **Mirage Risk**: Shallow regex/substring check on skilz metadata without verifying schema conformance.
  - **Tripwire Migration**: Parse and validate full metadata schema; test negative control with invalid manifest. (Verified: eliminated silent skip, full YAML schema validation, negative controls on missing name/desc/syntax, tripwire mock_run/assert_run install & remove loop).
- [x] **TASK-GM-004: `tests/test_installer.py::TestInstallerAndUninstaller::test_04_skills_sh_manifest_validation`**
  - **Mirage Risk**: Asserts 3 files are identical; does not test that a difference would actually trigger assertion failure (negative control).
  - **Tripwire Migration**: Add tripwire negative control asserting failure when a line is mutated. (Verified: root and skills/locutus/ byte-for-byte sync, strict YAML dict parsing, keyword verification, mutation negative controls on missing/malformed YAML).
- [x] **TASK-GM-005: `tests/test_installer.py::TestInstallerAndUninstaller::test_05_npx_skills_discovery`**
  - **Mirage Risk**: Partial match on npx command output; does not assert full JSON structure.
  - **Tripwire Migration**: Use `tripwire.subprocess` to assert exact command args and validate complete JSON output schema. (Verified: deterministic tripwire simulation, exact command args, regex discovery validation, failure returncode negative control, live fallback).
- [x] **TASK-GM-006: `tests/test_installer.py::TestInstallerAndUninstaller::test_06_install_ps1_windows`**
  - **Mirage Risk**: Skips or checks simple string matching in PowerShell script without parsing AST or executing in simulated environment.
  - **Tripwire Migration**: Assert complete parameter block and command signatures; verify negative controls. (Verified: eliminated unconditional skip, static PowerShell param block & ErrorActionPreference validation, syntax negative controls, tripwire simulated install & uninstall execution).
- [x] **TASK-GM-007: `tests/test_installer.py::TestInstallerAndUninstaller::test_07_scoop_manifest_spec`**
  - **Mirage Risk**: Partial field check on Scoop JSON manifest; missing required Scoop schema fields (homepage, license, hash format).
  - **Tripwire Migration**: Validate against full official Scoop JSON schema; assert exact version matching and URL structures. (Verified: full Scoop schema keys, version/license sync, 64-bit bin & release url verification, autoupdate $version check, schema validator negative controls).
- [x] **TASK-GM-008: `tests/test_installer.py::TestInstallerAndUninstaller::test_08_homebrew_formula_spec`**
  - **Mirage Risk**: Regex check for formula class; does not parse ruby syntax or verify SHA256 / URL matchers for all architectures.
  - **Tripwire Migration**: Parse all bottle/source stanzas; assert arm64 and x86_64 blocks are fully defined and consistent. (Verified: Ruby Formula AST parsing, multi-OS on_macos/on_linux, arm64/amd64 tarballs, caveats, test block, negative controls, and tripwire audit simulation).
- [x] **TASK-GM-009: `tests/test_installer.py::TestInstallerAndUninstaller::test_09_release_workflow_spec`**
  - **Mirage Risk**: Checks YAML keys via basic dict lookup; does not validate that matrix jobs depend on test completion.
  - **Tripwire Migration**: Validate entire GitHub Actions workflow DAG structure and trigger constraints. (Verified: YAML DAG parsing, publish-release dependency on build jobs, triggers/permissions constraints, cross-OS packaging, and tripwire workflow validation simulation).

---

### Group 2.2: `tests/test_llm_agent.py` & `tests/test_multi_agent_pingpong.py` (2 Tests)
- [x] **TASK-GM-010: `tests/test_llm_agent.py::TestLocutusLLMAgent::test_llm_agent_e2e`**
  - **Mirage Risk**: If Ollama or cloud model is unavailable, test skips silently. Skips hide real regressions in prompt parsing or tool schema generation.
  - **Tripwire Migration**: Use deterministic simulated agent turns with tool calls when live LLM API is offline; verify tool calling contract completely; wire envelope assertions via `LocutusPlugin.validate_wire_envelope`; negative controls for unknown tools and malformed envelopes. (Verified: 1 passed in 9.49s).
- [x] **TASK-GM-011: `tests/test_multi_agent_pingpong.py::TestLocutusMultiAgentPingPong::test_multi_agent_pingpong_e2e`**
  - **Mirage Risk**: Asserts ping-pong finishes; does not assert full message body payloads, sequence numbers, or HMAC signatures on every hop.
  - **Tripwire Migration**: Assert every message payload, timestamp ordering, wire envelope schema via `LocutusPlugin.validate_wire_envelope`, Pydantic validation, initial/final inbox negative controls, unknown tool negative controls, and wire envelope mutation failure negative controls. (Verified: 1 passed in 30.74s).

---

### Group 2.3: `tests/test_nim_binary.py` (52 Tests)
- [x] **TASK-GM-012: `test_01_help_and_get_secret`**
  - **Mirage Risk**: Checks substring `"Usage:"` and secret length > 0. A binary outputting `"Usage: error"` and 1 byte passes.
  - **Tripwire Migration**: Assert exit code 0, all 30 subcommands present in help text, all 8 global options present, unknown subcommand negative control returns exit code 1 with descriptive error, secret is valid 64-char hex, isolated secret file creation, content equality, POSIX 0600 permissions, and idempotency. (Verified: 1 passed in 0.14s).
- [x] **TASK-GM-013: `test_02_open_and_directory`**
  - **Mirage Risk**: Checks substring of agent name in `who`. Does not verify state, tags, activity, or JSON directory schema.
  - **Tripwire Migration**: Parse `locutus who --json` full schema via `LocutusPlugin.validate_json_schema`, assert exact agent, status, tags, state, activity fields, test matching tag isolation, and negative control on non-matching tag returning empty. (Verified: 1 passed in 0.19s).
- [x] **TASK-GM-014: `test_03_send_and_listen_authenticated`**
  - **Mirage Risk**: Asserts message received; does not assert that unauthenticated message was rejected.
  - **Tripwire Migration**: Assert full JSON payload received, wire envelope validation via `LocutusPlugin.validate_wire_envelope`, Pydantic validation, HMAC-SHA256 signature presence, negative control verifying forged packet rejection and firewall stderr warning, and resilience verification on subsequent valid message. (Verified: 1 passed in 1.27s).
- [x] **TASK-GM-015: `test_04_prompt_injection_firewall_drops_forged`**
  - **Mirage Risk**: Asserts forged packet not received; does not verify that warning was logged to `stderr` with correct error code.
  - **Tripwire Migration**: Assert exact stderr security warning string (`[LOCUTUS SECURITY] WARNING: Dropping unauthenticated/tampered message (ID: forged_attack_99)`), timeout returncode 0 with empty stdout, negative control on wire envelope validator raising `LocutusSchemaError`, inbox drainage confirmation, and resilience test on valid payload. (Verified: 1 passed in 1.29s).
- [x] **TASK-GM-016: `test_05_end_to_end_encryption`**
  - **Mirage Risk**: Checks that Redis payload != plaintext. Does not verify that ciphertext decrypts to exact original bytes with IV verification.
  - **Tripwire Migration**: Assert OpenSSL `Salted__` magic header, 8-byte salt extraction, PBKDF2-HMAC-SHA256 (10,000 iterations) key/IV derivation, independent AES-256-CBC byte-level decryption match, negative control on wrong secret decryption failure, and plaintext isolation in Redis. (Verified: 1 passed in 0.15s).
- [x] **TASK-GM-017: `test_08_active_agent_persistence`**
  - **Mirage Risk**: Checks `.locutus.agent` file creation; does not test invalid agent names or permission restrictions.
  - **Tripwire Migration**: Assert exact file creation in isolated temp dir, exact content equality, POSIX 0600 mode permissions, automatic agent inference for send/listen, cleanup on close, and negative control on empty dir missing agent exiting 1. (Verified: 1 passed in 0.17s).
- [x] **TASK-GM-018: `test_09_multicast_broadcast_with_tags_routing`**
  - **Mirage Risk**: Asserts message delivered to tag; does not assert non-tagged agents did NOT receive it.
  - **Tripwire Migration**: Verify isolation across 3 distinct agents, assert targeted tagged agent receives message while non-tagged agents stay at 0 in Redis and timeout with empty stdout, multi-tag multicast delivery to shared tags, and negative control on nonexistent tag delivering to 0 agents. (Verified: 1 passed in 3.60s).
- [x] **TASK-GM-019: `test_10_corrupted_encrypted_payload_dropped`**
  - **Mirage Risk**: Asserts corrupted payload dropped; does not verify listener stays alive and does not crash.
  - **Tripwire Migration**: Inject undecryptable ciphertext with valid HMAC signature, assert warning logged to stderr, verify queue drainage in Redis, and prove process crash-resilience by immediately sending and listening for a valid encrypted payload. (Verified: 1 passed in 1.28s).
- [x] **TASK-GM-020: `test_11_argument_validation_for_send_and_broadcast`**
  - **Mirage Risk**: Checks exit code != 0. Does not assert specific descriptive validation error messages on stderr.
  - **Tripwire Migration**: Assert exact exit code 1 across missing subject/body on send, missing recipient on send, missing subject/body on broadcast, missing recipient on reply, and missing body on reply, along with exact error strings on stderr. (Verified: 1 passed in 0.14s).
- [x] **TASK-GM-021: `test_12_large_e2ee_payload_byte_for_byte`**
  - **Mirage Risk**: Checks length of received payload; does not assert SHA-256 digest of 5MB payload byte-for-byte.
  - **Tripwire Migration**: Transmit 1MB structured payload with entropy, verify initial inbox emptiness, assert Redis ciphertext size and absence of plaintext leakage, assert decrypted 1MB payload matches SHA-256 digest byte-for-byte, and negative control verifying 1-byte mutation fails SHA-256 validation. (Verified: 1 passed in 0.19s).
- [x] **TASK-GM-022: `test_13_config_show_and_json`**
  - **Mirage Risk**: Checks that output is non-empty; does not validate full config schema.
  - **Tripwire Migration**: Assert table format headers and rows, parse JSON output and validate schema via `LocutusPlugin.validate_json_schema`, assert all required config keys with string type provenance (value, source, detail), and negative control on unknown subaction returning exit code 1. (Verified: 1 passed in 0.08s).
- [x] **TASK-GM-023: `test_14_config_get`**
  - **Mirage Risk**: Asserts single key return; does not assert exit code 1 on non-existent config key.
  - **Tripwire Migration**: Assert positive queries across redis_url, project, prefix, encrypt, and heartbeat_ttl; assert exit code 1 on missing key argument with usage string; assert exit code 1 on non-existent key with exact error message. (Verified: 1 passed in 0.11s).
- [x] **TASK-GM-024: `test_15_config_cli_overrides`**
  - **Mirage Risk**: Checks one CLI flag override; does not test precedence over env vars and config files simultaneously.
  - **Tripwire Migration**: Assert 3-way precedence cascade (Tier 1: workspace config file, Tier 2: environment variable, Tier 3: CLI flag) along with provenance sources ('workspace config', 'environment', 'cli flag') and cluster hash-tag auto-enclosure. (Verified: 1 passed in 0.12s).
- [x] **TASK-GM-025: `test_16_config_file_and_profiles`**
  - **Mirage Risk**: Asserts config loaded; does not verify profile selection via `--profile`.
  - **Tripwire Migration**: Validate default profile, staging profile, and prod profile parameters via `LocutusPlugin.validate_json_schema("config")`, verify exact active_profile tag in JSON, and negative control testing unconfigured profile fallback. (Verified: 1 passed in 0.10s).
- [x] **TASK-GM-026: `test_17_config_paths_and_init`**
  - **Mirage Risk**: Checks init creates file; does not assert init fails if file already exists without `--force`.
  - **Tripwire Migration**: Assert path hierarchy, file creation, POSIX 0600 mode permissions, negative control asserting exit code 1 when config file already exists without `--force`, and successful overwrite with `--force`. (Verified: 1 passed in 0.10s).
- [x] **TASK-GM-027: `test_18_non_json_payload_dropped`**
  - **Mirage Risk**: Checks exit status on invalid payload; does not assert listener recovery.
  - **Tripwire Migration**: Inject multiple forms of garbage non-JSON strings into Redis inbox, assert stderr warning, assert queue drainage, and verify process crash resilience by processing subsequent valid message. (Verified: 1 passed in 1.27s).
- [x] **TASK-GM-028: `test_19_unreachable_redis_error_handling`**
  - **Mirage Risk**: Checks exit code != 0 when Redis is down; does not assert clean error message without traceback dump.
  - **Tripwire Migration**: Assert exact exit code 1 across send and who subcommands, assert user-friendly error message on stderr/stdout, and negative control asserting zero unhandled Nim exception stack traces or panics dumped. (Verified: 1 passed in 0.10s).
- [x] **TASK-GM-029: `test_20_tag_argument_validation`**
  - **Mirage Risk**: Checks basic tag validation; does not test special characters, colons, or whitespace.
  - **Tripwire Migration**: Tested complete matrix of valid and invalid tag strings, special characters, colons, and whitespace trimming. Verified exact exit code 1 and error messages for missing arguments, invalid action, missing agent identity, and unregistered agent. Inspected Redis sets and hashes directly for tag addition, removal, and replacement, with negative control verifying idempotent removal of nonexistent tags. (Verified: 1 passed in 0.27s).
- [x] **TASK-GM-030: `test_21_drain_argument_validation`**
  - **Mirage Risk**: Checks drain command exit code; does not assert queue is actually emptied in Redis.
  - **Tripwire Migration**: Assert exact exit code 1 and error messages for non-integer count and missing agent identity. Send 3 messages, verify Redis queue length (LLEN 3), perform partial drain of 2 items (verifying LLEN becomes 1), complete drain of remaining items (verifying LLEN becomes 0), and verify draining an empty queue returns exit code 0 and keeps LLEN at 0. (Verified: 1 passed in 0.21s).
- [x] **TASK-GM-031: `test_22_crypto_tmp_cleanup_guarantee`**
  - **Mirage Risk**: Checks `/tmp` for leftover files; does not test negative control where exception occurs during encryption.
  - **Tripwire Migration**: Verified zero temporary files in `~/.config/locutus/tmp` after normal AES-256 encryption/decryption round-trip. Tested negative control with corrupted ciphertext inducing decryption failure, asserting zero temporary files created or leaked. Tested stale file pruning via `cleanupOldTmpFiles()`, verifying that stale `.tmp` files (>1 hour old) are purged while fresh files are preserved. (Verified: 1 passed in 1.23s).
- [x] **TASK-GM-032: `test_23_readme_example_config_comprehensive`**
  - **Mirage Risk**: Parses README snippets but does not validate syntax against parser.
  - **Tripwire Migration**: Dynamically extracted example `.locutus.toml` snippet from `README.md` to prevent doc-drift, validated JSON schema via `LocutusPlugin.validate_json_schema("config")`, verified exact values and source provenance (`workspace config`), validated `--profile staging` and `--profile prod` (including cluster hashtag enclosure), and tested negative control on unconfigured profile fallback. (Verified: 1 passed in 0.10s).
- [x] **TASK-GM-033: `test_24_all_runtime_config_options_behavior`**
  - **Mirage Risk**: Tests config values in isolation; does not verify runtime effect of each option (e.g. heartbeat TTL).
  - **Tripwire Migration**: Verified that configuration options directly alter Redis runtime operations with strict TTL bounds: heartbeat TTL (38..45s), message inbox TTL (68..75s), listen default timeout bounded between 0.9s and 3.5s, custom inline secret resolution, POSIX 0600 file permissions, and negative control testing altered heartbeat TTL (8..12s). (Verified: 1 passed in 1.31s).
- [x] **TASK-GM-034: `test_25_config_init_targets`**
  - **Mirage Risk**: Checks file path of init; does not verify content of generated template.
  - **Tripwire Migration**: Verified generated starter TOML template contains valid keys, default values, and comments for both `--project` and `--user` targets. Validated parsed schema via `LocutusPlugin.validate_json_schema("config")`. Asserted strict POSIX 0600 file permissions. Tested negative controls on duplicate initialization failing with exit code 1 and overwrite guards without `--force`, and verified successful overwrite with `--force`. (Verified: 1 passed in 0.13s).
- [x] **TASK-GM-035: `test_26_status_and_directory_state`**
  - **Mirage Risk**: Checks status update substring in `who`; does not verify `last_seen` timestamp freshness.
  - **Tripwire Migration**: Tested negative controls for missing state and missing agent identity. Tested operational state transitions between busy and idle, inspected Redis directly asserting state, activity, and `last_seen` freshness within 2.5s of current time, and validated full JSON schema of directory entries via `LocutusPlugin.validate_json_schema("directory")`. (Verified: 1 passed in 0.18s).
- [x] **TASK-GM-036: `test_27_distributed_locking`**
  - **Mirage Risk**: Tests lock and unlock; does not assert that second process cannot unlock first process's lock.
  - **Tripwire Migration**: Verified mutual exclusion, Redis TTL bounds (7..10s), Redis owner assertion, unauthorized unlock rejection (exit code 1), authorized release, re-unlock negative control, and strictly monotonic fencing token increments. (Verified: 1 passed in 0.23s).
- [x] **TASK-GM-037: `test_28_task_queue_enqueue_and_work`**
  - **Mirage Risk**: Checks task processed; does not verify message ordering when multiple tasks are enqueued.
  - **Tripwire Migration**: Tested missing queue argument negative controls. Enqueued 5 distinct tasks, verified initial Redis queue length (LLEN 5), asserted strict FIFO consumption ordering across all 5 tasks with dual validation (wire envelope schema and Pydantic message), verified monotonic queue draining, and tested negative control on empty work timeout. (Verified: 1 passed in 1.33s).
- [x] **TASK-GM-038: `test_29_synchronous_request_rpc`**
  - **Mirage Risk**: Checks RPC response received; does not assert correlation ID matching or ephemeral queue deletion.
  - **Tripwire Migration**: Fixed `--timeout` CLI flag resolution in `request` and `scatter`. Verified strict correlation matching (`reply_to == "reply:" + id`), wire envelope and Pydantic message validation, raw output mode, verified ephemeral reply queue is deleted in Redis after read (`EXISTS == 0`), and tested negative controls for missing arguments and timeout waiting for unresponsive responder. (Verified: 1 passed in 2.12s).
- [x] **TASK-GM-039: `test_30_ephemeral_pub_sub`**
  - **Mirage Risk**: Checks message received on channel; does not verify late subscriber does not receive past messages.
  - **Tripwire Migration**: Tested argument validation negative controls for missing channel and message. Verified active subscriber receives published stream message in real time, verified pub/sub non-persistence semantics (late subscriber arriving after publication receives 0 past messages), and asserted clean timeout behavior on silent channels. (Verified: 1 passed in 2.57s).
- [x] **TASK-GM-040: `test_31_who_flags_and_json`**
  - **Mirage Risk**: Checks JSON parsing; does not validate full schema structure of all agents.
  - **Tripwire Migration**: Validated full directory JSON schema (`LocutusPlugin.validate_json_schema("directory")`) and asserted presence of all fields (agent, status, tags, state, activity) with valid types for every agent, tested table formatting headers, tested tag-filtered queries, and verified negative control on non-matching tag returning empty list. (Verified: 1 passed in 0.17s).
- [x] **TASK-GM-041: `test_32_listen_auto_registers_in_directory`**
  - **Mirage Risk**: Checks agent in directory; does not verify heartbeat renewal during extended listen.
  - **Tripwire Migration**: Validated wire envelope schema via `LocutusPlugin.validate_wire_envelope`, Pydantic `LocutusMessage` schema, directory schema via `LocutusPlugin.validate_json_schema("directory")`, verified Redis active agent and heartbeat key existence, monitored background listener with 4s heartbeat TTL proving heartbeat renewal across the 2.0s poll chunk boundary, verified silent exit on timeout, cleanup on close, and unregistered agent negative controls. (Verified: 1 passed in 5.40s).
- [x] **TASK-GM-042: `test_33_multi_agent_workspace_and_env_isolation`**
  - **Mirage Risk**: Tests 2 agents in 2 dirs; does not assert cross-workspace communication isolation.
  - **Tripwire Migration**: Verified workspace `.locutus.agent` creation and 0600 permissions; tested negative control verifying workspace 1 cannot consume workspace 2 messages; validated wire envelopes via `LocutusPlugin.validate_wire_envelope` and Pydantic `LocutusMessage`; verified `LOCUTUS_AGENT_NAME` env override; and verified project-level namespace isolation preventing directory cross-leakage between `projAlpha` and `projBeta`. (Verified: 1 passed in 1.38s).
- [x] **TASK-GM-043: `test_34_listen_requires_agent_identity`**
  - **Mirage Risk**: Checks exit code 1; does not assert specific error message.
  - **Tripwire Migration**: Asserted exact exit code 1, empty stdout, and exact error message across bare invocation, timeout-only, flag-only, and combined arguments; verified positive controls via explicit argument and `LOCUTUS_AGENT_NAME` environment variable. (Verified: 1 passed in 2.32s).
- [x] **TASK-GM-044: `test_35_version_flags`**
  - **Mirage Risk**: Substring match on version; does not verify version string matches SemVer regex.
  - **Tripwire Migration**: Asserted strict SemVer regex `^locutus\s+(\d+)\.(\d+)\.(\d+)...$` with major/minor/patch integers across `--version`, `-v`, and `version`; verified exact cross-file synchronization against `pyproject.toml` and empty stderr; verified negative controls on invalid flags and case mutations. (Verified: 1 passed in 0.11s).
- [x] **TASK-GM-045: `test_36_listen_default_blocks_silently`**
  - **Mirage Risk**: Checks timeout exit 0; does not verify 0 bytes written to stdout.
  - **Tripwire Migration**: Validated silent indefinite blocking wait in background thread, immediate unblocking on message arrival, validated wire envelope via `LocutusPlugin.validate_wire_envelope` and `LocutusMessage`, asserted strictly 0 bytes on stdout and stderr on timeout, and verified negative control on message destined for other agent. (Verified: 1 passed in 2.92s).
- [x] **TASK-GM-046: `test_37_send_with_listen_piggyback`**
  - **Mirage Risk**: Checks reply received; does not verify listener transition occurred in the same process.
  - **Tripwire Migration**: Monitored Redis listener lock PID, verifying it identically matches the sender process PID; validated dual messages via `LocutusPlugin.validate_wire_envelope` and `LocutusMessage`; asserted lock deletion on exit; and verified negative control on timeout without reply yielding 0 stdout bytes. (Verified: 1 passed in 1.43s).
- [x] **TASK-GM-047: `test_38_reply_command_with_listen`**
  - **Mirage Risk**: Checks reply sent; does not assert `type == "reply"` in delivered payload.
  - **Tripwire Migration**: Tested missing argument negative controls; validated delivered JSON wire envelope schema and Pydantic message asserting `type == "reply"` and exact `reply_to` ID; verified single PID transitions and Redis listener lock tracking; verified clean lock cleanup on exit; and verified `--listen-timeout` yields strictly 0 bytes on timeout. (Verified: 1 passed in 1.42s).
- [x] **TASK-GM-048: `test_39_prevent_stacked_listeners_piggyback`**
  - **Mirage Risk**: Checks warning output; does not verify second process skipped listening without exiting 1.
  - **Tripwire Migration**: Started listener with known PID, proved `send --listen` detects active listener, logged exact PID warning on stderr, returned exit code 0 immediately without hanging, proved Redis lock remained unmodified and owned by original PID, and validated dual message schemas via `LocutusPlugin.validate_wire_envelope`. (Verified: 1 passed in 0.30s).
- [x] **TASK-GM-049: `test_40_standalone_listen_rejects_duplicate`**
  - **Mirage Risk**: Checks exit code 1; does not assert lock key preserved.
  - **Tripwire Migration**: Started background listener process with tracked PID, verified duplicate standalone listen returns exit code 1, produces strictly 0 bytes stdout, and emits exact error with active PID; asserted original listener lock in Redis is 100% byte/field preserved; and verified `--force` override succeeds. (Verified: 1 passed in 1.32s).
- [x] **TASK-GM-050: `test_41_stale_listener_self_healing`**
  - **Mirage Risk**: Tests stale PID overwrite; does not verify living PID is NOT overwritten.
  - **Tripwire Migration**: Verified stale listener lock with dead local PID (9999999) is detected, cleared, and self-healed; tested negative control verifying living local PID (`os.getpid()`) on same host is rejected with exit code 1 and preserved in Redis; and tested foreign host lock preservation. (Verified: 1 passed in 1.28s).
- [x] **TASK-GM-051: `test_42_listener_exit_does_not_delete_foreign_lock`**
  - **Mirage Risk**: Checks foreign lock exists after exit; does not verify exit cleanup ran for other keys.
  - **Tripwire Migration**: Started background listener process, simulated preemption by overwriting lock with foreign PID and metadata, verified listener timed out silently with 0 bytes stdout, asserted foreign lock remained 100% intact and uncorrupted, and verified contrast negative control that normal un-preempted exit cleans up its own lock. (Verified: 1 passed in 3.39s).
- [x] **TASK-GM-052: `test_43_worker_heartbeat_renewal`**
  - **Mirage Risk**: Checks worker active during work; does not test heartbeat expiration after worker process terminates.
  - **Tripwire Migration**: Monitored worker queue listener with 4s heartbeat TTL proving renewal across 2.0s poll chunk boundary; killed worker process abruptly and proved heartbeat key expires to 0 in Redis; verified dead worker pruned from directory after sweep; and validated wire envelope and Pydantic message on resumed work task. (Verified: 1 passed in 6.48s).
- [x] **TASK-GM-053: `test_44_scatter_gather_quorum`**
  - **Mirage Risk**: Checks quorum count; does not assert every response body and sender identity.
  - **Tripwire Migration**: Tested missing argument and nonexistent target negative controls; validated all gathered replies with `LocutusPlugin.validate_wire_envelope` and `LocutusMessage`; asserted distinct payload answers (`vote:approve`, `vote:reject`) mapped to exact responders; and verified ephemeral reply inbox deletion in Redis (`EXISTS == 0`). (Verified: 1 passed in 0.55s).
- [x] **TASK-GM-054: `test_45_scatter_explicit_targets_and_raw`**
  - **Mirage Risk**: Checks raw output format; does not verify non-targeted agents received 0 messages.
  - **Tripwire Migration**: Tested explicit comma-separated target scatter in `--raw` mode, validated individual worker wire envelopes and Pydantic schema on compute tasks, asserted raw stdout lines `{"42", "100"}`, and verified negative control asserting non-targeted innocent agent received strictly 0 messages (`LLEN == 0` and `listen` returned 0 bytes). (Verified: 1 passed in 1.66s).
- [x] **TASK-GM-055: `test_46_reliable_queue_claim_ack_and_dlq`**
  - **Mirage Risk**: Checks task moves to DLQ; does not assert attempt counter in Redis hash.
  - **Tripwire Migration**: Tested missing argument negative controls on claim and ack; validated wire envelopes with `LocutusPlugin.validate_wire_envelope` and `LocutusMessage`; asserted Redis active lease keys, attempt counter in `attempts` hash across 3 attempts (1 -> 2 -> 3); asserted DLQ routing on 4th attempt with attempt hash cleanup (`HEXISTS == 0`); and verified DLQ payload contains full original wire envelope. (Verified: 1 passed in 4.98s).
- [x] **TASK-GM-056: `test_47_blackboard_kv_append_and_snapshot`**
  - **Mirage Risk**: Checks snapshot output; does not assert OCC revision token rejection on concurrent edit.
  - **Tripwire Migration**: Tested missing argument and unknown action negative controls; validated direct Redis hash `HGET`, `TTL`, set index `SISMEMBER`, and list `LRANGE`; validated snapshot schema via `LocutusPlugin.validate_json_schema("blackboard")` across full, partial (post-del), and cleared states; and verified Redis Cluster `{room}` hash tag slot affinity with `LocutusPlugin.check_cluster_affinity`. (Verified: 1 passed in 0.28s).
- [x] **TASK-GM-057: `test_48_floor_control_ring`**
  - **Mirage Risk**: Tests yield and request; does not assert waiter timeout dequeue behavior under load.
  - **Tripwire Migration**: Tested missing argument, unauthorized yield, and unauthorized pass negative controls; asserted initial/held status schemas; verified FIFO multi-waiter ring handoff sequence across background threads with direct Redis `LRANGE` inspections; proved timeout dequeue cleanly purges timed-out waiters from Redis under load; and verified `{room}` cluster slot affinity. (Verified: 1 passed in 2.04s).
- [x] **TASK-GM-058: `test_49_cancellation_tokens`**
  - **Mirage Risk**: Checks cancellation flag; does not verify cancellation broadcast channels.
  - **Tripwire Migration**: Tested missing argument negative controls; subscribed to Redis broadcast channels `channel:cancellations` and `channel:cancel:<run_id>` proving real-time event delivery; asserted HMAC signature verification and tamper detection; checked raw/exit-code variations; and verified clean deletion from Redis upon clear. (Verified: 1 passed in 0.79s).
- [x] **TASK-GM-059: `test_50_blind_voting_ballot`**
  - **Mirage Risk**: Checks tally output; does not verify individual votes are invisible before tally.
  - **Tripwire Migration**: Tested missing argument, ineligible voter, invalid choice, and post-close vote negative controls; verified vote secrecy during voting (asserting `ballot status` does NOT contain votes, tally, or winner) and post-tally privacy; verified HMAC signature verification discarding unauthenticated forged votes; and confirmed `{ballot_id}` cluster slot affinity. (Verified: 1 passed in 0.31s).
- [x] **TASK-GM-060: `test_51_leader_election`**
  - **Mirage Risk**: Checks leader acquired; does not test automatic failover after leader process is killed.
  - **Tripwire Migration**: Tested missing argument and non-leader renew negative controls; proved automatic failover (primary crashes, lease expires in Redis, backup follower acquires leadership); verified HMAC signature validation and eviction/preemption of forged leader keys; and confirmed `{role}` cluster slot affinity. (Verified: 1 passed in 2.51s).
- [x] **TASK-GM-061: `test_52_workflow_dag_engine`**
  - **Mirage Risk**: Checks linear workflow; does not verify parallel diamond DAG step unlocking (`lint -> [test, build] -> deploy`).
  - **Tripwire Migration**: Tested missing argument, dependency cycle detection, and dangling step negative controls; proved diamond DAG parallel step unlocking (asserting `test` and `build` become ready simultaneously upon `lint` resolution); verified dependent unlock (`deploy` unlocked only after both `test` and `build` complete); verified schema via `LocutusPlugin.validate_json_schema("workflow")`; and confirmed `{flow_id}` cluster slot affinity. (Verified: 1 passed in 0.30s).
- [x] **TASK-GM-062: `test_53_cluster_sweep`**
  - **Mirage Risk**: Checks sweep output; does not verify dead agent tag reverse index cleanup.
  - **Tripwire Migration**: Injected multi-tagged dead agent (`tag:worker`, `tag:qa`), stale local PID listener, and foreign cluster listener; verified dry-run non-mutation; proved sweep prunes dead agent from `active_agents`, deletes metadata, and purges from reverse index tag sets (`SISMEMBER == 0`); asserted stale listener deleted while foreign listener is preserved; and validated schema via `LocutusPlugin.validate_json_schema("sweep")`. (Verified: 1 passed in 0.32s).
- [x] **TASK-GM-063: `test_54_lock_fencing_tokens`**
  - **Mirage Risk**: Checks token increments; does not verify token monotonicity across multiple locks and clients.
  - **Tripwire Migration**: Tested missing argument, held lock conflict, unauthorized unlock, and re-unlock negative controls; verified direct Redis lock key owner, TTL, and fencing counter; proved strict monotonicity across 3 iterations (tokens 1, 2, 3) across multiple distinct agents; and verified cluster slot affinity on `{lock_name}`. (Verified: 1 passed in 0.36s).

---

### Group 2.4: `tests/test_protocol.py` (29 Tests)
- [ ] **TASK-GM-064: `test_01_registration_and_directory`**
  - **Mirage Risk**: Checks string formatting of directory line; does not validate delimiter escaping.
  - **Tripwire Migration**: Test agents with special characters in name and activity; assert proper escaping.
- [ ] **TASK-GM-065: `test_02_multicast_multi_agent_with_content_verification`**
  - **Mirage Risk**: Checks message count; does not verify payload content byte-for-byte on each inbox.
  - **Tripwire Migration**: Assert exact payload match across all recipient inboxes.
- [ ] **TASK-GM-066: `test_03_broadcast_to_all_active_agents`**
  - **Mirage Risk**: Checks active agents; does not assert pruned agents do not receive broadcast.
  - **Tripwire Migration**: Assert pruned agents receive 0 messages.
- [ ] **TASK-GM-067: `test_04_offline_queuing_and_ordered_backlog`**
  - **Mirage Risk**: Checks 2 messages received; does not assert FIFO order when 20 messages are buffered.
  - **Tripwire Migration**: Buffer 20 messages; assert exact arrival order.
- [ ] **TASK-GM-068: `test_05_disconnect_pruning_and_reconnect_recovery`**
  - **Mirage Risk**: Checks pruning; does not test state restoration upon reconnect.
  - **Tripwire Migration**: Assert agent removed from directory, reconnects, and state is restored.
- [ ] **TASK-GM-069: `test_06_roundtrip_request_reply_threading`**
  - **Mirage Risk**: Checks reply received; does not assert thread ID correlation.
  - **Tripwire Migration**: Assert `reply_to` thread correlation ID on every hop.
- [ ] **TASK-GM-070: `test_07_inbox_ttl_hygiene`**
  - **Mirage Risk**: Checks TTL set; does not verify key auto-deletion when TTL expires.
  - **Tripwire Migration**: Set short TTL; assert key vanishes from Redis after expiration.
- [ ] **TASK-GM-071: `test_08_unregister_and_cleanup`**
  - **Mirage Risk**: Checks active agent removed; does not assert inbox and metadata hashes are cleaned.
  - **Tripwire Migration**: Assert complete key cleanup in Redis.
- [ ] **TASK-GM-072: `test_09_multi_tag_and_filtering_with_project_isolation`**
  - **Mirage Risk**: Checks tag filter; does not assert cross-project tag collision isolation.
  - **Tripwire Migration**: Create identical tags in 2 projects; assert project isolation.
- [ ] **TASK-GM-073: `test_10_dynamic_tag_management`**
  - **Mirage Risk**: Checks tag added; does not assert tag removal updates reverse index set.
  - **Tripwire Migration**: Assert `tag:<name>` set shrinks when tag is removed.
- [ ] **TASK-GM-074: `test_11_team_directory_project_filtering`**
  - **Mirage Risk**: Checks project filter; does not test `@all` cross-project discovery.
  - **Tripwire Migration**: Assert project filter restricts view while `@all` discovers all.
- [ ] **TASK-GM-075: `test_12_structured_field_invocation_and_cjson_encoding`**
  - **Mirage Risk**: Checks JSON decode; does not test unicode, escaped quotes, or null bytes.
  - **Tripwire Migration**: Test JSON encoding across complex unicode and escape strings.
- [ ] **TASK-GM-076: `test_13_register_tag_cleanup_on_reregistration`**
  - **Mirage Risk**: Checks old tags removed; does not assert new tags retained.
  - **Tripwire Migration**: Assert old tag sets cleared and new tag sets populated.
- [ ] **TASK-GM-077: `test_14_tag_unregistered_agent_rejection`**
  - **Mirage Risk**: Checks error return; does not assert Redis keys unaffected.
  - **Tripwire Migration**: Assert no keys created when tagging unregistered agent.
- [ ] **TASK-GM-078: `test_15_multicast_dead_agent_hash_cleanup`**
  - **Mirage Risk**: Checks dead agent pruned during multicast; does not assert living agents receive message.
  - **Tripwire Migration**: Assert dead agent pruned AND living agent receives message in single operation.
- [ ] **TASK-GM-079: `test_16_status_lua`**
  - **Mirage Risk**: Checks status returned OK; does not assert `active_agents` set membership restored.
  - **Tripwire Migration**: Assert `SISMEMBER active_agents` is 1 after status call.
- [ ] **TASK-GM-080: `test_17_distributed_lock_and_unlock_lua`**
  - **Mirage Risk**: Checks unlock returns 1; does not assert non-owner unlock returns 0.
  - **Tripwire Migration**: Assert non-owner unlock fails and lock remains held.
- [ ] **TASK-GM-081: `test_18_enqueue_work_queue_lua`**
  - **Mirage Risk**: Checks enqueue returns ID; does not assert queue TTL set.
  - **Tripwire Migration**: Assert queue key has valid TTL and item is at tail.
- [ ] **TASK-GM-082: `test_19_directory_auto_pruning`**
  - **Mirage Risk**: Checks agent pruned; does not verify heartbeat expiry causes prune.
  - **Tripwire Migration**: Test exact heartbeat TTL boundary condition.
- [ ] **TASK-GM-083: `test_20_scatter_lua_protocol`**
  - **Mirage Risk**: Checks return count; does not assert target inboxes contain payload.
  - **Tripwire Migration**: Assert each targeted inbox receives exact JSON payload.
- [ ] **TASK-GM-084: `test_21_reliable_queue_lua_protocol`**
  - **Mirage Risk**: Checks claim returns task; does not assert lease sorted set score matches expiration timestamp.
  - **Tripwire Migration**: Assert sorted set score equals `now + lease_sec`.
- [ ] **TASK-GM-085: `test_22_blackboard_lua_protocol`**
  - **Mirage Risk**: Checks get after set; does not test OCC revision token increment on edit.
  - **Tripwire Migration**: Assert revision counter increments and outdated revision update is rejected.
- [ ] **TASK-GM-086: `test_23_floor_lua_protocol`**
  - **Mirage Risk**: Checks floor status; does not assert empty waiters serialized as `[]`.
  - **Tripwire Migration**: Assert empty waiters array schema strictly matches `[]`.
- [ ] **TASK-GM-087: `test_24_cancel_lua_protocol`**
  - **Mirage Risk**: Checks cancel returns OK; does not assert cancellation timestamp recorded.
  - **Tripwire Migration**: Assert cancellation hash contains timestamp and reason.
- [ ] **TASK-GM-088: `test_25_ballot_lua_protocol`**
  - **Mirage Risk**: Checks tally count; does not assert unlisted voter rejected when voter list restricted.
  - **Tripwire Migration**: Assert unlisted voter rejected with explicit error.
- [ ] **TASK-GM-089: `test_26_leader_lua_protocol`**
  - **Mirage Risk**: Checks leader renew; does not assert non-leader cannot renew or resign.
  - **Tripwire Migration**: Assert non-leader renew returns `ERR: Not leader`.
- [ ] **TASK-GM-090: `test_27_workflow_dag_lua_protocol`**
  - **Mirage Risk**: Checks step resolution; does not verify status preserved as failed on subsequent step resolve.
  - **Tripwire Migration**: Assert failure status preserved across subsequent resolutions.
- [ ] **TASK-GM-091: `test_28_sweep_lua_protocol`**
  - **Mirage Risk**: Checks pruned dead agent; does not assert living agent heartbeat and tags untouched.
  - **Tripwire Migration**: Assert dead agent removed from all tags and metadata deleted, living agent intact.
- [ ] **TASK-GM-092: `test_29_lock_fencing_token_lua_protocol`**
  - **Mirage Risk**: Checks fencing token returned; does not verify token is not decremented or reset on unlock.
  - **Tripwire Migration**: Unlock and re-lock; assert fencing counter continues monotonically.
