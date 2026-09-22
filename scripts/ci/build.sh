#!/usr/bin/env bash
set -euo pipefail

# scripts/ci/build.sh: Compiles the native Locutus binary
# Uses nimble build if available, otherwise direct nim compiler invocation.

NIM_BIN="${NIM_BIN:-nim}"
OUT_DIR="${OUT_DIR:-bin}"

echo "=== Compiling native Locutus binary with ${NIM_BIN} ==="
mkdir -p "${OUT_DIR}"
rm -f "${OUT_DIR}/locutus" "${OUT_DIR}/locutus.exe"

if command -v nimble >/dev/null 2>&1; then
  nimble build -y -d:release
else
  "${NIM_BIN}" c -d:release -o:"${OUT_DIR}/locutus" src/locutus.nim
fi

echo "=== Verifying compiled binary ==="
if [ -f "./${OUT_DIR}/locutus.exe" ]; then
  "./${OUT_DIR}/locutus.exe" --version || "./${OUT_DIR}/locutus.exe" --help >/dev/null
  echo "=== Build succeeded: ${OUT_DIR}/locutus.exe ==="
else
  "./${OUT_DIR}/locutus" --version || "./${OUT_DIR}/locutus" --help >/dev/null
  echo "=== Build succeeded: ${OUT_DIR}/locutus ==="
fi
