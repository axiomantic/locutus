import os
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INSTALL_SH = os.path.join(REPO_ROOT, "scripts", "install.sh")
INSTALL_PS1 = os.path.join(REPO_ROOT, "scripts", "install.ps1")
SKILLS_DIR = os.path.join(REPO_ROOT, "skills", "locutus")


class TestInstallerAndUninstaller(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="locutus_install_test_")
        self.install_dir = os.path.join(self.temp_dir, "bin")
        self.fake_home = os.path.join(self.temp_dir, "home")
        os.makedirs(self.install_dir, exist_ok=True)
        os.makedirs(self.fake_home, exist_ok=True)

        # Mock coding assistant parent directories in fake home
        os.makedirs(os.path.join(self.fake_home, ".claude"), exist_ok=True)
        os.makedirs(os.path.join(self.fake_home, ".gemini", "config"), exist_ok=True)
        os.makedirs(os.path.join(self.fake_home, ".agents"), exist_ok=True)
        os.makedirs(os.path.join(self.fake_home, ".codex"), exist_ok=True)

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    @unittest.skipIf(os.name == "nt", "POSIX install.sh test")
    def test_01_install_sh_with_mock_agents(self):
        """Test install.sh installs both binary and skills into mock assistant directories."""
        env = os.environ.copy()
        env["INSTALL_DIR"] = self.install_dir
        env["HOME"] = self.fake_home
        env["BUILD_FROM_SOURCE"] = "1"
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:" + os.path.dirname(shutil.which("nim") or "")

        res = subprocess.run(
            ["bash", INSTALL_SH],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(res.returncode, 0, f"install.sh failed:\n{res.stderr}\n{res.stdout}")

        # 1. Verify binary
        binary_path = os.path.join(self.install_dir, "locutus")
        self.assertTrue(os.path.isfile(binary_path), f"Binary not installed at {binary_path}")
        self.assertTrue(os.access(binary_path, os.X_OK), "Binary is not executable")

        verify_run = subprocess.run([binary_path, "--help"], capture_output=True, text=True)
        self.assertEqual(verify_run.returncode, 0)
        self.assertIn("Nim Native", verify_run.stdout)

        # 2. Verify skills were installed into detected mock assistants
        claude_skill = os.path.join(self.fake_home, ".claude", "skills", "locutus", "SKILL.md")
        claude_ref = os.path.join(self.fake_home, ".claude", "skills", "locutus", "references", "wire_spec.md")
        gemini_skill = os.path.join(self.fake_home, ".gemini", "config", "skills", "locutus", "SKILL.md")
        agents_skill = os.path.join(self.fake_home, ".agents", "skills", "locutus", "SKILL.md")
        codex_skill = os.path.join(self.fake_home, ".codex", "skills", "locutus", "SKILL.md")

        self.assertTrue(os.path.isfile(claude_skill), f"Missing {claude_skill}")
        self.assertTrue(os.path.isfile(claude_ref), f"Missing {claude_ref}")
        self.assertTrue(os.path.isfile(gemini_skill), f"Missing {gemini_skill}")
        self.assertTrue(os.path.isfile(agents_skill), f"Missing {agents_skill}")
        self.assertTrue(os.path.isfile(codex_skill), f"Missing {codex_skill}")

        # 3. Test Uninstallation
        un_res = subprocess.run(
            ["bash", INSTALL_SH, "--uninstall"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(un_res.returncode, 0, f"Uninstall failed:\n{un_res.stderr}\n{un_res.stdout}")
        self.assertIn("completely uninstalled", un_res.stdout)

        # Verify binary removed
        self.assertFalse(os.path.exists(binary_path), "Binary was not removed")

        # Verify skills removed
        self.assertFalse(os.path.exists(os.path.dirname(claude_skill)), "Claude skill directory was not removed")
        self.assertFalse(os.path.exists(os.path.dirname(gemini_skill)), "Gemini skill directory was not removed")
        self.assertFalse(os.path.exists(os.path.dirname(agents_skill)), "Agents skill directory was not removed")
        self.assertFalse(os.path.exists(os.path.dirname(codex_skill)), "Codex skill directory was not removed")

    @unittest.skipIf(os.name == "nt", "POSIX install.sh test")
    def test_02_install_sh_no_skills_flag(self):
        """Test install.sh with NO_SKILLS=1 installs only binary and leaves skills unconfigured."""
        env = os.environ.copy()
        env["INSTALL_DIR"] = self.install_dir
        env["HOME"] = self.fake_home
        env["NO_SKILLS"] = "1"
        env["BUILD_FROM_SOURCE"] = "1"
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:" + os.path.dirname(shutil.which("nim") or "")

        res = subprocess.run(
            ["bash", INSTALL_SH],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Skipping AI agent skill installation", res.stdout)

        # Binary should exist
        binary_path = os.path.join(self.install_dir, "locutus")
        self.assertTrue(os.path.isfile(binary_path))

        # No skill dirs created
        claude_skill = os.path.join(self.fake_home, ".claude", "skills", "locutus")
        self.assertFalse(os.path.exists(claude_skill))

    def test_03_skilz_compatibility(self):
        """Test skilz install and remove if skilz CLI is available."""
        skilz_cmd = shutil.which("skilz")
        if not skilz_cmd:
            venv_skilz = os.path.join(REPO_ROOT, ".venv", "bin", "skilz")
            if os.path.isfile(venv_skilz):
                skilz_cmd = venv_skilz

        if not skilz_cmd:
            self.skipTest("skilz package manager not installed")

        proj_dir = os.path.join(self.temp_dir, "test_agent_project")
        os.makedirs(proj_dir, exist_ok=True)

        # Install skill into test project for claude
        install_res = subprocess.run(
            [skilz_cmd, "-y", "install", "-f", SKILLS_DIR, "--agent", "claude", "-p"],
            cwd=proj_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(install_res.returncode, 0, f"skilz install failed:\n{install_res.stderr}\n{install_res.stdout}")
        self.assertIn("Installed: locutus", install_res.stdout)

        installed_skill = os.path.join(proj_dir, ".claude", "skills", "locutus", "SKILL.md")
        installed_spec = os.path.join(proj_dir, ".claude", "skills", "locutus", "references", "wire_spec.md")
        self.assertTrue(os.path.isfile(installed_skill), "skilz did not install SKILL.md")
        self.assertTrue(os.path.isfile(installed_spec), "skilz did not install references/wire_spec.md")

        # Uninstall skill via skilz
        remove_res = subprocess.run(
            [skilz_cmd, "-y", "remove", "locutus", "-p"],
            cwd=proj_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(remove_res.returncode, 0, f"skilz remove failed:\n{remove_res.stderr}\n{remove_res.stdout}")
        self.assertFalse(os.path.exists(installed_skill), "SKILL.md was not removed by skilz")

    def test_04_skills_sh_manifest_validation(self):
        """Validate that SKILL.md conforms to the Agent Skills frontmatter standard."""
        skill_file = os.path.join(SKILLS_DIR, "SKILL.md")
        self.assertTrue(os.path.isfile(skill_file))

        with open(skill_file, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertTrue(content.startswith("---"))
        parts = content.split("---", 2)
        self.assertGreaterEqual(len(parts), 3, "Invalid YAML frontmatter")

        frontmatter = parts[1]
        self.assertIn("name: locutus", frontmatter)
        self.assertIn("description:", frontmatter)

    def test_05_npx_skills_discovery(self):
        """Test npx skills list if npx is available."""
        npx_cmd = shutil.which("npx")
        if not npx_cmd:
            self.skipTest("npx not installed")

        res = subprocess.run(
            [npx_cmd, "skills", "add", REPO_ROOT, "-l"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode == 0:
            self.assertIn("Found 1 skill", res.stdout)
            self.assertIn("locutus", res.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows install.ps1 test")
    def test_06_install_ps1_windows(self):
        """Test install.ps1 and -Uninstall on Windows."""
        temp_user = os.path.join(self.temp_dir, "win_user")
        temp_appdata = os.path.join(self.temp_dir, "win_appdata")
        os.makedirs(os.path.join(temp_user, ".claude"), exist_ok=True)
        os.makedirs(os.path.join(temp_user, ".agents"), exist_ok=True)
        os.makedirs(temp_appdata, exist_ok=True)

        env = os.environ.copy()
        env["USERPROFILE"] = temp_user
        env["LOCALAPPDATA"] = temp_appdata
        env["NO_SKILLS"] = "0"

        # Install
        res = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", INSTALL_PS1, "-BuildFromSource"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(res.returncode, 0, f"install.ps1 failed:\n{res.stderr}\n{res.stdout}")

        exe_path = os.path.join(temp_appdata, "Programs", "locutus", "locutus.exe")
        self.assertTrue(os.path.isfile(exe_path), f"Missing {exe_path}")

        # Check skills
        claude_skill = os.path.join(temp_user, ".claude", "skills", "locutus", "SKILL.md")
        self.assertTrue(os.path.isfile(claude_skill))

        # Uninstall
        un_res = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", INSTALL_PS1, "-Uninstall"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(un_res.returncode, 0, f"Uninstall failed:\n{un_res.stderr}\n{un_res.stdout}")
        self.assertFalse(os.path.exists(exe_path))
        self.assertFalse(os.path.exists(os.path.dirname(claude_skill)))

    def test_07_scoop_manifest_spec(self):
        """Verify Scoop manifest has valid schema, hooks, and skill installation notes."""
        import json
        manifest_path = os.path.join(REPO_ROOT, "packaging", "scoop", "locutus.json")
        self.assertTrue(os.path.isfile(manifest_path))

        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["architecture"]["64bit"]["bin"], "locutus.exe")
        self.assertIn("post_install", data)
        self.assertIn("post_uninstall", data)
        self.assertIn("notes", data)

        post_install_text = " ".join(data["post_install"])
        self.assertIn("npx", post_install_text)
        self.assertIn("skilz", post_install_text)

        post_uninstall_text = " ".join(data["post_uninstall"])
        self.assertIn("npx", post_uninstall_text)
        self.assertIn("skilz", post_uninstall_text)

        notes_text = " ".join(data["notes"])
        self.assertIn("npx skills", notes_text)
        self.assertIn("skilz", notes_text)
        self.assertIn("$dir\\skills\\locutus", notes_text)

    def test_08_homebrew_formula_spec(self):
        """Verify Homebrew formula installs skills into pkgshare and includes caveats."""
        formula_path = os.path.join(REPO_ROOT, "Formula", "locutus.rb")
        self.assertTrue(os.path.isfile(formula_path))

        with open(formula_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn('pkgshare.install "skills"', content)
        self.assertIn("def caveats", content)
        self.assertIn("npx skills add", content)
        self.assertIn("skilz install", content)
        self.assertIn("#{opt_pkgshare}/skills/locutus", content)

    def test_09_release_workflow_spec(self):
        """Verify GitHub release workflow packages skills across Linux, macOS, Debian, and Windows."""
        workflow_path = os.path.join(REPO_ROOT, ".github", "workflows", "release.yml")
        self.assertTrue(os.path.isfile(workflow_path))

        with open(workflow_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Check tarball bundling
        self.assertIn("tar -czf dist/locutus-linux-amd64.tar.gz -C dist/linux-amd64 locutus skills", content)
        self.assertIn("tar -czf dist/locutus-darwin-arm64.tar.gz -C dist/darwin-arm64 locutus skills", content)

        # Check Windows zip bundling
        self.assertIn("Copy-Item -Recurse -Force skills dist\\windows-amd64\\skills", content)

        # Check Debian skills and postinst
        self.assertIn("usr/share/locutus/skills", content)
        self.assertIn("cp -r skills/locutus deb-amd64/usr/share/locutus/skills/", content)
        self.assertIn("deb-amd64/DEBIAN/postinst", content)
        self.assertIn("npx skills add /usr/share/locutus/skills/locutus -g", content)
        self.assertIn("skilz install -f /usr/share/locutus/skills/locutus", content)


if __name__ == "__main__":
    unittest.main()

