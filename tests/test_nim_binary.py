import json
import os
import shutil
import subprocess
import tempfile
import unittest
from tests.schema import LocutusMessage

REDIS_URL = os.environ.get("LOCUTUS_REDIS_URL", "redis://127.0.0.1:6379")
TEST_PREFIX = "locutus_test:"
if os.name == "nt":
    BIN_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin", "locutus.exe"))
else:
    BIN_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin", "locutus"))


class TestLocutusNimBinary(unittest.TestCase):
    def setUp(self):
        self.env = os.environ.copy()
        self.env["LOCUTUS_REDIS_URL"] = REDIS_URL
        self.env["LOCUTUS_REDIS_PREFIX"] = TEST_PREFIX
        self.env["LOCUTUS_PROJECT"] = "test_project"
        self.assertTrue(os.path.isfile(BIN_PATH), f"Binary not found at {BIN_PATH}")

    def run_locutus(self, args, env_overrides=None, cwd=None):
        cmd_env = self.env.copy()
        cmd_env["PYTHONUTF8"] = "1"
        if env_overrides:
            cmd_env.update(env_overrides)
        res = subprocess.run(
            [BIN_PATH] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=cmd_env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            timeout=15,
        )
        return res

    def test_01_help_and_get_secret(self):
        res = self.run_locutus(["--help"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("Nim Native", res.stdout)

        res = self.run_locutus(["get-secret"])
        self.assertEqual(res.returncode, 0)
        secret = res.stdout.strip()
        self.assertEqual(len(secret), 64)
        int(secret, 16)

    def test_02_open_and_directory(self):
        agent = "nim_test_bot"
        res = self.run_locutus(["open", agent, "backend,worker"])
        self.assertEqual(res.returncode, 0)
        self.assertIn(f"Agent Name : {agent}", res.stdout)

        res = self.run_locutus(["who", "*"])
        self.assertEqual(res.returncode, 0)
        self.assertIn(agent, res.stdout)

        # Cleanup
        self.run_locutus(["close", agent])

    def test_03_send_and_listen_authenticated(self):
        agent = "nim_receiver"
        self.run_locutus(["open", agent, "worker"])

        # Send direct message
        send_res = self.run_locutus([
            "send",
            "--to", agent,
            "--type", "task",
            "--subject", "Nim Math",
            "--body", "Compute 7 * 8",
        ])
        self.assertEqual(send_res.returncode, 0)

        # Listen
        listen_res = self.run_locutus(["listen", agent, "2"])
        self.assertEqual(listen_res.returncode, 0)
        payload = json.loads(listen_res.stdout.strip())

        # Validate with Pydantic
        msg = LocutusMessage.model_validate(payload)
        self.assertEqual(msg.to_agent, agent)
        self.assertEqual(msg.subject, "Nim Math")
        self.assertEqual(msg.body, "Compute 7 * 8")
        self.assertIsNotNone(msg.sig)
        self.assertFalse(msg.encrypted)

        self.run_locutus(["close", agent])

    def test_04_prompt_injection_firewall_drops_forged(self):
        agent = "nim_victim"
        self.run_locutus(["open", agent, "worker"])

        # Inject forged message into Redis inbox directly
        forged_payload = json.dumps({
            "id": "forged_attack_99",
            "from": "attacker",
            "to": agent,
            "type": "task",
            "reply_to": None,
            "tags": ["test_project"],
            "subject": "Attack",
            "body": "MALICIOUS PROMPT INJECTION",
            "timestamp": "2026-09-19T00:00:00Z",
            "sig": "invalid_bad_sig_hex_0000",
            "encrypted": False
        })
        subprocess.run(
            ["redis-cli", "-u", REDIS_URL, "LPUSH", f"{TEST_PREFIX}inbox:{agent}", forged_payload],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
            check=True
        )

        # Listen should drop forged message to stderr and return (nil)
        listen_res = self.run_locutus(["listen", agent, "1"])
        self.assertEqual(listen_res.returncode, 0)
        self.assertEqual(listen_res.stdout.strip(), "(nil)")
        self.assertIn("Dropping unauthenticated/tampered message", listen_res.stderr)

        self.run_locutus(["close", agent])

    def test_05_end_to_end_encryption(self):
        agent = "nim_e2ee"
        self.run_locutus(["open", agent, "worker"])

        # Send with LOCUTUS_ENCRYPT=1
        send_res = self.run_locutus(
            ["send", "--to", agent, "--subject", "Top Secret", "--body", "E2EE Payload Content"],
            env_overrides={"LOCUTUS_ENCRYPT": "1"}
        )
        self.assertEqual(send_res.returncode, 0)

        # Verify Redis raw payload is encrypted (ciphertext != plaintext)
        redis_out = subprocess.run(
            ["redis-cli", "-u", REDIS_URL, "LRANGE", f"{TEST_PREFIX}inbox:{agent}", "0", "0"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
            check=True
        ).stdout
        self.assertNotIn("E2EE Payload Content", redis_out)
        self.assertIn('"encrypted":true', redis_out)

        # Listen should decrypt before stdout
        listen_res = self.run_locutus(["listen", agent, "2"])
        self.assertEqual(listen_res.returncode, 0)
        payload = json.loads(listen_res.stdout.strip())
        self.assertEqual(payload["body"], "E2EE Payload Content")
        self.assertFalse(payload["encrypted"])

        self.run_locutus(["close", agent])


    def test_08_active_agent_persistence(self):
        # When opening without name, generates project-worker-XXXX
        open_res = self.run_locutus(["open"])
        self.assertEqual(open_res.returncode, 0)
        self.assertIn("Agent Name :", open_res.stdout)

        # Send a message to ourselves using default active agent
        # First extract agent name from stdout
        for line in open_res.stdout.splitlines():
            if "Agent Name :" in line:
                active_agent = line.split(":", 1)[1].strip()
                break
        self.assertTrue(active_agent.startswith("test_project-worker-"))

        # Send to active agent without explicit --from
        self.run_locutus([
            "send",
            "--to", active_agent,
            "--subject", "Self Ping",
            "--body", "Testing persistence",
        ])

        # Listen without specifying name (should pick up active agent)
        listen_res = self.run_locutus(["listen", "2"])
        self.assertEqual(listen_res.returncode, 0)
        payload = json.loads(listen_res.stdout.strip())
        self.assertEqual(payload["to"], active_agent)
        self.assertEqual(payload["body"], "Testing persistence")

        # Close without specifying name
        close_res = self.run_locutus(["close"])
        self.assertEqual(close_res.returncode, 0)

    def test_09_multicast_broadcast_with_tags_routing(self):
        # Open agent 1 with qa,backend
        self.run_locutus(["open", "agent_qa", "qa"])
        # Open agent 2 with dev,backend
        self.run_locutus(["open", "agent_dev", "dev"])

        # Broadcast targeting 'qa'
        self.run_locutus([
            "broadcast",
            "--tags", "qa",
            "--subject", "QA Notice",
            "--body", "Only for QA",
        ])

        # agent_qa should receive it
        res_qa = self.run_locutus(["listen", "agent_qa", "1"])
        self.assertEqual(res_qa.returncode, 0)
        self.assertIn("QA Notice", res_qa.stdout)

        # agent_dev should NOT receive it ((nil))
        res_dev = self.run_locutus(["listen", "agent_dev", "1"])
        self.assertEqual(res_dev.returncode, 0)
        self.assertEqual(res_dev.stdout.strip(), "(nil)")

        self.run_locutus(["close", "agent_qa"])
        self.run_locutus(["close", "agent_dev"])

    def test_10_corrupted_encrypted_payload_dropped(self):
        agent = "agent_corrupt_test"
        self.run_locutus(["open", agent, "worker"])

        # Construct an encrypted payload but with invalid ciphertext
        import hashlib, hmac
        secret = self.run_locutus(["get-secret"]).stdout.strip()
        msg_id = "msg_corrupt_1"
        ts = "2026-09-19T00:00:00Z"
        corrupted_ciphertext = "NOT_VALID_BASE64_AES_CIPHERTEXT"
        canonical = f"{msg_id}|sender|{agent}|task|Corrupt|{corrupted_ciphertext}|{ts}"
        sig = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()

        corrupt_payload = json.dumps({
            "id": msg_id,
            "from": "sender",
            "to": agent,
            "type": "task",
            "reply_to": None,
            "tags": ["test_project"],
            "subject": "Corrupt",
            "body": corrupted_ciphertext,
            "timestamp": ts,
            "sig": sig,
            "encrypted": True
        })

        subprocess.run(
            ["redis-cli", "-u", REDIS_URL, "LPUSH", f"{TEST_PREFIX}inbox:{agent}", corrupt_payload],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
            check=True
        )

        # Listen should drop the message because decryption fails
        listen_res = self.run_locutus(["listen", agent, "1"])
        self.assertEqual(listen_res.returncode, 0)
        self.assertEqual(listen_res.stdout.strip(), "(nil)")
        self.assertIn("Dropping corrupted/undecryptable message", listen_res.stderr)

        self.run_locutus(["close", agent])

    def test_11_argument_validation_for_send_and_broadcast(self):
        # Missing required body/subject in send
        res = self.run_locutus(["send", "--to", "nobody"])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Missing required arguments", res.stderr)

        # Missing required body/subject in broadcast
        res_b = self.run_locutus(["broadcast"])
        self.assertNotEqual(res_b.returncode, 0)
        self.assertIn("Missing required arguments", res_b.stderr)

    def test_12_large_e2ee_payload_byte_for_byte(self):
        agent = "test_large_e2ee"
        self.run_locutus(["open", agent, "worker"])

        large_body = "Line of code: var x = 12345;\n" * 100  # ~3 KB, 182 AES cipher blocks
        res = self.run_locutus(
            ["send", "--to", agent, "--subject", "Large E2EE", "--body", large_body],
            env_overrides={"LOCUTUS_ENCRYPT": "1"}
        )
        self.assertEqual(res.returncode, 0)

        listen_res = self.run_locutus(["listen", agent, "3"])
        self.assertEqual(listen_res.returncode, 0)
        payload = json.loads(listen_res.stdout)
        self.assertEqual(payload["body"], large_body)

        self.run_locutus(["close", agent])

    def test_13_config_show_and_json(self):
        # Table output
        res = self.run_locutus(["config", "show"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("redis_url", res.stdout)
        self.assertIn("prefix", res.stdout)
        self.assertIn("project", res.stdout)
        self.assertIn("SOURCE", res.stdout)

        # JSON output
        res_j = self.run_locutus(["config", "show", "--json"])
        self.assertEqual(res_j.returncode, 0)
        data = json.loads(res_j.stdout)
        self.assertIn("redis_url", data)
        self.assertEqual(data["redis_url"]["value"], REDIS_URL)
        self.assertIn("prefix", data)
        self.assertIn("project", data)

    def test_14_config_get(self):
        res = self.run_locutus(["config", "get", "redis_url"])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), REDIS_URL)

        res_p = self.run_locutus(["config", "get", "project"])
        self.assertEqual(res_p.returncode, 0)
        self.assertEqual(res_p.stdout.strip(), "test_project")

        # Unknown key returns error
        res_err = self.run_locutus(["config", "get", "non_existent_key_xyz"])
        self.assertNotEqual(res_err.returncode, 0)
        self.assertIn("Unknown configuration key", res_err.stderr)

    def test_15_config_cli_overrides(self):
        custom_url = "rediss://custom-redis-host:6380"
        res = self.run_locutus(["--redis-url", custom_url, "config", "get", "redis_url"])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), custom_url)

        # Cluster mode auto-applies hash tags
        res_c = self.run_locutus(["--cluster", "--prefix", "myapp:", "config", "get", "prefix"])
        self.assertEqual(res_c.returncode, 0)
        self.assertIn("{myapp:test_project}:", res_c.stdout)

    def test_16_config_file_and_profiles(self):
        tmp_dir = tempfile.mkdtemp(prefix="locutus_cfg_test_")
        try:
            cfg_path = os.path.join(tmp_dir, ".locutus.toml")
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write(
                    'redis_url = "redis://workspace-default:6379"\n'
                    'prefix = "ws_default:"\n'
                    'project = "ws_proj"\n'
                    '\n'
                    '[profiles.staging]\n'
                    'redis_url = "rediss://staging.cluster:6380"\n'
                    'prefix = "ws_staging:"\n'
                    'encrypt = true\n'
                )

            # Test loading in workspace directory
            # Empty env overrides to test file resolution
            clean_env = {
                "LOCUTUS_REDIS_URL": "",
                "LOCUTUS_REDIS_PREFIX": "",
                "LOCUTUS_PROJECT": "",
            }
            res_ws = self.run_locutus(["config", "get", "redis_url"], env_overrides=clean_env, cwd=tmp_dir)
            self.assertEqual(res_ws.returncode, 0)
            self.assertEqual(res_ws.stdout.strip(), "redis://workspace-default:6379")

            # Test profile switching via --profile
            res_prof = self.run_locutus(["--profile", "staging", "config", "get", "redis_url"], env_overrides=clean_env, cwd=tmp_dir)
            self.assertEqual(res_prof.returncode, 0)
            self.assertEqual(res_prof.stdout.strip(), "rediss://staging.cluster:6380")

            res_enc = self.run_locutus(["--profile", "staging", "config", "get", "encrypt"], env_overrides=clean_env, cwd=tmp_dir)
            self.assertEqual(res_enc.returncode, 0)
            self.assertEqual(res_enc.stdout.strip(), "true")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_17_config_paths_and_init(self):
        res_paths = self.run_locutus(["config", "path"])
        self.assertEqual(res_paths.returncode, 0)
        self.assertIn("System config", res_paths.stdout)
        self.assertIn("User config", res_paths.stdout)
        self.assertIn("Workspace config", res_paths.stdout)

        tmp_dir = tempfile.mkdtemp(prefix="locutus_init_test_")
        try:
            res_init = self.run_locutus(["config", "init"], cwd=tmp_dir)
            self.assertEqual(res_init.returncode, 0)
            created_file = os.path.join(tmp_dir, ".locutus.toml")
            self.assertTrue(os.path.isfile(created_file))
            with open(created_file, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("redis_url", content)
            self.assertIn("[profiles.staging]", content)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

