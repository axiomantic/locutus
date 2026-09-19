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
- [ ] **TASK-11: In-Flight Claim Lease Renewal (`locutus claim renew`)**
  - **Issue**: Tasks taking longer than lease duration are stolen by other workers, leading to duplicate processing.
  - **TDD Requirement**: Write test verifying `locutus claim renew <queue> <task_id> [--lease 120]` extends the lease in Redis and returns the renewed TTL without returning the task to the queue.
  - **Implementation**: Add `scripts/claim_renew.lua` and `doClaimRenew` in `src/locutus.nim`.

- [ ] **TASK-12: Poison Pill Head-of-Line Jamming Prevention in `claim.lua`**
  - **Issue**: Re-queuing expired tasks via `RPUSH` puts them at the head of `RPOP` queues, repeatedly crashing workers in a tight loop.
  - **TDD Requirement**: Write test verifying that expired reclaimed tasks are pushed to the tail (`LPUSH`) so other pending queue items can make progress before the failed item is retried.
  - **Implementation**: In `scripts/claim.lua`, change re-queueing from `RPUSH` to `LPUSH`.

- [ ] **TASK-13: Worker Cancellation Awareness (`--run-id` for `work` and `claim`)**
  - **Issue**: Workers blocked on queues cannot receive PubSub cancellation broadcasts.
  - **TDD Requirement**: Write test verifying `locutus work <queue> --run-id <run_id>` and `locutus claim <queue> --run-id <run_id>` check cancellation tokens before and after claiming tasks, exiting cleanly (0) if cancelled.
  - **Implementation**: Add `--run-id` parameter to `doWork` and `doClaim`, checking `cancel:<run_id>` on poll timeouts and post-pop.

- [ ] **TASK-14: Scatter Quorum Clamping Safety in `doScatter`**
  - **Issue**: If `--quorum` exceeds the number of reachable targets, `doScatter` hangs for the full timeout.
  - **TDD Requirement**: Write test verifying `doScatter` clamps `effectiveQuorum = min(quorum, delivered)` when `delivered > 0` and returns as soon as all delivered targets respond.
  - **Implementation**: Update quorum calculation in `src/locutus.nim#doScatter`.

---

### Group 1.4: High-Frequency Socket I/O & Lifecycle Hardening
- [ ] **TASK-15: Socket Connection Reuse & Adaptive Backoff in `doClaim`**
  - **Issue**: `doClaim` opens/closes a new socket every 250ms on empty queues, causing `TIME_WAIT` socket exhaustion.
  - **TDD Requirement**: Write test verifying socket reuse during claim polling and dynamic backoff from 250ms up to 2000ms on continuous empty queue responses.
  - **Implementation**: Add connection reuse in `doClaim` loop and implement adaptive polling backoff.

- [ ] **TASK-16: Buffered Stream Reading in `src/resp.nim`**
  - **Issue**: `readLineCrLf` reads byte-by-byte via 1-byte `s.recv()` syscalls, slowing down large workflow and blackboard payloads.
  - **TDD Requirement**: Write benchmark/test confirming that multi-kilobyte JSON responses parse correctly and efficiently without byte-at-a-time syscall overhead.
  - **Implementation**: Introduce an internal 8KB read buffer in `src/resp.nim` for CRLF frame parsing.

- [ ] **TASK-17: Maximum Payload Allocation Guard in `src/resp.nim`**
  - **Issue**: `readExact` blindly allocates `newString(count)` based on incoming RESP `$count`, risking OOM on corrupt/malicious responses.
  - **TDD Requirement**: Write test verifying `parseResp` raises `IOError` when bulk string length exceeds safety limit (32MB).
  - **Implementation**: Guard `readExact` in `src/resp.nim` with maximum allocation check.

- [ ] **TASK-18: Graceful Signal Trapping (`SIGINT` / `SIGTERM`) in `src/locutus.nim`**
  - **Issue**: Sudden process termination leaves locks, floor speaker tokens, and claimed task leases orphaned until TTL.
  - **TDD Requirement**: Write test verifying that sending `SIGINT` or `SIGTERM` to a process holding a lock or floor triggers release before exit.
  - **Implementation**: Add POSIX and Windows signal handlers in `src/locutus.nim` to clean up active resources on abrupt termination.

