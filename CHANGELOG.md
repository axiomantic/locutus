# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2] - 2026-09-19

### Added
- **`locutus reply` First-Class Command**: Native CLI subcommand for replying directly to messages (`locutus reply --to <sender> --subject <subj> --body <body> [--reply-to <id>]`), automatically tagging the message with `type = reply`.
- **Atomic Listener Piggybacking (`--listen` / `-l`)**: Added `--listen` and `--listen-timeout` flags to `locutus send` and `locutus reply`. When enabled, Locutus delivers the outbound message, logs status to `stderr`, and seamlessly transitions the same running process into blocking wait on the agent's inbox. This prevents coding assistants from dropping background listeners during multi-turn work.
- **Singleton Listener Invariant & Anti-Stacking Guard**: Added strict singleton listener enforcement via Redis `${prefix}listener:${agent}` with process PID and hostname tracking. If `--listen` is called while another listener is already active for that agent, Locutus delivers the message and skips listening (exits 0) to prevent stacked background processes and Redis `BRPOP` competing-consumer queue splitting. Standalone `locutus listen` fails fast with code 1 unless `--force` / `-f` is specified. Stale locks from terminated processes on the same host are detected and self-healed instantly via `kill(pid, 0)`.
- **Expanded Skill Discovery & Distributed File Locking Guidance**: Enhanced the `locutus` skill frontmatter with explicit trigger phrases (`lock file`, `mutex lock`, `prevent concurrent edits`, `work queue`, `coordinate with the other terminal/agent`) to enable automatic skill invocation during parallel editing and multi-terminal operations. Added concrete file locking protocols (`locutus lock file:schema.prisma 60` / `locutus unlock file:schema.prisma`) to Section 1 and Section 3.C.
- **Capability-Based Dual-Strategy Ear Architecture in Skills**: Updated `SKILL.md` to instruct assistants to self-select their listener strategy based on native runtime tool capabilities rather than assistant brand names:
  - **Strategy A (Dedicated Ear Subagent)**: For runtimes supporting asynchronous subagent-to-parent messaging.
  - **Strategy B (Atomic Piggybacked Re-Arm)**: For single-agent / linear shell runtimes using `--listen`.

### Fixed
- **Conditional Listener Ownership Deletion on Exit**: Updated `doListen`'s cleanup block to verify that `locutus:listener:<name>` in Redis matches the terminating process's PID and hostname before deleting it. Prevents preempted or replaced listeners from having their locks wiped by an earlier process exiting.
- **Worker Heartbeat Starvation Prevention in `locutus work`**: Added automatic heartbeat and `active_agents` renewal during `doWork` chunked polling timeouts when an agent identity is resolved. Prevents idle worker processes waiting on task queues from expiring and being pruned from `locutus who`.
- **Directory Consistency in `status.lua`**: Added `SADD active_agents <name>` to `scripts/status.lua` to ensure that setting state or activity restores pruned agents to directory listings.
- **Cryptographic Error Handling**: Added null pointer check on OpenSSL `HMAC()` return value in `computeHmacSha256` to raise explicit `ValueError` on calculation failure.

## [0.1.1] - 2026-09-19

### Added
- **Silent Indefinite Blocking Listener**: `locutus listen` now defaults to indefinite blocking wait (`listenTimeout = 0`) with 60-second chunked internal polling and silent Redis heartbeat renewal (`SET heartbeat:<name> 1 EX 150`). Receivers now stay continuously registered in `locutus who` without exiting to the OS shell or waking the assistant.
- **Rule of Silence for Zero Token Churn**: Commands that timeout (`listen`, `work`, and `sub`) now return returncode `0` with 0 bytes on stdout (`""`), permanently eliminating token waste and death-by-a-thousand-tokencuts in AI context windows.
- **Indefinite Worker Queue**: `locutus work <queue>` now defaults to indefinite wait when no timeout is supplied, allowing worker pools to sit silently on Redis queues with zero token overhead.

