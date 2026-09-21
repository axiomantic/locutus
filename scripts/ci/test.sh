#!/usr/bin/env bash
set -euo pipefail

# scripts/ci/test.sh: Unified test runner for Locutus.
# Manages local Redis daemon if needed, and runs pytest.

REDIS_URL="${LOCUTUS_REDIS_URL:-redis://127.0.0.1:6379}"
export LOCUTUS_REDIS_URL="${REDIS_URL}"

echo "=== Checking Redis connectivity at ${LOCUTUS_REDIS_URL} ==="
REDIS_HOST=$(echo "${REDIS_URL}" | sed -E 's|redis://([^:/]+).*|\1|')
REDIS_PORT=$(echo "${REDIS_URL}" | sed -E 's|redis://[^:]+:([0-9]+).*|\1|')
if [ "${REDIS_PORT}" = "${REDIS_URL}" ] || [ -z "${REDIS_PORT}" ]; then
  REDIS_PORT="6379"
fi

# If targeting localhost/127.0.0.1 and ping fails, start daemonized redis-server if installed
if [ "${REDIS_HOST}" = "127.0.0.1" ] || [ "${REDIS_HOST}" = "localhost" ]; then
  if ! command -v redis-cli >/dev/null 2>&1 || ! redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" ping >/dev/null 2>&1; then
    if command -v redis-server >/dev/null 2>&1; then
      echo "Starting local redis-server daemon on port ${REDIS_PORT}..."
      redis-server --port "${REDIS_PORT}" --daemonize yes
      sleep 1
    fi
  fi
fi

if command -v redis-cli >/dev/null 2>&1; then
  if redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" ping; then
    echo "Redis connection verified."
  else
    echo "Warning: redis-cli ping failed for ${REDIS_HOST}:${REDIS_PORT}"
  fi
fi

# Locate Python executable
PY_CMD="python3"
if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "${VIRTUAL_ENV}/bin/python" ] && "${VIRTUAL_ENV}/bin/python" -c "" 2>/dev/null; then
  PY_CMD="${VIRTUAL_ENV}/bin/python"
elif [ -x ".venv/bin/python" ] && .venv/bin/python -c "" 2>/dev/null; then
  PY_CMD=".venv/bin/python"
elif command -v python >/dev/null 2>&1; then
  PY_CMD="python"
fi

echo "=== Running Locutus pytest suite with ${PY_CMD} ==="
"${PY_CMD}" -m pytest -v -m "not llm" "${@}"

echo "=== All Locutus test checks passed! ==="