- [ ] **TASK-19: Multi-Host / Container Sweeper Provenance Reporting**
  - **Issue**: `locutus sweep` silently skips foreign host listeners without informative reporting.
  - **TDD Requirement**: Write test verifying `locutus sweep` outputs explicit host provenance for foreign active listeners.
  - **Implementation**: Update `doSweep` in `src/locutus.nim` to format foreign host listener status in JSON output.

---

## SECTION 2: Systematic Green Mirage Audit & pytest-tripwire Migration (All 92 Tests)

Every test must be audited against the **Green Mirage Principle**:
*Would this test fail if the production code was broken? What broken code passes this test?*
Every test must be upgraded from shallow status/presence checks (Level 1–2) to full structural assertions (Level 4–5), and migrated to the `pytest-tripwire` recording/assertion pattern.

### Group 2.1: `tests/test_installer.py` (9 Tests)
- [ ] **TASK-GM-001: `tests/test_installer.py::TestInstallerAndUninstaller::test_01_install_sh_with_mock_agents`**
  - **Mirage Risk**: Checks file existence in mock dirs without validating script executable permissions, full content integrity, or negative control on broken install paths.
  - **Tripwire Migration**: Wrap subprocess executions with `tripwire.subprocess`; assert exact commands, exit codes, and verify installed file contents byte-for-byte.
- [ ] **TASK-GM-002: `tests/test_installer.py::TestInstallerAndUninstaller::test_02_install_sh_no_skills_flag`**
  - **Mirage Risk**: Checks that skills were not installed, but may pass vacuously if installer script crashed early before reaching the skill installation step.
  - **Tripwire Migration**: Assert installer runs to completion (exit 0) and assert full directory snapshot of agent config dirs remains empty.
- [ ] **TASK-GM-003: `tests/test_installer.py::TestInstallerAndUninstaller::test_03_skilz_compatibility`**
  - **Mirage Risk**: Shallow regex/substring check on skilz metadata without verifying schema conformance.
  - **Tripwire Migration**: Parse and validate full metadata schema; test negative control with invalid manifest.
- [ ] **TASK-GM-004: `tests/test_installer.py::TestInstallerAndUninstaller::test_04_skills_sh_manifest_validation`**
  - **Mirage Risk**: Asserts 3 files are identical; does not test that a difference would actually trigger assertion failure (negative control).
  - **Tripwire Migration**: Add tripwire negative control asserting failure when a line is mutated.
- [ ] **TASK-GM-005: `tests/test_installer.py::TestInstallerAndUninstaller::test_05_npx_skills_discovery`**
  - **Mirage Risk**: Partial match on npx command output; does not assert full JSON structure.
  - **Tripwire Migration**: Use `tripwire.subprocess` to assert exact command args and validate complete JSON output schema.
- [ ] **TASK-GM-006: `tests/test_installer.py::TestInstallerAndUninstaller::test_06_install_ps1_windows`**
  - **Mirage Risk**: Skips or checks simple string matching in PowerShell script without parsing AST or executing in simulated environment.
  - **Tripwire Migration**: Assert complete parameter block and command signatures; verify negative controls.
- [ ] **TASK-GM-007: `tests/test_installer.py::TestInstallerAndUninstaller::test_07_scoop_manifest_spec`**
  - **Mirage Risk**: Partial field check on Scoop JSON manifest; missing required Scoop schema fields (homepage, license, hash format).
  - **Tripwire Migration**: Validate against full official Scoop JSON schema; assert exact version matching and URL structures.
- [ ] **TASK-GM-008: `tests/test_installer.py::TestInstallerAndUninstaller::test_08_homebrew_formula_spec`**
  - **Mirage Risk**: Regex check for formula class; does not parse ruby syntax or verify SHA256 / URL matchers for all architectures.
  - **Tripwire Migration**: Parse all bottle/source stanzas; assert arm64 and x86_64 blocks are fully defined and consistent.
