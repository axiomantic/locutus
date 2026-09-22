#!/usr/bin/env bash
set -euo pipefail

# scripts/ci/install-deps.sh: Install build and test dependencies for Locutus CI
# Suitable for containerized CI (Forgejo / GitHub Actions) and local environments.

echo "=== Installing Locutus CI dependencies ==="

# Install system packages (Python, Redis, Git) if on Debian/Ubuntu and not present
if command -v apt-get >/dev/null 2>&1; then
  MISSING_PKGS=()
  if ! command -v redis-cli >/dev/null 2>&1 || ! command -v redis-server >/dev/null 2>&1; then
    MISSING_PKGS+=(redis-server redis-tools)
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    MISSING_PKGS+=(python3)
  fi
  if ! command -v pip3 >/dev/null 2>&1 && ! command -v pip >/dev/null 2>&1; then
    MISSING_PKGS+=(python3-pip python3-venv)
  fi
  if ! command -v git >/dev/null 2>&1; then
    MISSING_PKGS+=(git)
  fi

  if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    echo "Installing missing system packages via apt-get: ${MISSING_PKGS[*]}..."
    if [ "$(id -u)" -eq 0 ]; then
      apt-get update -q
      apt-get install -y --no-install-recommends "${MISSING_PKGS[@]}"
    elif command -v sudo >/dev/null 2>&1; then
      sudo apt-get update -q
      sudo apt-get install -y --no-install-recommends "${MISSING_PKGS[@]}"
    fi
  fi
fi

# Install Nim dependencies via nimble
if command -v nimble >/dev/null 2>&1; then
  echo "Installing Nim dependencies via nimble..."
  nimble install -y --depsOnly || true
  # Ensure pkgs2 packages with srcDir="src" have src/ present if nimble copied them to pkg root
  for pdir in "${HOME:-/root}/.nimble/pkgs2" /root/.nimble/pkgs2 /opt/ci-cache/nimble/pkgs2; do
    if [ -d "$pdir" ]; then
      for d in "$pdir"/*; do
        if [ -d "$d" ] && [ ! -e "$d/src" ]; then
          ln -s . "$d/src" 2>/dev/null || true
        fi
      done
    fi
  done
fi

# Setup Python test dependencies
PYTHON_BIN="${PYTHON_BIN:-python3}"
if command -v uv >/dev/null 2>&1; then
  echo "Using uv to install Python dependencies..."
  uv pip install --system -e . "skilz>=0.1.0" 2>/dev/null || uv pip install -e . "skilz>=0.1.0" 2>/dev/null || uv pip install --system -e . 2>/dev/null || uv pip install -e .
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "${VIRTUAL_ENV}/bin/pip" ]; then
  echo "Using active virtualenv pip..."
  "${VIRTUAL_ENV}/bin/pip" install --upgrade pip
  "${VIRTUAL_ENV}/bin/pip" install -e . "skilz>=0.1.0" 2>/dev/null || "${VIRTUAL_ENV}/bin/pip" install -e .
elif command -v pip3 >/dev/null 2>&1; then
  echo "Using pip3..."
  pip3 install --upgrade pip 2>/dev/null || true
  pip3 install --break-system-packages -e . "skilz>=0.1.0" 2>/dev/null || pip3 install -e . "skilz>=0.1.0" 2>/dev/null || pip3 install --break-system-packages -e . 2>/dev/null || pip3 install -e .
elif command -v pip >/dev/null 2>&1; then
  echo "Using pip..."
  pip install --upgrade pip 2>/dev/null || true
  pip install --break-system-packages -e . "skilz>=0.1.0" 2>/dev/null || pip install -e . "skilz>=0.1.0" 2>/dev/null || pip install --break-system-packages -e . 2>/dev/null || pip install -e .
else
  echo "Warning: pip not found. Ensure python packages are installed."
fi

echo "=== Locutus CI dependencies ready ==="
