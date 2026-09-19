#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:-}"
ASSETS_DIR="${2:-dist}"

if [ -z "$VERSION" ]; then
  echo "Usage: update-homebrew-tap.sh <version> [assets_dir]"
  exit 1
fi

VERSION="${VERSION#v}"
SHA_FILE="${ASSETS_DIR}/SHA256SUMS.txt"
if [ ! -f "$SHA_FILE" ]; then
  echo "Error: SHA256SUMS.txt not found in ${ASSETS_DIR}"
  exit 1
fi

DARWIN_ARM64_SHA=$(grep "locutus-darwin-arm64.tar.gz" "$SHA_FILE" | awk '{print $1}')
DARWIN_AMD64_SHA=$(grep "locutus-darwin-amd64.tar.gz" "$SHA_FILE" | awk '{print $1}')
LINUX_ARM64_SHA=$(grep "locutus-linux-arm64.tar.gz" "$SHA_FILE" | awk '{print $1}')
LINUX_AMD64_SHA=$(grep "locutus-linux-amd64.tar.gz" "$SHA_FILE" | awk '{print $1}')

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

git clone https://github.com/axiomantic/homebrew-tap.git "$TMP_DIR"

cat << FORMULA > "$TMP_DIR/Formula/locutus.rb"
class Locutus < Formula
  desc "Message exchange and routing for software agents over Redis without a background daemon"
  homepage "https://github.com/axiomantic/locutus"
  version "${VERSION}"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/axiomantic/locutus/releases/download/v#{version}/locutus-darwin-arm64.tar.gz"
      sha256 "${DARWIN_ARM64_SHA}"
    else
      url "https://github.com/axiomantic/locutus/releases/download/v#{version}/locutus-darwin-amd64.tar.gz"
      sha256 "${DARWIN_AMD64_SHA}"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/axiomantic/locutus/releases/download/v#{version}/locutus-linux-arm64.tar.gz"
      sha256 "${LINUX_ARM64_SHA}"
    else
      url "https://github.com/axiomantic/locutus/releases/download/v#{version}/locutus-linux-amd64.tar.gz"
      sha256 "${LINUX_AMD64_SHA}"
    end
  end

  head "https://github.com/axiomantic/locutus.git", branch: "main"

  depends_on "nim" => :build if build.head?
  depends_on "redis" => :recommended

  def install
    if build.head?
      system "nim", "c", "-d:release", "--opt:speed", "-o:bin/locutus", "src/locutus.nim"
      bin.install "bin/locutus"
    else
      bin.install "locutus"
    end
    pkgshare.install "skills" if File.exist?("skills")
  end

  def caveats
    <<~EOS
      To equip your AI coding assistants (Claude Code, Antigravity, OpenCode, Cursor):
        npx skills add axiomantic/locutus -g
        # Or using skilz:
        skilz install https://github.com/axiomantic/locutus
        # Or offline from local Homebrew files:
        npx skills add #{opt_pkgshare}/skills/locutus -g
    EOS
  end

  test do
    assert_match "Nim Native", shell_output("#{bin}/locutus --help")
  end
end
FORMULA

cd "$TMP_DIR"
git config user.name "Axiomantic Bot"
git config user.email "info@axiomantic.org"
git add Formula/locutus.rb
git commit -m "chore(release): bump locutus formula to v${VERSION}"
git push origin main
echo "✓ Successfully updated Formula/locutus.rb in axiomantic/homebrew-tap to v${VERSION}"