- [ ] **TASK-GM-009: `tests/test_installer.py::TestInstallerAndUninstaller::test_09_release_workflow_spec`**
  - **Mirage Risk**: Checks YAML keys via basic dict lookup; does not validate that matrix jobs depend on test completion.
  - **Tripwire Migration**: Validate entire GitHub Actions workflow DAG structure and trigger constraints.

---

### Group 2.2: `tests/test_llm_agent.py` & `tests/test_multi_agent_pingpong.py` (2 Tests)
- [ ] **TASK-GM-010: `tests/test_llm_agent.py::TestLocutusLLMAgent::test_llm_agent_e2e`**
  - **Mirage Risk**: If Ollama or cloud model is unavailable, test skips silently. Skips hide real regressions in prompt parsing or tool schema generation.
  - **Tripwire Migration**: Use `tripwire.http` to mock LLM completions with recorded deterministic interactions when live API is offline; verify tool calling contract completely.
- [ ] **TASK-GM-011: `tests/test_multi_agent_pingpong.py::TestLocutusMultiAgentPingPong::test_multi_agent_pingpong_e2e`**
  - **Mirage Risk**: Asserts ping-pong finishes; does not assert full message body payloads, sequence numbers, or HMAC signatures on every hop.
  - **Tripwire Migration**: Assert every message payload, timestamp ordering, and Redis key transitions using `tripwire.redis`.

---

### Group 2.3: `tests/test_nim_binary.py` (52 Tests)
- [ ] **TASK-GM-012: `test_01_help_and_get_secret`**
  - **Mirage Risk**: Checks substring `"Usage:"` and secret length > 0. A binary outputting `"Usage: error"` and 1 byte passes.
  - **Tripwire Migration**: Assert exit code 0, all subcommands present in help text, secret is valid 64-char hex, and permissions are `0600`.
- [ ] **TASK-GM-013: `test_02_open_and_directory`**
  - **Mirage Risk**: Checks substring of agent name in `who`. Does not verify state, tags, activity, or JSON directory schema.
  - **Tripwire Migration**: Parse `locutus who --json` full schema and assert exact fields.
- [ ] **TASK-GM-014: `test_03_send_and_listen_authenticated`**
  - **Mirage Risk**: Asserts message received; does not assert that unauthenticated message was rejected.
  - **Tripwire Migration**: Assert full JSON payload received; add negative control verifying forged packet rejection.
- [ ] **TASK-GM-015: `test_04_prompt_injection_firewall_drops_forged`**
  - **Mirage Risk**: Asserts forged packet not received; does not verify that warning was logged to `stderr` with correct error code.
  - **Tripwire Migration**: Assert exact stderr security warning string and timeout returncode.
- [ ] **TASK-GM-016: `test_05_end_to_end_encryption`**
  - **Mirage Risk**: Checks that Redis payload != plaintext. Does not verify that ciphertext decrypts to exact original bytes with IV verification.
  - **Tripwire Migration**: Assert AES ciphertext format (`aes256:iv:payload`); decrypt independently in Python and assert match.
- [ ] **TASK-GM-017: `test_08_active_agent_persistence`**
  - **Mirage Risk**: Checks `.locutus.agent` file creation; does not test invalid agent names or permission restrictions.
  - **Tripwire Migration**: Assert exact file content, mode permissions, and rejection of invalid characters.
- [ ] **TASK-GM-018: `test_09_multicast_broadcast_with_tags_routing`**
  - **Mirage Risk**: Asserts message delivered to tag; does not assert non-tagged agents did NOT receive it.
  - **Tripwire Migration**: Verify isolation: assert tagged agent receives message and non-tagged agent inbox remains empty.
- [ ] **TASK-GM-019: `test_10_corrupted_encrypted_payload_dropped`**
  - **Mirage Risk**: Asserts corrupted payload dropped; does not verify listener stays alive and does not crash.
  - **Tripwire Migration**: Assert listener remains running after dropping corrupted frame and processes subsequent valid frame.
