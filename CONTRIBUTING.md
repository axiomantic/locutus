# Contributing to Locutus

We welcome contributions to Locutus! Whether you are optimizing Lua scripts, improving the native Nim binary, adding integrations for new coding assistants, or expanding test coverage, here is how to get started.

## Development Setup

### Prerequisites
- **Nim**: 2.0+ (`brew install nim` on macOS, `sudo apt install nim` on Linux, or `curl https://nim-lang.org/choosenim/init.sh -sSf | sh`)
- **Redis Server**: 6.2+ (`brew install redis` on macOS, `sudo apt install redis-server` on Linux, or Docker)
- **OpenSSL**: 1.1+ / 3.0+
- **Python**: 3.10+ (for integration tests and Pydantic validation)

### Quick Build & Test

```bash
# 1. Clone repository
git clone https://github.com/axiomantic/locutus.git
cd locutus

# 2. Build native Nim binary (1 second)
nim c -d:release -o:bin/locutus src/locutus.nim

# 3. Install Python dependencies for test runner
python3 -m venv .venv
source .venv/bin/activate
pip install -r <(echo "pydantic>=2.0")

# 4. Start local Redis server
brew services start redis  # or: docker run -d -p 6379:6379 redis:alpine

# 5. Run full test suite
python3 -m unittest discover tests
```

## Architectural Guidelines

1. **Zero Glue / Zero Background Daemons**:
   - Locutus core must remain zero-dependency. Do not add long-running background Python/Node daemon processes. All coordination runs over Redis primitives and the native `locutus` binary.

2. **Air-Gap Prompt Firewall**:
   - Never allow unverified or tampered data from Redis to reach assistant stdout. Signature verification must happen strictly before message deserialization.

3. **EVALSHA Caching**:
   - All Lua scripts in `scripts/` are embedded at compile-time into `src/locutus.nim` and cached using Redis `EVALSHA` with automatic `EVAL` fallback.

## Pull Request Process

1. Fork the repo and create a topic branch from `main`.
2. Ensure all 32 unit tests pass: `python3 -m unittest discover tests`.
3. If modifying `scripts/*.lua`, remember to recompile `bin/locutus` (`nim c -d:release -o:bin/locutus src/locutus.nim`).
4. Submit a Pull Request describing your changes, motivation, and test evidence.
