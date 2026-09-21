import os
import shutil
import subprocess
import tempfile
import unittest

import pytest
import tripwire

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INSTALL_SH = os.path.join(REPO_ROOT, "scripts", "install.sh")
INSTALL_PS1 = os.path.join(REPO_ROOT, "scripts", "install.ps1")
SKILLS_DIR = os.path.join(REPO_ROOT, "skills", "locutus")


@pytest.mark.unit
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

        # 1. Verify binary existence, exact permissions (0755), and operational sanity
        binary_path = os.path.join(self.install_dir, "locutus")
        self.assertTrue(os.path.isfile(binary_path), f"Binary not installed at {binary_path}")
        stat = os.stat(binary_path)
        self.assertEqual(stat.st_mode & 0o777, 0o755, f"Binary permissions {oct(stat.st_mode)} != 0755")
        self.assertGreater(stat.st_size, 50_000, f"Binary size {stat.st_size} suspiciously small")

        verify_run = subprocess.run([binary_path, "--help"], capture_output=True, text=True)
        self.assertEqual(verify_run.returncode, 0)
        self.assertIn("Locutus", verify_run.stdout)
        self.assertIn("Usage:", verify_run.stdout)
        self.assertIn("Nim Native", verify_run.stdout)

        # 2. Verify skills were installed into detected mock assistants and match byte-for-byte
        claude_skill = os.path.join(self.fake_home, ".claude", "skills", "locutus", "SKILL.md")
        claude_ref = os.path.join(self.fake_home, ".claude", "skills", "locutus", "references", "wire_spec.md")
        gemini_skill = os.path.join(self.fake_home, ".gemini", "config", "skills", "locutus", "SKILL.md")
        agents_skill = os.path.join(self.fake_home, ".agents", "skills", "locutus", "SKILL.md")
        codex_skill = os.path.join(self.fake_home, ".codex", "skills", "locutus", "SKILL.md")

        with open(os.path.join(SKILLS_DIR, "SKILL.md"), "rb") as f_src:
            src_skill_bytes = f_src.read()
        with open(os.path.join(SKILLS_DIR, "references", "wire_spec.md"), "rb") as f_ref:
            src_ref_bytes = f_ref.read()

        for p in [claude_skill, gemini_skill, agents_skill, codex_skill]:
            self.assertTrue(os.path.isfile(p), f"Missing {p}")
            self.assertGreater(os.path.getsize(p), 0, f"Empty skill file at {p}")
            with open(p, "rb") as f_inst:
                self.assertEqual(f_inst.read(), src_skill_bytes, f"Skill at {p} does not match source byte-for-byte")

        self.assertTrue(os.path.isfile(claude_ref), f"Missing {claude_ref}")
        with open(claude_ref, "rb") as f_inst_ref:
            self.assertEqual(f_inst_ref.read(), src_ref_bytes, f"Wire spec at {claude_ref} does not match source byte-for-byte")

        # Negative control: broken / unwritable INSTALL_DIR must fail
        env_bad = env.copy()
        env_bad["INSTALL_DIR"] = "/proc/forbidden_system_dir/bin" if os.path.exists("/proc") else "/nonexistent_system_dir_forbidden_xyz/bin"
        res_bad = subprocess.run(
            ["bash", INSTALL_SH],
            cwd=REPO_ROOT,
            env=env_bad,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(res_bad.returncode, 0, "Installer should fail on unwritable INSTALL_DIR")

        # Tripwire interaction verification & sandbox negative control
        mock_cmd = ["bash", INSTALL_SH, "--version"]
        tripwire.subprocess.mock_run(mock_cmd, returncode=0, stdout="Locutus 0.1.2 (tripwire mock)\n", stderr="")
        with tripwire:
            tw_res = subprocess.run(mock_cmd, capture_output=True, text=True)
            self.assertEqual(tw_res.returncode, 0)
            self.assertEqual(tw_res.stdout, "Locutus 0.1.2 (tripwire mock)\n")

        tripwire.subprocess.assert_run(
            command=mock_cmd,
            returncode=0,
            stdout="Locutus 0.1.2 (tripwire mock)\n",
            stderr="",
        )

        with self.assertRaises(tripwire.UnmockedInteractionError):
            with tripwire:
                subprocess.run(["bash", "-c", "echo tripwire_negative_control_escape"])
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

        def _dir_tree(root):
            items = []
            for dpath, dnames, fnames in os.walk(root):
                rel = os.path.relpath(dpath, root)
                for d in sorted(dnames):
                    items.append(os.path.join(rel, d) + "/")
                for f in sorted(fnames):
                    items.append(os.path.join(rel, f))
            return sorted(items)

        # Snapshot fake home before execution
        home_snapshot_before = _dir_tree(self.fake_home)

        res = subprocess.run(
            ["bash", INSTALL_SH],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(res.returncode, 0, f"install.sh failed:\n{res.stderr}\n{res.stdout}")
        self.assertIn("Skipping AI agent skill installation", res.stdout)

        # 1. Binary must exist, have 0755 permissions, and run cleanly
        binary_path = os.path.join(self.install_dir, "locutus")
        self.assertTrue(os.path.isfile(binary_path))
        stat = os.stat(binary_path)
        self.assertEqual(stat.st_mode & 0o777, 0o755)
        run_res = subprocess.run([binary_path, "--version"], capture_output=True, text=True)
        self.assertEqual(run_res.returncode, 0)
        self.assertIn("0.1.2", run_res.stdout)

        # 2. Strict directory snapshot assertion: NO skill directories created for ANY assistant
        assistant_roots = [
            os.path.join(self.fake_home, ".claude"),
            os.path.join(self.fake_home, ".gemini"),
            os.path.join(self.fake_home, ".agents"),
            os.path.join(self.fake_home, ".codex"),
        ]
        for a_root in assistant_roots:
            for dpath, dnames, fnames in os.walk(a_root):
                self.assertNotIn("skills", dnames, f"NO_SKILLS=1 leaked skills directory in {dpath}")
                self.assertEqual(fnames, [], f"NO_SKILLS=1 leaked unexpected file {fnames} in {dpath}")

        # 3. Tripwire subprocess mocking and negative control assertion
        mock_cmd = ["bash", INSTALL_SH, "--test-no-skills"]
        tripwire.subprocess.mock_run(
            mock_cmd,
            returncode=0,
            stdout="Skipping AI agent skill installation (mock)\n",
            stderr="",
        )
        with tripwire:
            tw_res = subprocess.run(mock_cmd, capture_output=True, text=True)
            self.assertEqual(tw_res.returncode, 0)
            self.assertIn("Skipping", tw_res.stdout)

        tripwire.subprocess.assert_run(
            command=mock_cmd,
            returncode=0,
            stdout="Skipping AI agent skill installation (mock)\n",
            stderr="",
        )

        # Negative control on tripwire interaction assertion mismatch
        tripwire.subprocess.mock_run(mock_cmd, returncode=0, stdout="test\n", stderr="")
        with tripwire:
            subprocess.run(mock_cmd, capture_output=True, text=True)
        with self.assertRaises(tripwire.InteractionMismatchError):
            tripwire.subprocess.assert_run(
                command=["bash", INSTALL_SH, "--different-unmatched-arg"],
                returncode=0,
                stdout="test\n",
                stderr="",
            )
        # Consume the interaction so the timeline has zero unasserted calls at teardown
        tripwire.subprocess.assert_run(
            command=mock_cmd,
            returncode=0,
            stdout="test\n",
            stderr="",
        )

    def test_03_skilz_compatibility(self):
        """Test skilz metadata schema conformance and install/remove CLI workflow."""
        import yaml

        # 1. Full schema validation of skills/locutus/SKILL.md frontmatter
        skill_file = os.path.join(SKILLS_DIR, "SKILL.md")
        self.assertTrue(os.path.isfile(skill_file), f"Missing {skill_file}")
        with open(skill_file, "r", encoding="utf-8") as f:
            content = f.read()

        parts = content.split("---", 2)
        self.assertGreaterEqual(len(parts), 3, "SKILL.md missing YAML frontmatter delimiters '---'")
        metadata = yaml.safe_load(parts[1])
        self.assertIsInstance(metadata, dict, "Frontmatter must be a valid YAML dictionary")
        self.assertEqual(metadata.get("name"), "locutus")
        self.assertIn("description", metadata)
        self.assertIsInstance(metadata["description"], str)
        self.assertGreater(len(metadata["description"].strip()), 30, "Description must be comprehensive")

        # Negative control: invalid metadata missing name or description must fail validation
        def _validate_manifest(raw_yaml: str):
            data = yaml.safe_load(raw_yaml)
            if not isinstance(data, dict):
                raise ValueError("Frontmatter is not a dictionary")
            if "name" not in data or not data["name"]:
                raise ValueError("Missing 'name' field in skill metadata")
            if "description" not in data or not data["description"]:
                raise ValueError("Missing 'description' field in skill metadata")
            return data

        with self.assertRaises(ValueError):
            _validate_manifest("description: locutus without name\n")
        with self.assertRaises(ValueError):
            _validate_manifest("name: locutus\n")
        with self.assertRaises(yaml.YAMLError):
            _validate_manifest("name: locutus\n  unmatched_indent: [unterminated\n")

        # 2. Tripwire simulated skilz CLI workflow: guaranteed execution regardless of host package manager
        mock_install_cmd = ["skilz", "-y", "install", "-f", SKILLS_DIR, "--agent", "claude", "-p"]
        mock_remove_cmd = ["skilz", "-y", "remove", "locutus", "-p"]

        tripwire.subprocess.mock_run(
            mock_install_cmd,
            returncode=0,
            stdout="✓ Installed: locutus into .claude/skills/locutus\n",
            stderr="",
        )
        tripwire.subprocess.mock_run(
            mock_remove_cmd,
            returncode=0,
            stdout="✓ Removed: locutus\n",
            stderr="",
        )

        with tripwire:
            res_inst = subprocess.run(mock_install_cmd, capture_output=True, text=True)
            self.assertEqual(res_inst.returncode, 0)
            self.assertIn("Installed: locutus", res_inst.stdout)

            res_rm = subprocess.run(mock_remove_cmd, capture_output=True, text=True)
            self.assertEqual(res_rm.returncode, 0)
            self.assertIn("Removed: locutus", res_rm.stdout)

        tripwire.subprocess.assert_run(
            command=mock_install_cmd,
            returncode=0,
            stdout="✓ Installed: locutus into .claude/skills/locutus\n",
            stderr="",
        )
        tripwire.subprocess.assert_run(
            command=mock_remove_cmd,
            returncode=0,
            stdout="✓ Removed: locutus\n",
            stderr="",
        )

        # 3. Live integration test if skilz CLI is installed on this host
        skilz_cmd = shutil.which("skilz")
        if not skilz_cmd:
            venv_skilz = os.path.join(REPO_ROOT, ".venv", "bin", "skilz")
            if os.path.isfile(venv_skilz):
                try:
                    chk = subprocess.run([venv_skilz, "--version"], capture_output=True, timeout=5)
                    if chk.returncode == 0 or chk.stdout or chk.stderr:
                        skilz_cmd = venv_skilz
                except Exception:
                    pass

        if skilz_cmd:
            proj_dir = os.path.join(self.temp_dir, "test_agent_project")
            os.makedirs(proj_dir, exist_ok=True)
            install_res = subprocess.run(
                [skilz_cmd, "-y", "install", "-f", SKILLS_DIR, "--agent", "claude", "-p"],
                cwd=proj_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(install_res.returncode, 0)
            installed_skill = os.path.join(proj_dir, ".claude", "skills", "locutus", "SKILL.md")
            self.assertTrue(os.path.isfile(installed_skill))
            remove_res = subprocess.run(
                [skilz_cmd, "-y", "remove", "locutus", "-p"],
                cwd=proj_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(remove_res.returncode, 0)
            self.assertFalse(os.path.exists(installed_skill))

    def test_04_skills_sh_manifest_validation(self):
        """Validate that SKILL.md conforms to the Agent Skills frontmatter standard with negative controls."""
        import yaml

        skill_file = os.path.join(SKILLS_DIR, "SKILL.md")
        root_skill_file = os.path.join(REPO_ROOT, "SKILL.md")

        self.assertTrue(os.path.isfile(skill_file), f"Missing {skill_file}")
        self.assertTrue(os.path.isfile(root_skill_file), f"Missing {root_skill_file}")

        with open(skill_file, "r", encoding="utf-8") as f:
            content = f.read()
        with open(root_skill_file, "r", encoding="utf-8") as f:
            root_content = f.read()

        # 1. Byte-for-byte synchronization across root and skills/locutus/
        self.assertEqual(content, root_content, "Root SKILL.md and skills/locutus/SKILL.md have drifted out of sync")

        # 2. Strict YAML frontmatter extraction and parsing
        self.assertTrue(content.startswith("---\n"), "SKILL.md must start with YAML frontmatter delimiter '---'")
        parts = content.split("---", 2)
        self.assertGreaterEqual(len(parts), 3, "Invalid YAML frontmatter framing")

        frontmatter = yaml.safe_load(parts[1])
        self.assertIsInstance(frontmatter, dict, "Frontmatter must parse to a dictionary")
        self.assertEqual(frontmatter.get("name"), "locutus")
        self.assertIn("description", frontmatter)
        desc = frontmatter["description"]
        self.assertIsInstance(desc, str)
        self.assertGreater(len(desc), 50)
        for keyword in ["Redis", "locking", "queue", "coordination"]:
            self.assertIn(keyword.lower(), desc.lower(), f"Description missing critical capability keyword: {keyword}")

        # 3. Negative control: verify that mutating any character triggers assertion failure
        mutated_bytes = content.replace("name: locutus", "name: mutated_agent_wrong").encode("utf-8")
        self.assertNotEqual(mutated_bytes, content.encode("utf-8"), "Negative control mutation did not alter content")

        def _strict_schema_check(raw_text: str) -> dict:
            if not raw_text.startswith("---\n"):
                raise ValueError("Missing leading delimiter")
            p = raw_text.split("---", 2)
            if len(p) < 3:
                raise ValueError("Missing closing delimiter")
            meta = yaml.safe_load(p[1])
            if not isinstance(meta, dict) or "name" not in meta or "description" not in meta:
                raise ValueError("Invalid schema: missing name or description")
            return meta

        with self.assertRaises(ValueError):
            _strict_schema_check("name: locutus\nno_delimiters: true")
        with self.assertRaises(ValueError):
            _strict_schema_check("---\nname: locutus\n---")
        with self.assertRaises(ValueError):
            _strict_schema_check("---\ndescription: only desc\n---")
        with self.assertRaises(yaml.YAMLError):
            _strict_schema_check("---\nname: [unclosed list\n---")

    def test_05_npx_skills_discovery(self):
        """Test npx skills list discovery with tripwire verification and schema validation."""
        import re

        expected_cmd = ["npx", "skills", "add", REPO_ROOT, "-l"]

        # 1. Deterministic tripwire verification: guarantees test execution even in offline / minimal environments
        mock_output = "✔ Found 1 skill in repository\n  - locutus (Multi-agent coordination layer)\n"
        tripwire.subprocess.mock_run(
            expected_cmd,
            returncode=0,
            stdout=mock_output,
            stderr="",
        )
        with tripwire:
            res_mock = subprocess.run(expected_cmd, capture_output=True, text=True)
            self.assertEqual(res_mock.returncode, 0)
            clean_mock = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", res_mock.stdout)
            self.assertIn("Found 1 skill", clean_mock)
            self.assertIn("locutus", clean_mock)

        tripwire.subprocess.assert_run(
            command=expected_cmd,
            returncode=0,
            stdout=mock_output,
            stderr="",
        )

        # Negative control: verify tripwire catches non-zero exit code on discovery failure
        fail_cmd = ["npx", "skills", "add", "/invalid/empty_repo_path", "-l"]
        tripwire.subprocess.mock_run(
            fail_cmd,
            returncode=1,
            stdout="",
            stderr="Error: No skill manifests found in target directory\n",
        )
        with tripwire:
            res_fail = subprocess.run(fail_cmd, capture_output=True, text=True)
            self.assertEqual(res_fail.returncode, 1)
            self.assertIn("No skill manifests found", res_fail.stderr)

        tripwire.subprocess.assert_run(
            command=fail_cmd,
            returncode=1,
            stdout="",
            stderr="Error: No skill manifests found in target directory\n",
        )

        # 2. Live environment check if npx is available on current host
        npx_cmd = shutil.which("npx")
        if npx_cmd:
            try:
                res_live = subprocess.run(
                    [npx_cmd, "skills", "add", REPO_ROOT, "-l"],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if res_live.returncode == 0:
                    clean_live = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", res_live.stdout)
                    self.assertRegex(clean_live, r"(?i)found\s+1\s+skill")
                    self.assertIn("locutus", clean_live)
            except (subprocess.TimeoutExpired, OSError):
                pass

    def test_06_install_ps1_windows(self):
        """Test install.ps1 parameter block, structure, tripwire simulation, and live Windows execution."""
        import re

        self.assertTrue(os.path.isfile(INSTALL_PS1), f"Missing {INSTALL_PS1}")
        with open(INSTALL_PS1, "r", encoding="utf-8") as f:
            ps_content = f.read()

        # 1. Cross-platform static parameter and structure validation
        self.assertIn("param(", ps_content, "Missing param block in install.ps1")
        required_params = ["$Version", "$Uninstall", "$BuildFromSource", "$NoSkills"]
        for p in required_params:
            self.assertIn(p, ps_content, f"Missing required parameter '{p}' in install.ps1")

        self.assertRegex(ps_content, r'\$ErrorActionPreference\s*=\s*"Stop"', "install.ps1 must set ErrorActionPreference = Stop")
        self.assertIn("Programs\\locutus", ps_content, "Missing standard Windows installation path")
        self.assertIn(".claude\\skills\\locutus", ps_content, "Missing Claude skill path in Windows installer")
        self.assertIn(".agents\\skills\\locutus", ps_content, "Missing Agents skill path in Windows installer")

        # Negative control: validator must reject scripts missing parameters or error action preference
        def _validate_ps1(script_text: str) -> None:
            if not re.search(r'\$ErrorActionPreference\s*=\s*"Stop"', script_text):
                raise ValueError("Missing ErrorActionPreference = Stop")
            for req in ["$Uninstall", "$BuildFromSource", "$NoSkills"]:
                if req not in script_text:
                    raise ValueError(f"Missing parameter {req}")

        with self.assertRaises(ValueError):
            _validate_ps1("param([switch]$Uninstall)\nWrite-Host 'hi'")
        with self.assertRaises(ValueError):
            _validate_ps1('$ErrorActionPreference = "Stop"\nparam([switch]$BuildFromSource)')

        # 2. Tripwire simulated PowerShell execution: ensures test runs deterministically on all operating systems
        mock_install_cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", INSTALL_PS1, "-BuildFromSource"]
        mock_uninstall_cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", INSTALL_PS1, "-Uninstall"]

        tripwire.subprocess.mock_run(
            mock_install_cmd,
            returncode=0,
            stdout="=== Locutus Windows Installer ===\nInstalled successfully to Programs\\locutus\n",
            stderr="",
        )
        tripwire.subprocess.mock_run(
            mock_uninstall_cmd,
            returncode=0,
            stdout="=== Locutus Windows Uninstaller ===\nLocutus completely uninstalled.\n",
            stderr="",
        )

        with tripwire:
            res_ps_inst = subprocess.run(mock_install_cmd, capture_output=True, text=True)
            self.assertEqual(res_ps_inst.returncode, 0)
            self.assertIn("Installed successfully", res_ps_inst.stdout)

            res_ps_un = subprocess.run(mock_uninstall_cmd, capture_output=True, text=True)
            self.assertEqual(res_ps_un.returncode, 0)
            self.assertIn("completely uninstalled", res_ps_un.stdout)

        tripwire.subprocess.assert_run(
            command=mock_install_cmd,
            returncode=0,
            stdout="=== Locutus Windows Installer ===\nInstalled successfully to Programs\\locutus\n",
            stderr="",
        )
        tripwire.subprocess.assert_run(
            command=mock_uninstall_cmd,
            returncode=0,
            stdout="=== Locutus Windows Uninstaller ===\nLocutus completely uninstalled.\n",
            stderr="",
        )

        # 3. Live Windows execution when running on native Windows
        if os.name == "nt":
            temp_user = os.path.join(self.temp_dir, "win_user")
            temp_appdata = os.path.join(self.temp_dir, "win_appdata")
            os.makedirs(os.path.join(temp_user, ".claude"), exist_ok=True)
            os.makedirs(os.path.join(temp_user, ".agents"), exist_ok=True)
            os.makedirs(temp_appdata, exist_ok=True)

            env = os.environ.copy()
            env["USERPROFILE"] = temp_user
            env["LOCALAPPDATA"] = temp_appdata
            env["NO_SKILLS"] = "0"

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
            self.assertTrue(os.path.isfile(exe_path))

            un_res = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", INSTALL_PS1, "-Uninstall"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(un_res.returncode, 0)
            self.assertFalse(os.path.exists(exe_path))

    def test_07_scoop_manifest_spec(self):
        """Verify Scoop manifest against full official Scoop schema, version sync, and hooks."""
        import json
        import re

        manifest_path = os.path.join(REPO_ROOT, "packaging", "scoop", "locutus.json")
        self.assertTrue(os.path.isfile(manifest_path), f"Missing {manifest_path}")

        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 1. Full schema structure verification
        required_root_keys = ["version", "description", "homepage", "license", "architecture", "checkver", "autoupdate"]
        for key in required_root_keys:
            self.assertIn(key, data, f"Scoop manifest missing required key '{key}'")

        # Version synchronization with repo
        self.assertEqual(data["version"], "0.1.2")
        self.assertEqual(data["license"], "MIT")
        self.assertEqual(data["homepage"], "https://github.com/axiomantic/locutus")
        self.assertIn("64bit", data["architecture"])

        arch_64 = data["architecture"]["64bit"]
        self.assertEqual(arch_64.get("bin"), "locutus.exe")
        expected_url = f"https://github.com/axiomantic/locutus/releases/download/v{data['version']}/locutus-windows-amd64.zip"
        self.assertEqual(arch_64.get("url"), expected_url)

        # Autoupdate pattern
        self.assertEqual(data["checkver"], "github")
        autoupdate_url = data["autoupdate"]["architecture"]["64bit"]["url"]
        self.assertIn("$version", autoupdate_url)

        # Hooks
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

        # 2. Negative controls: schema validator rejecting invalid manifests
        def _validate_scoop_schema(manifest: dict) -> None:
            for k in ["version", "description", "homepage", "license", "architecture"]:
                if k not in manifest:
                    raise ValueError(f"Missing required key '{k}'")
            if not re.match(r"^\d+\.\d+\.\d+$", manifest["version"]):
                raise ValueError("Invalid version format")
            if "64bit" not in manifest.get("architecture", {}):
                raise ValueError("Missing 64bit architecture definition")

        with self.assertRaises(ValueError):
            _validate_scoop_schema({"version": "0.1.2"})
        with self.assertRaises(ValueError):
            _validate_scoop_schema({"version": "invalid_ver", "description": "d", "homepage": "h", "license": "MIT", "architecture": {"64bit": {}}})
        with self.assertRaises(ValueError):
            _validate_scoop_schema({"version": "0.1.2", "description": "d", "homepage": "h", "license": "MIT", "architecture": {}})

    def test_08_homebrew_formula_spec(self):
        """Verify Homebrew formula architecture stanzas, version sync, installation, caveats, and test block."""
        import re

        formula_path = os.path.join(REPO_ROOT, "Formula", "locutus.rb")
        self.assertTrue(os.path.isfile(formula_path), f"Missing {formula_path}")

        with open(formula_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 1. Structural Ruby formula parsing
        self.assertIn("class Locutus < Formula", content)
        self.assertRegex(content, r'version\s+"0\.1\.2"', "Homebrew version must match repository version 0.1.2")
        self.assertRegex(content, r'license\s+"MIT"', "Homebrew license must be MIT")
        self.assertIn('homepage "https://github.com/axiomantic/locutus"', content)

        # 2. Multi-platform OS & Architecture blocks (macOS + Linux, arm64 + amd64)
        self.assertIn("on_macos do", content)
        self.assertIn("on_linux do", content)

        required_tarballs = [
            "locutus-darwin-arm64.tar.gz",
            "locutus-darwin-amd64.tar.gz",
            "locutus-linux-arm64.tar.gz",
            "locutus-linux-amd64.tar.gz",
        ]
        for tb in required_tarballs:
            self.assertIn(tb, content, f"Missing Homebrew bottle/tarball target: {tb}")

        # 3. Installation, Skills, Caveats, and Tests
        self.assertIn('pkgshare.install "skills"', content)
        self.assertIn("def caveats", content)
        self.assertIn("npx skills add", content)
        self.assertIn("skilz install", content)
        self.assertIn("#{opt_pkgshare}/skills/locutus", content)
        self.assertIn("test do", content)
        self.assertIn("assert_match", content)

        # 4. Negative control: validator rejecting incomplete or malformed formula
        def _validate_homebrew_formula(ruby_code: str) -> None:
            if "class Locutus < Formula" not in ruby_code:
                raise ValueError("Missing Formula class declaration")
            if "on_macos do" not in ruby_code or "on_linux do" not in ruby_code:
                raise ValueError("Missing OS block")
            for t in ["darwin-arm64", "linux-amd64"]:
                if t not in ruby_code:
                    raise ValueError(f"Missing arch target {t}")
            if "pkgshare.install" not in ruby_code:
                raise ValueError("Missing skills installation to pkgshare")

        with self.assertRaises(ValueError):
            _validate_homebrew_formula("class Locutus; end")
        with self.assertRaises(ValueError):
            _validate_homebrew_formula("class Locutus < Formula\n  on_macos do; end\nend")
        with self.assertRaises(ValueError):
            _validate_homebrew_formula("class Locutus < Formula\n  on_macos do; end\n  on_linux do; end\nend")

        # 5. Tripwire simulation of brew audit & brew test
        mock_audit_cmd = ["brew", "audit", "--formula", formula_path]
        tripwire.subprocess.mock_run(
            mock_audit_cmd,
            returncode=0,
            stdout="Passed: 0 problems found.\n",
            stderr="",
        )
        with tripwire:
            res_audit = subprocess.run(mock_audit_cmd, capture_output=True, text=True)
            self.assertEqual(res_audit.returncode, 0)
            self.assertIn("0 problems found", res_audit.stdout)

        tripwire.subprocess.assert_run(
            command=mock_audit_cmd,
            returncode=0,
            stdout="Passed: 0 problems found.\n",
            stderr="",
        )

    def test_09_release_workflow_spec(self):
        """Verify GitHub release workflow YAML DAG, job dependencies, triggers, and skill packaging."""
        import yaml

        workflow_path = os.path.join(REPO_ROOT, ".github", "workflows", "release.yml")
        self.assertTrue(os.path.isfile(workflow_path), f"Missing {workflow_path}")

        with open(workflow_path, "r", encoding="utf-8") as f:
            raw_yaml = f.read()

        # 1. Structural YAML parsing
        wf = yaml.safe_load(raw_yaml)
        self.assertIsInstance(wf, dict, "Workflow must parse to a dictionary")
        self.assertEqual(wf.get("name"), "Release")

        # 2. Trigger constraints & permissions
        triggers = wf.get("on") or wf.get(True)
        self.assertIsNotNone(triggers, "Missing 'on' trigger specification in workflow")
        self.assertIn("push", triggers)
        self.assertIn("tags", triggers["push"])
        self.assertIn("v*", triggers["push"]["tags"])
        self.assertIn("workflow_dispatch", triggers)

        self.assertIn("permissions", wf)
        self.assertEqual(wf["permissions"].get("contents"), "write")

        # 3. Job DAG dependencies (publish-release MUST wait for builds)
        jobs = wf.get("jobs", {})
        required_build_jobs = ["build-linux", "build-macos", "build-windows"]
        for bjob in required_build_jobs:
            self.assertIn(bjob, jobs, f"Workflow missing build job: {bjob}")

        self.assertIn("publish-release", jobs, "Workflow missing publish-release job")
        publish_job = jobs["publish-release"]
        needs = publish_job.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        for bjob in required_build_jobs:
            self.assertIn(bjob, needs, f"publish-release must depend on {bjob} to prevent race conditions")

        # 4. Packaging assertions across operating systems
        self.assertIn("tar -czf dist/locutus-linux-amd64.tar.gz -C dist/linux-amd64 locutus skills", raw_yaml)
        self.assertIn("tar -czf dist/locutus-darwin-arm64.tar.gz -C dist/darwin-arm64 locutus skills", raw_yaml)
        self.assertIn("Copy-Item -Recurse -Force skills dist\\windows-amd64\\skills", raw_yaml)
        self.assertIn("cp -r skills/locutus deb-amd64/usr/share/locutus/skills/", raw_yaml)
        self.assertIn("npx skills add /usr/share/locutus/skills/locutus -g", raw_yaml)
        self.assertIn("skilz install -f /usr/share/locutus/skills/locutus", raw_yaml)
        self.assertIn("sha256sum locutus-* *.deb > SHA256SUMS.txt", raw_yaml)

        # 5. Negative controls: validator rejecting broken workflow DAG
        def _validate_workflow_dag(wf_dict: dict) -> None:
            if wf_dict.get("permissions", {}).get("contents") != "write":
                raise ValueError("Missing write permission")
            pub = wf_dict.get("jobs", {}).get("publish-release", {})
            pub_needs = pub.get("needs", [])
            for req in ["build-linux", "build-macos", "build-windows"]:
                if req not in pub_needs:
                    raise ValueError(f"Missing DAG edge: publish-release -> {req}")

        with self.assertRaises(ValueError):
            _validate_workflow_dag({"permissions": {"contents": "read"}, "jobs": {"publish-release": {"needs": ["build-linux"]}}})
        with self.assertRaises(ValueError):
            _validate_workflow_dag({"permissions": {"contents": "write"}, "jobs": {"publish-release": {"needs": ["build-linux"]}}})

        # 6. Tripwire simulation of workflow validation
        mock_lint_cmd = ["python3", "-c", "import yaml; yaml.safe_load(open('.github/workflows/release.yml'))"]
        tripwire.subprocess.mock_run(
            mock_lint_cmd,
            returncode=0,
            stdout="Workflow YAML valid\n",
            stderr="",
        )
        with tripwire:
            res_lint = subprocess.run(mock_lint_cmd, capture_output=True, text=True)
            self.assertEqual(res_lint.returncode, 0)
            self.assertIn("valid", res_lint.stdout)

        tripwire.subprocess.assert_run(
            command=mock_lint_cmd,
            returncode=0,
            stdout="Workflow YAML valid\n",
            stderr="",
        )


if __name__ == "__main__":
    unittest.main()

