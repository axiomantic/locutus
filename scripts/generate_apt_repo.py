#!/usr/bin/env python3
"""
generate_apt_repo.py
Generates an APT repository structure (Packages, Packages.gz, Release, index.html)
from Debian (.deb) packages.
"""
import gzip
import hashlib
import os
import shutil
import subprocess
import sys


def sha256_file(filepath):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def md5_file(filepath):
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def extract_control_info(deb_path):
    """Extracts control fields from .deb package via dpkg-deb or ar."""
    if shutil.which("dpkg-deb"):
        out = subprocess.check_output(["dpkg-deb", "-f", deb_path], text=True).strip()
        return out

    # Fallback using ar and tar if dpkg-deb is not available
    import tempfile
    import tarfile

    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["ar", "x", deb_path], cwd=tmp, check=True)
        control_tar = None
        for cand in ["control.tar.gz", "control.tar.xz", "control.tar.zst"]:
            p = os.path.join(tmp, cand)
            if os.path.isfile(p):
                control_tar = p
                break
        if control_tar and not control_tar.endswith(".zst"):
            with tarfile.open(control_tar, "r:*") as tar:
                member = tar.extractfile("./control") or tar.extractfile("control")
                if member:
                    return member.read().decode("utf-8").strip()

    # Fallback to minimal control based on filename
    basename = os.path.basename(deb_path)
    parts = basename.replace(".deb", "").split("_")
    pkg = parts[0] if len(parts) > 0 else "locutus"
    ver = parts[1] if len(parts) > 1 else "0.1.2"
    arch = parts[2] if len(parts) > 2 else "amd64"
    return f"Package: {pkg}\nVersion: {ver}\nArchitecture: {arch}\nMaintainer: Axiomantic <info@axiomantic.org>\nDescription: Locutus Inter-Assistant Redis Bus"


def main():
    if len(sys.argv) < 3:
        print("Usage: generate_apt_repo.py <debs_dir> <apt_output_dir>")
        sys.exit(1)

    debs_dir = os.path.abspath(sys.argv[1])
    out_dir = os.path.abspath(sys.argv[2])
    os.makedirs(out_dir, exist_ok=True)

    deb_files = [f for f in os.listdir(debs_dir) if f.endswith(".deb")]
    if not deb_files:
        print(f"Warning: No .deb files found in {debs_dir}")
        return

    packages_entries = []

    for deb in sorted(deb_files):
        deb_path = os.path.join(debs_dir, deb)
        dest_deb = os.path.join(out_dir, deb)
        if deb_path != dest_deb:
            shutil.copy2(deb_path, dest_deb)

        control_output = extract_control_info(deb_path)
        size = os.path.getsize(dest_deb)
        sha256 = sha256_file(dest_deb)
        md5 = md5_file(dest_deb)

        entry = f"{control_output}\nFilename: {deb}\nSize: {size}\nMD5sum: {md5}\nSHA256: {sha256}\n"
        packages_entries.append(entry)

    packages_content = "\n".join(packages_entries) + "\n"
    packages_path = os.path.join(out_dir, "Packages")
    with open(packages_path, "w", encoding="utf-8") as f:
        f.write(packages_content)

    packages_gz_path = os.path.join(out_dir, "Packages.gz")
    with gzip.open(packages_gz_path, "wb") as gz:
        gz.write(packages_content.encode("utf-8"))

    # Generate Release file
    release_lines = [
        "Origin: Axiomantic",
        "Label: Locutus",
        "Suite: stable",
        "Codename: stable",
        "Architectures: amd64 arm64",
        "Components: main",
        "Description: Axiomantic Locutus Debian/Ubuntu APT Repository",
        "MD5Sum:",
    ]

    for fname in ["Packages", "Packages.gz"]:
        fpath = os.path.join(out_dir, fname)
        sz = os.path.getsize(fpath)
        m = md5_file(fpath)
        release_lines.append(f" {m} {sz} {fname}")

    release_lines.append("SHA256:")
    for fname in ["Packages", "Packages.gz"]:
        fpath = os.path.join(out_dir, fname)
        sz = os.path.getsize(fpath)
        s = sha256_file(fpath)
        release_lines.append(f" {s} {sz} {fname}")

    release_path = os.path.join(out_dir, "Release")
    with open(release_path, "w", encoding="utf-8") as f:
        f.write("\n".join(release_lines) + "\n")

    # Generate a user-friendly index.html
    html_content = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Axiomantic Locutus APT Repository</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; line-height: 1.6; color: #24292e; }
    pre { background: #f6f8fa; padding: 16px; border-radius: 6px; overflow: auto; border: 1px solid #e1e4e8; }
    code { font-family: SFMono-Regular, Consolas, 'Liberation Mono', Menlo, monospace; font-size: 0.9em; }
    h1 { border-bottom: 1px solid #eaecef; padding-bottom: 10px; }
  </style>
</head>
<body>
  <h1>Axiomantic Locutus APT Repository</h1>
  <p>Official Debian/Ubuntu package repository for <strong>Locutus</strong> (amd64 & arm64).</p>

  <h2>Installation via APT</h2>
  <pre><code># 1. Add repository to sources
echo "deb [trusted=yes] https://axiomantic.github.io/locutus/apt/ ./" | sudo tee /etc/apt/sources.list.d/locutus.list

# 2. Update and install
sudo apt-get update
sudo apt-get install -y locutus</code></pre>

  <h2>Homebrew Installation (macOS & Linux)</h2>
  <pre><code>brew install axiomantic/tap/locutus</code></pre>

  <h2>Universal One-Line Installer</h2>
  <pre><code>curl -fsSL https://raw.githubusercontent.com/axiomantic/locutus/main/scripts/install.sh | bash</code></pre>

  <p>For documentation, source code, and release downloads, visit the <a href="https://github.com/axiomantic/locutus">Locutus GitHub Repository</a>.</p>
</body>
</html>
"""
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"APT repository successfully generated in {out_dir}")


if __name__ == "__main__":
    main()
