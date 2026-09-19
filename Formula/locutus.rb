class Locutus < Formula
  desc "Message exchange and routing for software agents over Redis without a background daemon"
  homepage "https://github.com/axiomantic/locutus"
  version "1.0.0"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/axiomantic/locutus/releases/download/v#{version}/locutus-darwin-arm64.tar.gz"
      sha256 "REPLACE_WITH_DARWIN_ARM64_SHA"
    else
      url "https://github.com/axiomantic/locutus/releases/download/v#{version}/locutus-darwin-amd64.tar.gz"
      sha256 "REPLACE_WITH_DARWIN_AMD64_SHA"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/axiomantic/locutus/releases/download/v#{version}/locutus-linux-arm64.tar.gz"
      sha256 "REPLACE_WITH_LINUX_ARM64_SHA"
    else
      url "https://github.com/axiomantic/locutus/releases/download/v#{version}/locutus-linux-amd64.tar.gz"
      sha256 "REPLACE_WITH_LINUX_AMD64_SHA"
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
        # Recommended (automatic updates when Homebrew upgrades locutus):
        ln -sf "#{opt_pkgshare}/skills/locutus" ~/.claude/skills/locutus

        # Or install via skills.sh (copies local skill to assistant):
        npx skills add #{opt_pkgshare}/skills/locutus -g

        # Or install via skilz:
        skilz install -f #{opt_pkgshare}/skills/locutus
    EOS
  end

  test do
    assert_match "Nim Native", shell_output("#{bin}/locutus --help")
  end
end