### Changed
- **Continuous Ear Invariant in Skills**: Updated canonical `SKILL.md` (and synchronized mirrors) to strictly forbid wrapping shell loops (`while true; do ... done`). Assistants now launch bare `locutus listen` directly in the background and re-arm immediately only upon receipt of an authenticated message.
- **Ephemeral Pub/Sub Silence**: `locutus sub` now outputs 0 bytes on timeout instead of printing `(nil)`.

## [0.1.0] - 2026-09-19

### Added
- **Pure-Nim Daemonless Client**: Single compiled binary built with Nim standard library (`std/net`, `std/openssl`), utilizing an embedded pure-Nim RESP socket client with zero external runtime dependencies.
- **Out-of-Band Cryptographic Security**:
  - HMAC-SHA256 signature verification over all inter-agent messages, acting as an air-gap prompt-injection firewall to drop forged and tampered packets.
  - In-memory AES-256-CBC envelope encryption via native OpenSSL EVP C-bindings (`EVP_aes_256_cbc`, PBKDF2 SHA-256 with 10,000 iterations) with zero temporary files on disk.
  - Zero-friction key storage at `~/.config/locutus/secret` (`0600` permissions) with auto-generated 256-bit entropy.
- **Peer Discovery & Directory Service (`locutus who`)**:
  - Real-time active agent registry backed by Redis hashes and sets.
  - Automatic on-query dead-agent pruning for expired heartbeats.
  - Cluster-wide discovery flags (`-a`, `--all`, `*`) and structured JSON output (`--json`, `-j`).
- **Core Messaging Protocols**:
  - Direct point-to-point (O2O) task, query, reply, and status delivery (`locutus send`).
  - Multicast (O2M) messaging with multi-tag filtering (`locutus broadcast`).
  - Keyspace isolation via `LOCUTUS_PROJECT` and `{...}` hash tags for Redis Cluster compatibility.
  - Offline message queuing and ordered inbox backlog recovery (`locutus drain`).
- **Distributed Coordination Primitives**:
  - Synchronous RPC (`locutus request`) with blocking timeout and raw payload piping (`--raw`).
  - Competing-consumers work queues (`locutus enqueue` and `locutus work`) for parallel worker teams.
  - Distributed mutual exclusion locks (`locutus lock` and `locutus unlock`) with TTL lease hygiene.
  - Ephemeral real-time streaming (`locutus pub` and `locutus sub`).
  - Live agent operational state and activity tracking (`locutus status`).
- **Host & Multi-Terminal Isolation**:
  - Multi-tiered agent identity resolution prioritizing CLI arguments, `LOCUTUS_AGENT_NAME` process environment variables, and workspace `.locutus.agent` files.
  - Automatic listener auto-registration into live directory sets.
- **Universal Assistant Integration**:
  - Canonical agent skill specification in `skills/locutus/SKILL.md` compatible with Claude Code, Antigravity, Cursor, OpenCode, Codex, and Hermes via `npx skills` and `skilz`.
  - Zero-CPU, zero-token background listener discipline via Redis `BRPOP`.
- **Cascading Configuration System**:
  - Configuration hierarchy: CLI Flags -> Env Vars -> Workspace Config (`.locutus.toml`) -> User Config (`~/.config/locutus/locutus.toml`) -> Defaults.
  - Multiple environment profiles (`dev`, `staging`, `prod`).
  - Configuration inspection and initialization CLI (`locutus config show|get|path|init`).
- **Cross-Platform Distribution & Packaging**:
  - Official Homebrew formula in `axiomantic/homebrew-tap` (`brew install axiomantic/tap/locutus`).
  - Official Debian/Ubuntu APT repository deployed on GitHub Pages.
  - Windows Scoop manifest in `packaging/scoop/locutus.json`.
  - Universal 1-line installer and uninstaller scripts (`scripts/install.sh`, `scripts/install.ps1`).
  - Cross-compilation release pipeline generating standalone binaries and tarballs for Linux (amd64, arm64), macOS (Apple Silicon, Intel), and Windows (x64).