- [ ] **TASK-GM-020: `test_11_argument_validation_for_send_and_broadcast`**
  - **Mirage Risk**: Checks exit code != 0. Does not assert specific descriptive validation error messages on stderr.
  - **Tripwire Migration**: Assert exact exit code 1 and exact argument validation error strings.
- [ ] **TASK-GM-021: `test_12_large_e2ee_payload_byte_for_byte`**
  - **Mirage Risk**: Checks length of received payload; does not assert SHA-256 digest of 5MB payload byte-for-byte.
  - **Tripwire Migration**: Compute and assert SHA-256 hash match on multi-megabyte payload.
- [ ] **TASK-GM-022: `test_13_config_show_and_json`**
  - **Mirage Risk**: Checks that output is non-empty; does not validate full config schema.
  - **Tripwire Migration**: Parse JSON output; assert all config keys and provenance types.
- [ ] **TASK-GM-023: `test_14_config_get`**
  - **Mirage Risk**: Asserts single key return; does not assert exit code 1 on non-existent config key.
  - **Tripwire Migration**: Test positive query match and negative control for unknown key.
- [ ] **TASK-GM-024: `test_15_config_cli_overrides`**
  - **Mirage Risk**: Checks one CLI flag override; does not test precedence over env vars and config files simultaneously.
  - **Tripwire Migration**: Assert 3-way precedence cascade (CLI flag beats ENV beats file).
- [ ] **TASK-GM-025: `test_16_config_file_and_profiles`**
  - **Mirage Risk**: Asserts config loaded; does not verify profile selection via `--profile`.
  - **Tripwire Migration**: Assert full config profile values match loaded profile.
- [ ] **TASK-GM-026: `test_17_config_paths_and_init`**
  - **Mirage Risk**: Checks init creates file; does not assert init fails if file already exists without `--force`.
  - **Tripwire Migration**: Assert file creation, permissions, and overwrite guard failure.
- [ ] **TASK-GM-027: `test_18_non_json_payload_dropped`**
  - **Mirage Risk**: Checks exit status on invalid payload; does not assert listener recovery.
  - **Tripwire Migration**: Verify that sending garbage string does not crash listener and listener continues listening.
- [ ] **TASK-GM-028: `test_19_unreachable_redis_error_handling`**
  - **Mirage Risk**: Checks exit code != 0 when Redis is down; does not assert clean error message without traceback dump.
  - **Tripwire Migration**: Assert user-friendly error message on stderr without unhandled Nim exception trace.
- [ ] **TASK-GM-029: `test_20_tag_argument_validation`**
  - **Mirage Risk**: Checks basic tag validation; does not test special characters, colons, or whitespace.
  - **Tripwire Migration**: Test complete matrix of valid and invalid tag strings.
- [ ] **TASK-GM-030: `test_21_drain_argument_validation`**
  - **Mirage Risk**: Checks drain command exit code; does not assert queue is actually emptied in Redis.
  - **Tripwire Migration**: Assert queue length becomes 0 and return code is 0.
- [ ] **TASK-GM-031: `test_22_crypto_tmp_cleanup_guarantee`**
  - **Mirage Risk**: Checks `/tmp` for leftover files; does not test negative control where exception occurs during encryption.
  - **Tripwire Migration**: Induce encryption failure and assert `/tmp` remains completely free of temporary files.
- [ ] **TASK-GM-032: `test_23_readme_example_config_comprehensive`**
  - **Mirage Risk**: Parses README snippets but does not validate syntax against parser.
  - **Tripwire Migration**: Execute all config examples from README through `config.nim` parser.
- [ ] **TASK-GM-033: `test_24_all_runtime_config_options_behavior`**
  - **Mirage Risk**: Tests config values in isolation; does not verify runtime effect of each option (e.g. heartbeat TTL).
  - **Tripwire Migration**: Test that changing `heartbeat_ttl` in config directly alters Redis `EX` TTL.
- [ ] **TASK-GM-034: `test_25_config_init_targets`**
  - **Mirage Risk**: Checks file path of init; does not verify content of generated template.
  - **Tripwire Migration**: Assert generated TOML/YAML contains valid default keys and comments.
