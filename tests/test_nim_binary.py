import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from tests.schema import LocutusMessage

REDIS_URL = os.environ.get("LOCUTUS_REDIS_URL", "redis://127.0.0.1:6379")
TEST_PREFIX = "locutus_test:"
if os.name == "nt":
    BIN_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin", "locutus.exe"))
else:
    BIN_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin", "locutus"))


import pytest

@pytest.mark.unit
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
        msg_qa = LocutusMessage.model_validate_json(res_qa.stdout.strip())
        self.assertEqual(msg_qa.to_agent, "@qa")
        self.assertEqual(msg_qa.subject, "QA Notice")
        self.assertEqual(msg_qa.body, "Only for QA")

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

    def test_18_non_json_payload_dropped(self):
        """Verify that malformed non-JSON messages in Redis are dropped to stderr and don't crash listener."""
        agent = "agent_malformed_test"
        self.run_locutus(["open", agent, "worker"])

        # Inject completely invalid non-JSON raw text
        subprocess.run(
            ["redis-cli", "-u", REDIS_URL, "LPUSH", f"{TEST_PREFIX}inbox:{agent}", "NOT_JSON_RAW_DATA_{{{"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
            check=True
        )

        listen_res = self.run_locutus(["listen", agent, "1"])
        self.assertEqual(listen_res.returncode, 0)
        self.assertEqual(listen_res.stdout.strip(), "(nil)")
        self.assertIn("Dropping non-JSON payload from inbox", listen_res.stderr)

        self.run_locutus(["close", agent])

    def test_19_unreachable_redis_error_handling(self):
        """Verify that attempting to contact an unreachable Redis instance returns a non-zero exit code."""
        # Use an unassigned port that immediately rejects connection
        res = self.run_locutus([
            "--redis-url", "redis://127.0.0.1:1",
            "send",
            "--to", "nobody",
            "--subject", "Fail",
            "--body", "Payload"
        ])
        self.assertNotEqual(res.returncode, 0)

    def test_20_tag_argument_validation(self):
        """Verify that 'locutus tag' validates required arguments and actions with non-zero exit."""
        # Missing arguments
        res_missing = self.run_locutus(["tag"])
        self.assertNotEqual(res_missing.returncode, 0)
        self.assertIn("Missing arguments for tag command", res_missing.stderr)

        # Invalid action
        res_invalid = self.run_locutus(["tag", "badaction", "mytag"])
        self.assertNotEqual(res_invalid.returncode, 0)
        self.assertIn("Invalid tag action", res_invalid.stderr)

    def test_21_drain_argument_validation(self):
        """Verify that 'locutus drain' rejects non-integer counts with non-zero exit."""
        res = self.run_locutus(["drain", "not_a_number"])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Invalid count", res.stderr)

    def test_22_crypto_tmp_cleanup_guarantee(self):
        """Verify that encrypt/decrypt operations leave zero temporary files in ~/.config/locutus/tmp/."""
        agent = "agent_crypto_cleanup_test"
        self.run_locutus(["open", agent, "worker"])

        # Send encrypted message
        res_send = self.run_locutus(
            ["send", "--to", agent, "--subject", "Cleanup Test", "--body", "Confidential Data 123"],
            env_overrides={"LOCUTUS_ENCRYPT": "1"}
        )
        self.assertEqual(res_send.returncode, 0)

        # Listen and decrypt
        res_listen = self.run_locutus(["listen", agent, "2"], env_overrides={"LOCUTUS_ENCRYPT": "1"})
        self.assertEqual(res_listen.returncode, 0)

        # Inspect tmp directory: no .tmp files should exist
        tmp_dir = os.path.expanduser("~/.config/locutus/tmp")
        if os.path.isdir(tmp_dir):
            tmp_files = [f for f in os.listdir(tmp_dir) if f.endswith(".tmp")]
            self.assertEqual(tmp_files, [], f"Leaked tmp files found in {tmp_dir}: {tmp_files}")

        self.run_locutus(["close", agent])

    def test_23_readme_example_config_comprehensive(self):
        """Fact-check: verify that the exact example .locutus.toml in README.md is supported and parsed correctly."""
        readme_example_toml = """redis_url = "redis://127.0.0.1:6379"
prefix = "locutus:"
project = "my-project"
encrypt = false
cluster = false
heartbeat_ttl = 150
message_ttl = 604800
listen_timeout = 90

# Shared secret file (avoids committing secrets into git)
secret_file = "~/.config/locutus/secret"

# Named profiles: locutus --profile staging <subcommand>
[profiles.staging]
redis_url = "rediss://staging.internal:6380"
prefix = "stg:locutus:"
encrypt = true

[profiles.prod]
redis_url = "rediss://prod-cluster.internal:6379"
cluster = true
encrypt = true
"""
        tmp_dir = tempfile.mkdtemp(prefix="locutus_readme_cfg_")
        try:
            cfg_path = os.path.join(tmp_dir, ".locutus.toml")
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write(readme_example_toml)

            clean_env = {
                "LOCUTUS_REDIS_URL": "",
                "LOCUTUS_REDIS_PREFIX": "",
                "LOCUTUS_PROJECT": "",
                "LOCUTUS_ENCRYPT": "",
                "LOCUTUS_CLUSTER": "",
            }

            # 1. Root default profile verification
            res = self.run_locutus(["config", "show", "--json"], env_overrides=clean_env, cwd=tmp_dir)
            self.assertEqual(res.returncode, 0)
            data = json.loads(res.stdout)

            self.assertEqual(data["redis_url"]["value"], "redis://127.0.0.1:6379")
            self.assertEqual(data["prefix"]["value"], "locutus:")
            self.assertEqual(data["project"]["value"], "my-project")
            self.assertEqual(data["encrypt"]["value"], "false")
            self.assertEqual(data["cluster"]["value"], "false")
            self.assertEqual(data["heartbeat_ttl"]["value"], "150")
            self.assertEqual(data["message_ttl"]["value"], "604800")
            self.assertEqual(data["listen_timeout"]["value"], "90")
            self.assertEqual(data["secret_file"]["value"], os.path.expanduser("~/.config/locutus/secret"))

            # 2. Staging profile verification
            res_stg = self.run_locutus(["--profile", "staging", "config", "show", "--json"], env_overrides=clean_env, cwd=tmp_dir)
            self.assertEqual(res_stg.returncode, 0)
            stg_data = json.loads(res_stg.stdout)
            self.assertEqual(stg_data["redis_url"]["value"], "rediss://staging.internal:6380")
            self.assertEqual(stg_data["prefix"]["value"], "stg:locutus:")
            self.assertEqual(stg_data["encrypt"]["value"], "true")

            # 3. Prod profile verification (cluster mode hashtag auto-applied)
            res_prod = self.run_locutus(["--profile", "prod", "config", "show", "--json"], env_overrides=clean_env, cwd=tmp_dir)
            self.assertEqual(res_prod.returncode, 0)
            prod_data = json.loads(res_prod.stdout)
            self.assertEqual(prod_data["redis_url"]["value"], "rediss://prod-cluster.internal:6379")
            self.assertEqual(prod_data["cluster"]["value"], "true")
            self.assertEqual(prod_data["prefix"]["value"], "{locutus:my-project}:")
            self.assertEqual(prod_data["encrypt"]["value"], "true")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_24_all_runtime_config_options_behavior(self):
        """Verify that every individual configuration option actually affects runtime operations."""
        tmp_dir = tempfile.mkdtemp(prefix="locutus_runtime_cfg_")
        custom_secret_file = os.path.join(tmp_dir, "custom_secret.key")
        custom_secret_val = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        with open(custom_secret_file, "w") as f:
            f.write(custom_secret_val + "\n")

        cfg_content = f"""redis_url = "{REDIS_URL}"
prefix = "{TEST_PREFIX}"
project = "cfg_test_proj"
agent_name = "configured_agent_99"
heartbeat_ttl = 45
message_ttl = 75
listen_timeout = 1
secret_file = "{custom_secret_file}"
"""
        try:
            cfg_path = os.path.join(tmp_dir, ".locutus.toml")
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write(cfg_content)

            clean_env = {
                "LOCUTUS_REDIS_URL": "",
                "LOCUTUS_REDIS_PREFIX": "",
                "LOCUTUS_PROJECT": "",
                "LOCUTUS_SECRET": "",
                "LOCUTUS_SECRET_FILE": "",
            }

            # A. Test agent_name and secret_file resolution
            res_agent = self.run_locutus(["config", "get", "agent_name"], env_overrides=clean_env, cwd=tmp_dir)
            self.assertEqual(res_agent.stdout.strip(), "configured_agent_99")

            res_secret = self.run_locutus(["get-secret"], env_overrides=clean_env, cwd=tmp_dir)
            self.assertEqual(res_secret.stdout.strip(), custom_secret_val)

            # B. Test heartbeat_ttl affects Redis TTL on open
            agent = "agent_heartbeat_ttl_test"
            self.run_locutus(["open", agent, "worker"], env_overrides=clean_env, cwd=tmp_dir)
            ttl_res = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "TTL", f"{TEST_PREFIX}heartbeat:{agent}"],
                capture_output=True,
                text=True,
                check=True
            )
            hb_ttl = int(ttl_res.stdout.strip())
            self.assertTrue(0 < hb_ttl <= 45, f"Expected heartbeat TTL <= 45, got {hb_ttl}")

            # C. Test message_ttl affects inbox TTL on send
            self.run_locutus(
                ["send", "--to", agent, "--subject", "TTL Check", "--body", "Checking message TTL"],
                env_overrides=clean_env,
                cwd=tmp_dir
            )
            inbox_ttl_res = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "TTL", f"{TEST_PREFIX}inbox:{agent}"],
                capture_output=True,
                text=True,
                check=True
            )
            msg_ttl = int(inbox_ttl_res.stdout.strip())
            self.assertTrue(0 < msg_ttl <= 75, f"Expected inbox TTL <= 75, got {msg_ttl}")

            # D. Test listen_timeout: drain message, then listen with empty inbox (should timeout in ~1s)
            self.run_locutus(["drain", "1", agent], env_overrides=clean_env, cwd=tmp_dir)
            start_t = time.time()
            res_listen = self.run_locutus(["listen", agent], env_overrides=clean_env, cwd=tmp_dir)
            elapsed = time.time() - start_t
            self.assertEqual(res_listen.returncode, 0)
            self.assertEqual(res_listen.stdout.strip(), "(nil)")
            self.assertTrue(elapsed < 4.0, f"Listen with listen_timeout=1 took too long: {elapsed}s")

            # E. Test inline secret configuration
            cfg_inline_secret = f"""redis_url = "{REDIS_URL}"
secret = "my_inline_secret_test_555"
"""
            cfg_inline_path = os.path.join(tmp_dir, "inline.toml")
            with open(cfg_inline_path, "w", encoding="utf-8") as f:
                f.write(cfg_inline_secret)

            res_inline = self.run_locutus(["--config", cfg_inline_path, "get-secret"], env_overrides=clean_env, cwd=tmp_dir)
            self.assertEqual(res_inline.stdout.strip(), "my_inline_secret_test_555")

            self.run_locutus(["close", agent], env_overrides=clean_env, cwd=tmp_dir)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_25_config_init_targets(self):
        """Verify that 'locutus config init' supports --project and handles existing files cleanly."""
        tmp_dir = tempfile.mkdtemp(prefix="locutus_init_targets_")
        try:
            # Test --project flag format
            res_proj = self.run_locutus(["config", "init", "--project"], cwd=tmp_dir)
            self.assertEqual(res_proj.returncode, 0)
            self.assertTrue(os.path.isfile(os.path.join(tmp_dir, ".locutus.toml")))

            # Re-running returns warning/error message without overwriting
            res_dup = self.run_locutus(["config", "init", "--project"], cwd=tmp_dir)
            self.assertIn("already exists", res_dup.stdout)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_26_status_and_directory_state(self):
        """Test 'locutus status' updates state and activity, reflected in 'locutus who'."""
        agent = "status_worker_test"
        self.run_locutus(["open", agent, "backend,worker"])

        # Update status to busy
        res_stat = self.run_locutus(["status", "busy", "Compiling LLVM", agent])
        self.assertEqual(res_stat.returncode, 0)
        self.assertEqual(res_stat.stdout.strip(), "OK")

        # Check directory
        res_who = self.run_locutus(["who", "*"])
        self.assertEqual(res_who.returncode, 0)
        self.assertIn(agent, res_who.stdout)
        self.assertIn("BUSY", res_who.stdout)
        self.assertIn("Compiling LLVM", res_who.stdout)

        # Update status to idle
        res_stat2 = self.run_locutus(["status", "idle", "Awaiting jobs", agent])
        self.assertEqual(res_stat2.returncode, 0)

        res_who2 = self.run_locutus(["who", "*"])
        self.assertEqual(res_who2.returncode, 0)
        self.assertIn(agent, res_who2.stdout)
        self.assertIn("IDLE", res_who2.stdout)
        self.assertIn("Awaiting jobs", res_who2.stdout)

        self.run_locutus(["close", agent])

    def test_27_distributed_locking(self):
        """Test 'locutus lock' acquisition, conflict rejection, and 'locutus unlock'."""
        lock_name = "deploy_mutex_test"

        # Acquire lock
        res_lock = self.run_locutus(["--agent-name=agent_lock_owner", "lock", lock_name, "10"])
        self.assertEqual(res_lock.returncode, 0)
        self.assertIn(f"LOCKED {lock_name} by agent_lock_owner", res_lock.stdout)

        # Attempt to acquire same lock with different owner -> should fail
        res_conflict = self.run_locutus(["--agent-name=intruder_agent", "lock", lock_name, "10"])
        self.assertNotEqual(res_conflict.returncode, 0)
        self.assertIn("already held", res_conflict.stderr)

        # Attempt to unlock with non-owner -> should fail
        res_unlock_bad = self.run_locutus(["--agent-name=intruder_agent", "unlock", lock_name])
        self.assertNotEqual(res_unlock_bad.returncode, 0)
        self.assertIn("not owner", res_unlock_bad.stderr)

        # Unlock with owner -> should succeed
        res_unlock = self.run_locutus(["--agent-name=agent_lock_owner", "unlock", lock_name])
        self.assertEqual(res_unlock.returncode, 0)
        self.assertIn(f"UNLOCKED {lock_name}", res_unlock.stdout)

    def test_28_task_queue_enqueue_and_work(self):
        """Test competing-consumers task queue: enqueue and work with Pydantic validation."""
        qname = "render_farm_jobs"
        subject = "Render Frame 42"
        body = "blender -b project.blend -f 42"

        # Enqueue task
        res_enq = self.run_locutus([
            "enqueue", qname,
            "--subject", subject,
            "--body", body,
            "--type", "task",
            "--from", "scheduler_agent",
        ])
        self.assertEqual(res_enq.returncode, 0)
        msg_id = res_enq.stdout.strip()
        self.assertTrue(msg_id.startswith("msg_"))

        # Work the queue
        res_work = self.run_locutus(["work", qname, "5"])
        self.assertEqual(res_work.returncode, 0)

        # Validate message schema with Pydantic
        msg = LocutusMessage.model_validate_json(res_work.stdout)
        self.assertEqual(msg.id, msg_id)
        self.assertEqual(msg.from_agent, "scheduler_agent")
        self.assertEqual(msg.to_agent, f"queue:{qname}")
        self.assertEqual(msg.subject, subject)
        self.assertEqual(msg.body, body)

        # Work on now-empty queue with 1s timeout returns (nil)
        res_empty = self.run_locutus(["work", qname, "1"])
        self.assertEqual(res_empty.returncode, 0)
        self.assertEqual(res_empty.stdout.strip(), "(nil)")

    def test_29_synchronous_request_rpc(self):
        """Test synchronous RPC 'locutus request' roundtrip between requester and responder."""
        server_agent = "rpc_server_agent"
        self.run_locutus(["open", server_agent, "rpc"])

        def server_loop():
            req_res = self.run_locutus(["listen", server_agent, "5"])
            if req_res.returncode == 0 and req_res.stdout.strip() not in ["", "(nil)"]:
                data = json.loads(req_res.stdout)
                reply_to = data.get("reply_to")
                if reply_to:
                    self.run_locutus([
                        "--agent-name=" + server_agent,
                        "send",
                        "--to", reply_to,
                        "--type", "reply",
                        "--subject", "RPC Result",
                        "--body", "answer:42"
                    ])

        t = threading.Thread(target=server_loop)
        t.start()
        time.sleep(0.3)

        # Requester calls request
        res_req = self.run_locutus([
            "--agent-name=rpc_client_agent",
            "request",
            "--to", server_agent,
            "--subject", "Math Question",
            "--body", "What is 6 * 7?",
            "--timeout", "8"
        ])
        t.join(timeout=10)

        self.assertEqual(res_req.returncode, 0)
        reply_msg = LocutusMessage.model_validate_json(res_req.stdout)
        self.assertEqual(reply_msg.body, "answer:42")
        self.assertEqual(reply_msg.subject, "RPC Result")

        # Also test --raw mode
        t2 = threading.Thread(target=server_loop)
        t2.start()
        time.sleep(0.3)

        res_raw = self.run_locutus([
            "--agent-name=rpc_client_agent",
            "request",
            "--to", server_agent,
            "--subject", "Math Question 2",
            "--body", "What is 6 * 7 again?",
            "--timeout", "8",
            "--raw"
        ])
        t2.join(timeout=10)

        self.assertEqual(res_raw.returncode, 0)
        self.assertEqual(res_raw.stdout.strip(), "answer:42")

        self.run_locutus(["close", server_agent])

    def test_30_ephemeral_pub_sub(self):
        """Test ephemeral streaming with 'locutus pub' and 'locutus sub'."""
        channel = "telemetry_test"
        message = "METRIC:cpu_temp=48C"

        received = []
        def sub_worker():
            res_sub = self.run_locutus(["sub", channel, "5"])
            if res_sub.returncode == 0:
                received.append(res_sub.stdout.strip())

        t = threading.Thread(target=sub_worker)
        t.start()
        time.sleep(0.4)

        # Publish message
        res_pub = self.run_locutus(["pub", channel, message])
        self.assertEqual(res_pub.returncode, 0)
        t.join(timeout=6)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0], message)

        # Test sub timeout on silent channel
        res_silent = self.run_locutus(["sub", "silent_channel_empty", "1"])
        self.assertEqual(res_silent.returncode, 0)
        self.assertEqual(res_silent.stdout.strip(), "(nil)")


if __name__ == "__main__":
    unittest.main()



