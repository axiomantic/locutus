#!/usr/bin/env bash
# Locutus Universal Installer for macOS and Linux
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/axiomantic/locutus/main/scripts/install.sh | bash
#
# Environment variables:
#   LOCUTUS_VERSION : Install specific release version (default: latest)
#   INSTALL_DIR     : Custom installation directory (default: /usr/local/bin or ~/.local/bin)

set -euo pipefail

REPO="axiomantic/locutus"
GITHUB_URL="https://github.com/${REPO}"

echo "=== Locutus Installer ==="

# 1. Detect Operating System
OS="$(uname -s)"
case "${OS}" in
  Darwin*) OS="darwin" ;;
  Linux*)  OS="linux" ;;
  *)
    echo "Error: Unsupported operating system '${OS}'."
    echo "Locutus currently provides pre-built binaries for macOS and Linux via this script."
    echo "For Windows, run: irm https://raw.githubusercontent.com/axiomantic/locutus/main/scripts/install.ps1 | iex"
    exit 1
    ;;
esac

# 2. Detect CPU Architecture
ARCH="$(uname -m)"
case "${ARCH}" in
  x86_64|amd64)  ARCH="amd64" ;;
  arm64|aarch64) ARCH="arm64" ;;
  *)
    echo "Error: Unsupported CPU architecture '${ARCH}'."
    exit 1
    ;;
esac

echo "Platform: ${OS}-${ARCH}"

# 3. Resolve Target Version
if [ -z "${LOCUTUS_VERSION:-}" ]; then
  echo "Resolving latest release from GitHub..."
  LATEST_JSON=$(curl -sSL "https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null || true)
  VERSION=$(echo "${LATEST_JSON}" | grep '"tag_name":' | head -n 1 | sed -E 's/.*"([^"]+)".*/\1/')
  if [ -z "${VERSION}" ]; then
    VERSION="v1.0.0"
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

# 4. Check for Native Platform Package Managers
# A. macOS with Homebrew
if [ "${OS}" = "darwin" ] && command -v brew >/dev/null 2>&1; then
  echo "Detected Homebrew on macOS. Installing via Homebrew tap..."
  brew install axiomantic/tap/locutus
  echo "✓ Locutus successfully installed via Homebrew."
  exit 0
fi

# B. Debian / Ubuntu with dpkg/apt
if [ "${OS}" = "linux" ] && (command -v dpkg >/dev/null 2>&1 || [ -f /etc/debian_version ]); then
  DEB_PKG="locutus_${VERSION#v}_${ARCH}.deb"
  DEB_URL="${GITHUB_URL}/releases/download/${VERSION}/${DEB_PKG}"
  TMP_DIR="$(mktemp -d)"
  trap 'rm -rf "${TMP_DIR}"' EXIT
  echo "Detected Debian/Ubuntu system. Downloading package: ${DEB_PKG}..."
  if curl -fSL --progress-bar -o "${TMP_DIR}/${DEB_PKG}" "${DEB_URL}"; then
    echo "Installing ${DEB_PKG} via dpkg/apt..."
    if [ "$EUID" -eq 0 ]; then
      apt-get install -y "${TMP_DIR}/${DEB_PKG}" 2>/dev/null || dpkg -i "${TMP_DIR}/${DEB_PKG}"
    elif command -v sudo >/dev/null 2>&1; then
      sudo apt-get install -y "${TMP_DIR}/${DEB_PKG}" 2>/dev/null || sudo dpkg -i "${TMP_DIR}/${DEB_PKG}"
    else
      dpkg -i "${TMP_DIR}/${DEB_PKG}"
    fi
    echo "✓ Locutus successfully installed from Debian package."
    exit 0
  else
    echo "Notice: ${DEB_PKG} not found on release, proceeding with standalone binary..."
  fi
fi

# 4. Prepare Download URL and Temp Directory
TARBALL="locutus-${OS}-${ARCH}.tar.gz"
DOWNLOAD_URL="${GITHUB_URL}/releases/download/${VERSION}/${TARBALL}"
CHECKSUMS_URL="${GITHUB_URL}/releases/download/${VERSION}/SHA256SUMS.txt"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

echo "Downloading ${DOWNLOAD_URL}..."
if ! curl -fSL --progress-bar -o "${TMP_DIR}/${TARBALL}" "${DOWNLOAD_URL}"; then
  echo "Error: Failed to download ${DOWNLOAD_URL}."
  echo "Check available releases at: ${GITHUB_URL}/releases"
  exit 1
fi

# Optional Checksum Verification
if curl -fsSL -o "${TMP_DIR}/SHA256SUMS.txt" "${CHECKSUMS_URL}" 2>/dev/null; then
  echo "Verifying cryptographic checksum..."
  cd "${TMP_DIR}"
  if command -v sha256sum >/dev/null 2>&1; then
    grep "${TARBALL}" SHA256SUMS.txt | sha256sum -c --status || {
      echo "Error: Checksum verification failed!"
      exit 1
    }
  elif command -v shasum >/dev/null 2>&1; then
    grep "${TARBALL}" SHA256SUMS.txt | shasum -a 256 -c --status || {
      echo "Error: Checksum verification failed!"
      exit 1
    }
  fi
  echo "✓ SHA256 checksum verified."
  cd - >/dev/null
fi

# 5. Extract Binary
tar -xzf "${TMP_DIR}/${TARBALL}" -C "${TMP_DIR}"
if [ ! -f "${TMP_DIR}/locutus" ]; then
  echo "Error: locutus binary not found inside archive."
  exit 1
fi
chmod +x "${TMP_DIR}/locutus"

# 6. Determine Destination Directory
if [ -n "${INSTALL_DIR:-}" ]; then
  DEST_DIR="${INSTALL_DIR}"
elif [ -w "/usr/local/bin" ]; then
  DEST_DIR="/usr/local/bin"
else
  DEST_DIR="${HOME}/.local/bin"
fi

mkdir -p "${DEST_DIR}"
cp "${TMP_DIR}/locutus" "${DEST_DIR}/locutus"
chmod +x "${DEST_DIR}/locutus"

echo "✓ Locutus installed to ${DEST_DIR}/locutus"

# 7. PATH Check
if [[ ":$PATH:" != *":${DEST_DIR}:"* ]]; then
  echo ""
  echo "⚠️  Note: ${DEST_DIR} is not in your system PATH."
  echo "Add it by placing this in your shell profile (~/.zshrc or ~/.bashrc):"
  echo "    export PATH=\"${DEST_DIR}:\$PATH\""
  echo ""
fi

# 8. Test Execution
"${DEST_DIR}/locutus" --help >/dev/null 2>&1 && echo "✓ Locutus is ready to use!" || true
echo "Run 'locutus --help' to get started."