- [ ] **TASK-GM-035: `test_26_status_and_directory_state`**
  - **Mirage Risk**: Checks status update substring in `who`; does not verify `last_seen` timestamp freshness.
  - **Tripwire Migration**: Assert `last_seen` is within 2 seconds of current time and state matches.
- [ ] **TASK-GM-036: `test_27_distributed_locking`**
  - **Mirage Risk**: Tests lock and unlock; does not assert that second process cannot unlock first process's lock.
  - **Tripwire Migration**: Assert lock exclusion and verify unauthorized unlock fails with exit code 1.
- [ ] **TASK-GM-037: `test_28_task_queue_enqueue_and_work`**
  - **Mirage Risk**: Checks task processed; does not verify message ordering when multiple tasks are enqueued.
  - **Tripwire Migration**: Enqueue 5 tasks; assert strict FIFO consumption ordering.
- [ ] **TASK-GM-038: `test_29_synchronous_request_rpc`**
  - **Mirage Risk**: Checks RPC response received; does not assert correlation ID matching or ephemeral queue deletion.
  - **Tripwire Migration**: Assert correlation `reply_to` matches and ephemeral reply queue is deleted after read.
- [ ] **TASK-GM-039: `test_30_ephemeral_pub_sub`**
  - **Mirage Risk**: Checks message received on channel; does not verify late subscriber does not receive past messages.
  - **Tripwire Migration**: Assert pub/sub non-persistence semantics: late subscribers receive 0 messages.
- [ ] **TASK-GM-040: `test_31_who_flags_and_json`**
  - **Mirage Risk**: Checks JSON parsing; does not validate full schema structure of all agents.
  - **Tripwire Migration**: Validate every agent object against JSON schema (name, alive, tags, state, activity).
- [ ] **TASK-GM-041: `test_32_listen_auto_registers_in_directory`**
  - **Mirage Risk**: Checks agent in directory; does not verify heartbeat renewal during extended listen.
  - **Tripwire Migration**: Monitor Redis heartbeat TTL over time, asserting TTL is refreshed.
- [ ] **TASK-GM-042: `test_33_multi_agent_workspace_and_env_isolation`**
  - **Mirage Risk**: Tests 2 agents in 2 dirs; does not assert cross-workspace communication isolation.
  - **Tripwire Migration**: Verify workspace A cannot read workspace B inbox when project namespace differs.
- [ ] **TASK-GM-043: `test_34_listen_requires_agent_identity`**
  - **Mirage Risk**: Checks exit code 1; does not assert specific error message.
  - **Tripwire Migration**: Assert exact error message stating agent identity is missing.
- [ ] **TASK-GM-044: `test_35_version_flags`**
  - **Mirage Risk**: Substring match on version; does not verify version string matches SemVer regex.
  - **Tripwire Migration**: Assert exact version string matches SemVer `^\d+\.\d+\.\d+$`.
- [ ] **TASK-GM-045: `test_36_listen_default_blocks_silently`**
  - **Mirage Risk**: Checks timeout exit 0; does not verify 0 bytes written to stdout.
  - **Tripwire Migration**: Assert exit code 0 AND stdout length == 0.
- [ ] **TASK-GM-046: `test_37_send_with_listen_piggyback`**
  - **Mirage Risk**: Checks reply received; does not verify listener transition occurred in the same process.
  - **Tripwire Migration**: Verify single PID handles both send and listening.
- [ ] **TASK-GM-047: `test_38_reply_command_with_listen`**
  - **Mirage Risk**: Checks reply sent; does not assert `type == "reply"` in delivered payload.
  - **Tripwire Migration**: Assert delivered JSON has `type: "reply"` and matches `reply_to` ID.
- [ ] **TASK-GM-048: `test_39_prevent_stacked_listeners_piggyback`**
  - **Mirage Risk**: Checks warning output; does not verify second process skipped listening without exiting 1.
  - **Tripwire Migration**: Assert exit code 0 and duplicate listener lock was not created.
- [ ] **TASK-GM-049: `test_40_standalone_listen_rejects_duplicate`**
  - **Mirage Risk**: Checks exit code 1; does not assert lock key preserved.
  - **Tripwire Migration**: Assert original listener lock is completely unmodified.
