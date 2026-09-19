# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
