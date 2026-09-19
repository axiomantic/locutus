#!/usr/bin/env bash
# Locutus Universal Installer for macOS and Linux
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/axiomantic/locutus/main/scripts/install.sh | bash
#
# Uninstallation:
#   curl -fsSL https://raw.githubusercontent.com/axiomantic/locutus/main/scripts/install.sh | bash -s -- --uninstall
#   # Or if you have the script locally:
#   ./scripts/install.sh --uninstall
#
# Environment variables:
#   LOCUTUS_VERSION   : Install specific release version (default: latest)
#   INSTALL_DIR       : Custom installation directory (default: /usr/local/bin or ~/.local/bin)
#   BUILD_FROM_SOURCE : Set to 1 to force building from source via Nim

set -euo pipefail

REPO="axiomantic/locutus"
GITHUB_URL="https://github.com/${REPO}"

# 0. Handle Uninstallation
if [[ "${1:-}" == "--uninstall" || "${1:-}" == "uninstall" || "${1:-}" == "-u" ]]; then
  echo "=== Locutus Uninstaller ==="
  REMOVED=0

  # A. Remove Skills
  echo "Checking for installed Locutus AI agent skills..."
  if command -v npx >/dev/null 2>&1; then
    npx -y skills remove locutus -g -y 2>/dev/null || true
  fi
  if command -v skilz >/dev/null 2>&1; then
    skilz -y remove locutus 2>/dev/null || true
  fi
  for sdir in \
    "${HOME}/.claude/skills/locutus" \
    "${HOME}/.gemini/config/skills/locutus" \
    "${HOME}/.gemini/antigravity/skills/locutus" \
    "${HOME}/.agents/skills/locutus" \
    "${HOME}/.codex/skills/locutus" \
    "${HOME}/.hermes/skills/locutus" \
    "${HOME}/.pi/agent/skills/locutus"; do
    if [ -d "$sdir" ]; then
      echo "Removing skill directory: $sdir"
      rm -rf "$sdir"
      REMOVED=1
    fi
  done

  # B. Check Homebrew
  if command -v brew >/dev/null 2>&1 && brew list locutus >/dev/null 2>&1; then
    echo "Detected Homebrew installation. Removing..."
    brew uninstall locutus && REMOVED=1
  fi

  # C. Check Debian / dpkg
  if command -v dpkg >/dev/null 2>&1 && dpkg -s locutus >/dev/null 2>&1; then
    echo "Detected Debian/dpkg installation. Removing..."
    if [ "${EUID:-$(id -u)}" -eq 0 ]; then
      apt-get remove -y locutus || dpkg -r locutus
    elif command -v sudo >/dev/null 2>&1; then
      sudo apt-get remove -y locutus || sudo dpkg -r locutus
    else
      dpkg -r locutus
    fi
    REMOVED=1
  fi

  # D. Check standard binary paths
  for p in "/usr/local/bin/locutus" "${HOME}/.local/bin/locutus" "${INSTALL_DIR:-}/locutus"; do
    if [ -f "$p" ]; then
      echo "Removing binary at: $p"
      if [ -w "$(dirname "$p")" ]; then
        rm -f "$p"
      elif command -v sudo >/dev/null 2>&1; then
        sudo rm -f "$p"
      fi
      REMOVED=1
    fi
  done

  if [ "$REMOVED" -eq 1 ]; then
    echo "✓ Locutus (binary and AI agent skills) has been completely uninstalled."
    echo "Note: Configuration files in ~/.config/locutus were preserved."
    echo "To remove config & secrets: rm -rf ~/.config/locutus"
  else
    echo "Locutus does not appear to be installed on this system."
  fi
  exit 0
fi

echo "=== Locutus Installer ==="

# 1. Detect Operating System
OS="$(uname -s)"
case "${OS}" in
  Darwin*) OS="darwin" ;;
  Linux*)  OS="linux" ;;
  *)
    echo "Notice: Non-standard Unix OS '${OS}' detected. Will attempt to build from source."
    OS="unknown"
    ;;
esac

# 2. Detect CPU Architecture
ARCH="$(uname -m)"
case "${ARCH}" in
  x86_64|amd64)  ARCH="amd64" ;;
  arm64|aarch64) ARCH="arm64" ;;
  *)
    echo "Notice: Non-standard architecture '${ARCH}' detected. Will attempt to build from source."
    ARCH="unknown"
    ;;
esac

echo "Platform: ${OS}-${ARCH}"