- [ ] **TASK-GM-050: `test_41_stale_listener_self_healing`**
  - **Mirage Risk**: Tests stale PID overwrite; does not verify living PID is NOT overwritten.
  - **Tripwire Migration**: Negative control: verify living PID on same host is rejected.
- [ ] **TASK-GM-051: `test_42_listener_exit_does_not_delete_foreign_lock`**
  - **Mirage Risk**: Checks foreign lock exists after exit; does not verify exit cleanup ran for other keys.
  - **Tripwire Migration**: Assert foreign lock preserved and local process state cleaned.
- [ ] **TASK-GM-052: `test_43_worker_heartbeat_renewal`**
  - **Mirage Risk**: Checks worker active during work; does not test heartbeat expiration after worker process terminates.
  - **Tripwire Migration**: Kill worker process and assert heartbeat expires and worker is pruned.
- [ ] **TASK-GM-053: `test_44_scatter_gather_quorum`**
  - **Mirage Risk**: Checks quorum count; does not assert every response body and sender identity.
  - **Tripwire Migration**: Parse array of replies; assert each sender and payload matches expected reply.
- [ ] **TASK-GM-054: `test_45_scatter_explicit_targets_and_raw`**
  - **Mirage Risk**: Checks raw output format; does not verify non-targeted agents received 0 messages.
  - **Tripwire Migration**: Verify target isolation: assert non-targeted agent received no scatter task.
- [ ] **TASK-GM-055: `test_46_reliable_queue_claim_ack_and_dlq`**
  - **Mirage Risk**: Checks task moves to DLQ; does not assert attempt counter in Redis hash.
  - **Tripwire Migration**: Verify attempt counter increments on each claim and DLQ contains full payload.
- [ ] **TASK-GM-056: `test_47_blackboard_kv_append_and_snapshot`**
  - **Mirage Risk**: Checks snapshot output; does not assert OCC revision token rejection on concurrent edit.
  - **Tripwire Migration**: Test OCC revision conflict rejection and verify full snapshot JSON schema.
- [ ] **TASK-GM-057: `test_48_floor_control_ring`**
  - **Mirage Risk**: Tests yield and request; does not assert waiter timeout dequeue behavior under load.
  - **Tripwire Migration**: Test timeout dequeue and assert speaker ring handoff sequence.
- [ ] **TASK-GM-058: `test_49_cancellation_tokens`**
  - **Mirage Risk**: Checks cancellation flag; does not verify cancellation broadcast channels.
  - **Tripwire Migration**: Subscribe to `channel:cancellations` and assert broadcast event received.
- [ ] **TASK-GM-059: `test_50_blind_voting_ballot`**
  - **Mirage Risk**: Checks tally output; does not verify individual votes are invisible before tally.
  - **Tripwire Migration**: Assert vote secrecy: query Redis before tally and assert choices cannot be inspected.
- [ ] **TASK-GM-060: `test_51_leader_election`**
  - **Mirage Risk**: Checks leader acquired; does not test automatic failover after leader process is killed.
  - **Tripwire Migration**: Kill leader process; assert follower acquires leadership upon lease expiry.
- [ ] **TASK-GM-061: `test_52_workflow_dag_engine`**
  - **Mirage Risk**: Checks linear workflow; does not verify parallel diamond DAG step unlocking (`lint -> [test, build] -> deploy`).
  - **Tripwire Migration**: Test diamond DAG: assert `test` and `build` become ready simultaneously upon `lint` completion.
- [ ] **TASK-GM-062: `test_53_cluster_sweep`**
  - **Mirage Risk**: Checks sweep output; does not verify dead agent tag reverse index cleanup.
  - **Tripwire Migration**: Verify `tag:<tag>` set membership is cleaned up on sweep.
- [ ] **TASK-GM-063: `test_54_lock_fencing_tokens`**
  - **Mirage Risk**: Checks token increments; does not verify token monotonicity across multiple locks and clients.
  - **Tripwire Migration**: Acquire lock across 3 iterations; assert tokens are strictly monotonic integers (N, N+1, N+2).

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