# 3. Resolve Target Version
if [ -z "${LOCUTUS_VERSION:-}" ]; then
  echo "Resolving latest release from GitHub..."
  LATEST_JSON=$(curl -sSL "https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null || true)
  VERSION=$(echo "${LATEST_JSON}" | (grep '"tag_name":' || true) | head -n 1 | sed -E 's/.*"([^"]+)".*/\1/')
  if [ -z "${VERSION}" ]; then
    VERSION="v0.1.1"
  else
    echo "Latest release: ${VERSION}"
  fi
else
  VERSION="${LOCUTUS_VERSION}"
  if [[ "${VERSION}" != v* ]]; then
    VERSION="v${VERSION}"
  fi
  echo "Requested version: ${VERSION}"
fi

# Determine Destination Directory
if [ -n "${INSTALL_DIR:-}" ]; then
  DEST_DIR="${INSTALL_DIR}"
elif [ -w "/usr/local/bin" ]; then
  DEST_DIR="/usr/local/bin"
else
  DEST_DIR="${HOME}/.local/bin"
fi
mkdir -p "${DEST_DIR}"

# Helper: Build from source if binaries are unavailable
build_from_source() {
  echo ""
  echo "=== Building Locutus from Source ==="
  echo "Checking for Nim compiler..."

  if ! command -v nim >/dev/null 2>&1; then
    echo "Nim not found on system. Installing Nim via choosenim..."
    if command -v curl >/dev/null 2>&1; then
      curl https://nim-lang.org/choosenim/init.sh -sSf | sh -s -- -y
    elif command -v wget >/dev/null 2>&1; then
      wget -qO- https://nim-lang.org/choosenim/init.sh | sh -s -- -y
    else
      echo "Error: Neither curl nor wget found. Please install Nim manually: https://nim-lang.org/install.html"
      exit 1
    fi
    export PATH="${HOME}/.nimble/bin:${PATH}"
  fi

  if ! command -v nim >/dev/null 2>&1; then
    echo "Error: Failed to set up Nim compiler. Please install Nim manually."
    exit 1
  fi
  if [ -f "src/locutus.nim" ]; then
    echo "Compiling native Locutus binary from local source tree..."
    rm -f "${DEST_DIR}/locutus"
    nim c -d:release --opt:speed -o:"${DEST_DIR}/locutus" src/locutus.nim
    chmod +x "${DEST_DIR}/locutus"
    if [ "${OS}" = "darwin" ] && command -v codesign >/dev/null 2>&1; then
      codesign -s - -f "${DEST_DIR}/locutus" 2>/dev/null || true
    fi
    echo "✓ Locutus compiled and installed to ${DEST_DIR}/locutus"
    return 0
  fi

  BUILD_TMP="$(mktemp -d)"
  trap 'rm -rf "${BUILD_TMP}"' EXIT

  echo "Fetching Locutus source (${VERSION})..."
  SRC_URL="${GITHUB_URL}/archive/refs/tags/${VERSION}.tar.gz"
  if ! curl -fsSL -o "${BUILD_TMP}/source.tar.gz" "${SRC_URL}"; then
    echo "Release tag tarball not found, falling back to main branch..."
    curl -fsSL -o "${BUILD_TMP}/source.tar.gz" "${GITHUB_URL}/archive/refs/heads/main.tar.gz"
  fi

  tar -xzf "${BUILD_TMP}/source.tar.gz" -C "${BUILD_TMP}"
  SRC_DIR=$(find "${BUILD_TMP}" -mindepth 1 -maxdepth 1 -type d | head -n 1)

  cd "${SRC_DIR}"
  echo "Compiling native Locutus binary with release optimizations..."
  rm -f "${DEST_DIR}/locutus"
  nim c -d:release --opt:speed -o:"${DEST_DIR}/locutus" src/locutus.nim
  chmod +x "${DEST_DIR}/locutus"
  if [ "${OS}" = "darwin" ] && command -v codesign >/dev/null 2>&1; then
    codesign -s - -f "${DEST_DIR}/locutus" 2>/dev/null || true
  fi

  echo "✓ Locutus compiled and installed to ${DEST_DIR}/locutus"
}

# Helper: Install AI Agent Skills
install_skills() {
  if [ "${NO_SKILLS:-0}" = "1" ] || [[ "${1:-}" == "--no-skills" ]]; then
    echo "Skipping AI agent skill installation (--no-skills requested)."
    return 0
  fi

  echo ""
  echo "=== Installing Locutus AI Agent Skills ==="
  SKILL_INSTALLED=0

  # Option A: skills.sh (Vercel Labs) via npx
  if command -v npx >/dev/null 2>&1; then
    echo "Attempting global skill installation via skills.sh (npx)..."
    if npx -y skills add axiomantic/locutus -g -a '*' -y 2>/dev/null; then
      echo "✓ Locutus skill installed globally via skills.sh."
      SKILL_INSTALLED=1
    fi
  fi

  # Option B: skilz (Spillwave)
  if [ "${SKILL_INSTALLED}" -eq 0 ] && command -v skilz >/dev/null 2>&1; then
    echo "Attempting global skill installation via skilz..."
    if skilz -y install https://github.com/axiomantic/locutus 2>/dev/null; then
      echo "✓ Locutus skill installed globally via skilz."
      SKILL_INSTALLED=1
    fi
  fi

  # Option C: Direct fallback to standard assistant directories
  if [ "${SKILL_INSTALLED}" -eq 0 ]; then
    echo "Configuring skills directly for detected AI coding assistants..."
    SKILL_URL="https://raw.githubusercontent.com/${REPO}/main/skills/locutus/SKILL.md"
    SPEC_URL="https://raw.githubusercontent.com/${REPO}/main/skills/locutus/references/wire_spec.md"
    TMP_SKILL="$(mktemp -d)"
    if curl -fsSL -o "${TMP_SKILL}/SKILL.md" "${SKILL_URL}" 2>/dev/null; then
      mkdir -p "${TMP_SKILL}/references"
      curl -fsSL -o "${TMP_SKILL}/references/wire_spec.md" "${SPEC_URL}" 2>/dev/null || true

      for target_skill in \
        "${HOME}/.claude/skills/locutus" \
        "${HOME}/.gemini/config/skills/locutus" \
        "${HOME}/.gemini/antigravity/skills/locutus" \
        "${HOME}/.agents/skills/locutus" \
        "${HOME}/.codex/skills/locutus" \
        "${HOME}/.hermes/skills/locutus" \
        "${HOME}/.pi/agent/skills/locutus"
      do
        parent_agent_dir="$(dirname "$(dirname "${target_skill}")")"
        if [ -d "${parent_agent_dir}" ] || [ -d "$(dirname "${target_skill}")" ]; then
          mkdir -p "${target_skill}/references"
          cp "${TMP_SKILL}/SKILL.md" "${target_skill}/SKILL.md"
          [ -f "${TMP_SKILL}/references/wire_spec.md" ] && cp "${TMP_SKILL}/references/wire_spec.md" "${target_skill}/references/wire_spec.md"
          echo "  ✓ Installed Locutus skill to: ${target_skill}"
          SKILL_INSTALLED=1
        fi
      done
      rm -rf "${TMP_SKILL}"
    fi
  fi

  if [ "${SKILL_INSTALLED}" -eq 1 ]; then
    echo "✓ AI Agent Skills configured successfully."
  else
    echo "Notice: No coding assistant directories detected yet."
    echo "Install the skill into your assistant at any time using:"
    echo "    npx skills add axiomantic/locutus -g"
    echo "    # Or: skilz install https://github.com/axiomantic/locutus"
  fi
}

# If user explicitly requested source build
if [ "${BUILD_FROM_SOURCE:-0}" = "1" ]; then
  build_from_source
  install_skills
  exit 0
fi

# 4. Check for Native Platform Package Managers
# A. macOS with Homebrew (skipped if custom INSTALL_DIR requested)
if [ -z "${INSTALL_DIR:-}" ] && [ "${OS}" = "darwin" ] && command -v brew >/dev/null 2>&1; then
  echo "Detected Homebrew on macOS. Installing via Homebrew tap..."
  if brew install axiomantic/tap/locutus; then
    echo "✓ Locutus successfully installed via Homebrew."
    install_skills
    exit 0
  else
    echo "Homebrew tap install failed or pending tap creation. Falling back to binary release..."
  fi
fi

# B. Debian / Ubuntu with dpkg/apt (skipped if custom INSTALL_DIR requested)
if [ -z "${INSTALL_DIR:-}" ] && [ "${OS}" = "linux" ] && [ "${ARCH}" != "unknown" ] && (command -v dpkg >/dev/null 2>&1 || [ -f /etc/debian_version ]); then
  DEB_PKG="locutus_${VERSION#v}_${ARCH}.deb"
  DEB_URL="${GITHUB_URL}/releases/download/${VERSION}/${DEB_PKG}"
  TMP_DIR="$(mktemp -d)"
  trap 'rm -rf "${TMP_DIR}"' EXIT
  echo "Detected Debian/Ubuntu system. Checking package: ${DEB_PKG}..."
  if curl -fSL --progress-bar -o "${TMP_DIR}/${DEB_PKG}" "${DEB_URL}" 2>/dev/null; then
    echo "Installing ${DEB_PKG} via dpkg/apt..."
    if [ "${EUID:-$(id -u)}" -eq 0 ]; then
      apt-get install -y "${TMP_DIR}/${DEB_PKG}" 2>/dev/null || dpkg -i "${TMP_DIR}/${DEB_PKG}"
    elif command -v sudo >/dev/null 2>&1; then
      sudo apt-get install -y "${TMP_DIR}/${DEB_PKG}" 2>/dev/null || sudo dpkg -i "${TMP_DIR}/${DEB_PKG}"
    else
      dpkg -i "${TMP_DIR}/${DEB_PKG}"
    fi
    echo "✓ Locutus successfully installed from Debian package."
    install_skills
    exit 0
  else
    echo "Notice: ${DEB_PKG} not found on release, proceeding with standalone binary..."
  fi
fi

# 5. Standalone Pre-Compiled Binary Download
if [ "${OS}" != "unknown" ] && [ "${ARCH}" != "unknown" ]; then
  TARBALL="locutus-${OS}-${ARCH}.tar.gz"
  DOWNLOAD_URL="${GITHUB_URL}/releases/download/${VERSION}/${TARBALL}"
  CHECKSUMS_URL="${GITHUB_URL}/releases/download/${VERSION}/SHA256SUMS.txt"

  TMP_DIR="$(mktemp -d)"
  trap 'rm -rf "${TMP_DIR}"' EXIT

  echo "Downloading pre-compiled binary: ${DOWNLOAD_URL}..."
  if curl -fSL --progress-bar -o "${TMP_DIR}/${TARBALL}" "${DOWNLOAD_URL}" 2>/dev/null; then
    # Cryptographic Checksum Verification
    if curl -fsSL -o "${TMP_DIR}/SHA256SUMS.txt" "${CHECKSUMS_URL}" 2>/dev/null; then
      echo "Verifying cryptographic checksum..."
      cd "${TMP_DIR}"
      if command -v sha256sum >/dev/null 2>&1; then
        grep "${TARBALL}" SHA256SUMS.txt | sha256sum -c --status 2>/dev/null && echo "✓ SHA256 checksum verified." || echo "Warning: Checksum mismatch or missing entry."
      elif command -v shasum >/dev/null 2>&1; then
        grep "${TARBALL}" SHA256SUMS.txt | shasum -a 256 -c --status 2>/dev/null && echo "✓ SHA256 checksum verified." || echo "Warning: Checksum mismatch or missing entry."
      fi
      cd - >/dev/null
    fi

    tar -xzf "${TMP_DIR}/${TARBALL}" -C "${TMP_DIR}"
    if [ -f "${TMP_DIR}/locutus" ]; then
      rm -f "${DEST_DIR}/locutus"
      cp "${TMP_DIR}/locutus" "${DEST_DIR}/locutus"
      chmod +x "${DEST_DIR}/locutus"
      if [ "${OS}" = "darwin" ] && command -v codesign >/dev/null 2>&1; then
        codesign -s - -f "${DEST_DIR}/locutus" 2>/dev/null || true
      fi
      echo "✓ Locutus installed to ${DEST_DIR}/locutus"

      # PATH Check
      if [[ ":$PATH:" != *":${DEST_DIR}:"* ]]; then
        echo ""
        echo "⚠️  Note: ${DEST_DIR} is not currently in your PATH."
        echo "Add it to your profile (~/.zshrc or ~/.bashrc):"
        echo "    export PATH=\"${DEST_DIR}:\$PATH\""
      fi

      "${DEST_DIR}/locutus" --help >/dev/null 2>&1 && echo "✓ Locutus is ready to use!" || true
      install_skills
      exit 0
    fi
  else
    echo "Notice: Pre-compiled binary not found for ${OS}-${ARCH}."
  fi
fi

# 6. Fallback: Build from source if binary was not found or architecture is unsupported
echo "Falling back to building Locutus from source..."
build_from_source

# PATH Check
if [[ ":$PATH:" != *":${DEST_DIR}:"* ]]; then
  echo ""
  echo "⚠️  Note: ${DEST_DIR} is not currently in your PATH."
  echo "Add it to your profile (~/.zshrc or ~/.bashrc):"
  echo "    export PATH=\"${DEST_DIR}:\$PATH\""
fi

"${DEST_DIR}/locutus" --help >/dev/null 2>&1 && echo "✓ Locutus is ready to use!" || true
install_skills
echo "Run 'locutus --help' to get started."
