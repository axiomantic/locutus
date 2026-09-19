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

        # Listen should drop forged message to stderr and return empty stdout
        listen_res = self.run_locutus(["listen", agent, "1"])
        self.assertEqual(listen_res.returncode, 0)
        self.assertEqual(listen_res.stdout.strip(), "")
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

        # agent_dev should NOT receive it (empty output on timeout)
        res_dev = self.run_locutus(["listen", "agent_dev", "1"])
        self.assertEqual(res_dev.returncode, 0)
        self.assertEqual(res_dev.stdout.strip(), "")

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
        self.assertEqual(listen_res.stdout.strip(), "")
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
        self.assertEqual(listen_res.stdout.strip(), "")
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
            self.assertEqual(os.path.normpath(data["secret_file"]["value"]), os.path.normpath(os.path.expanduser("~/.config/locutus/secret")))

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
            self.assertEqual(res_listen.stdout.strip(), "")
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

        # Work on now-empty queue with 1s timeout returns empty stdout
        res_empty = self.run_locutus(["work", qname, "1"])
        self.assertEqual(res_empty.returncode, 0)
        self.assertEqual(res_empty.stdout.strip(), "")

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

        # Test sub timeout on silent channel (returns 0 bytes stdout)
        res_silent = self.run_locutus(["sub", "silent_channel_empty", "1"])
        self.assertEqual(res_silent.returncode, 0)
        self.assertEqual(res_silent.stdout.strip(), "")

    def test_31_who_flags_and_json(self):
        """Test 'locutus who' supports -a, --all, and --json output."""
        agent = "who_test_bot"
        self.run_locutus(["open", agent, "backend,testgroup"])

        try:
            # 1. Test -a flag (cluster-wide view)
            res_a = self.run_locutus(["who", "-a"])
            self.assertEqual(res_a.returncode, 0)
            self.assertIn(agent, res_a.stdout)
            self.assertIn("ACTIVE", res_a.stdout)

            # 2. Test --all flag
            res_all = self.run_locutus(["who", "--all"])
            self.assertEqual(res_all.returncode, 0)
            self.assertIn(agent, res_all.stdout)

            # 3. Test --json flag
            res_json = self.run_locutus(["who", "--json", "-a"])
            self.assertEqual(res_json.returncode, 0)
            agents = json.loads(res_json.stdout.strip())
            self.assertIsInstance(agents, list)
            match = [a for a in agents if a["agent"] == agent]
            self.assertEqual(len(match), 1)
            self.assertEqual(match[0]["status"], "ACTIVE")
            self.assertIn("backend", match[0]["tags"])
            self.assertIn("testgroup", match[0]["tags"])
        finally:
            self.run_locutus(["close", agent])

    def test_32_listen_auto_registers_in_directory(self):
        """Test that listening with an agent name auto-registers it in the active directory."""
        agent = "auto_listener_bot"
        self.run_locutus(["close", agent])

        # Send a message to the agent while it is offline
        self.run_locutus(["send", "--to", agent, "--subject", "Wake", "--body", "Wakeup"])

        # Agent listens for 1 second (consuming the message)
        res_listen = self.run_locutus(["listen", agent, "1"])
        self.assertEqual(res_listen.returncode, 0)

        # Verify agent is immediately visible as ACTIVE in locutus who
        res_who = self.run_locutus(["who", "-a", "--json"])
        self.assertEqual(res_who.returncode, 0)
        agents = json.loads(res_who.stdout.strip())
        match = [a for a in agents if a["agent"] == agent]
        self.assertEqual(len(match), 1, f"Expected {agent} to be registered in who output")
        self.assertEqual(match[0]["status"], "ACTIVE")

        self.run_locutus(["close", agent])

    def test_33_multi_agent_workspace_and_env_isolation(self):
        """Test that workspace-scoped .locutus.agent and LOCUTUS_AGENT_NAME isolate agents on the same host."""
        tmp1 = tempfile.mkdtemp(prefix="locutus_ws1_")
        tmp2 = tempfile.mkdtemp(prefix="locutus_ws2_")

        try:
            # 1. Open agent 1 in directory 1
            res1 = self.run_locutus(["open", "agent_one_ws", "teamA"], cwd=tmp1)
            self.assertEqual(res1.returncode, 0)

            # 2. Open agent 2 in directory 2
            res2 = self.run_locutus(["open", "agent_two_ws", "teamB"], cwd=tmp2)
            self.assertEqual(res2.returncode, 0)

            # 3. In dir1, listen with 1s timeout without passing name - must pick up agent_one_ws
            # Send message to agent_one_ws
            self.run_locutus(["send", "--to", "agent_one_ws", "--subject", "Dir1", "--body", "Payload1"])
            listen1 = self.run_locutus(["listen", "2"], cwd=tmp1)
            self.assertEqual(listen1.returncode, 0)
            data1 = json.loads(listen1.stdout.strip())
            self.assertEqual(data1["to"], "agent_one_ws")
            self.assertEqual(data1["body"], "Payload1")

            # 4. In dir2, listen with 1s timeout without passing name - must pick up agent_two_ws
            self.run_locutus(["send", "--to", "agent_two_ws", "--subject", "Dir2", "--body", "Payload2"])
            listen2 = self.run_locutus(["listen", "2"], cwd=tmp2)
            self.assertEqual(listen2.returncode, 0)
            data2 = json.loads(listen2.stdout.strip())
            self.assertEqual(data2["to"], "agent_two_ws")
            self.assertEqual(data2["body"], "Payload2")

            # 5. LOCUTUS_AGENT_NAME env var overrides workspace directory file
            env_override = {"LOCUTUS_AGENT_NAME": "agent_override_env"}
            self.run_locutus(["send", "--to", "agent_override_env", "--subject", "Env", "--body", "EnvPayload"])
            listen_env = self.run_locutus(["listen", "2"], cwd=tmp1, env_overrides=env_override)
            self.assertEqual(listen_env.returncode, 0)
            data_env = json.loads(listen_env.stdout.strip())
            self.assertEqual(data_env["to"], "agent_override_env")

        finally:
            shutil.rmtree(tmp1, ignore_errors=True)
            shutil.rmtree(tmp2, ignore_errors=True)
            self.run_locutus(["close", "agent_one_ws"])
            self.run_locutus(["close", "agent_two_ws"])
            self.run_locutus(["close", "agent_override_env"])

    def test_34_listen_requires_agent_identity(self):
        """Test that calling 'locutus listen' without an agent name or configured identity fails fast."""
        clean_dir = tempfile.mkdtemp(prefix="locutus_clean_ws_")
        clean_env = {
            "LOCUTUS_AGENT_NAME": "",
            "A2A_NAME": "",
            "MY_NAME": "",
            "HOME": clean_dir,
            "USERPROFILE": clean_dir,
        }
        try:
            res = self.run_locutus(["listen", "1"], cwd=clean_dir, env_overrides=clean_env)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("No agent name specified", res.stderr)
        finally:
            shutil.rmtree(clean_dir, ignore_errors=True)

    def test_35_version_flags(self):
        """Test locutus --version, -v, and version subcommand output exact semantic version."""
        for flag in [["--version"], ["-v"], ["version"]]:
            res = self.run_locutus(flag)
            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.stdout.strip(), "locutus 0.1.1")

    def test_36_listen_default_blocks_silently(self):
        """Test that 'locutus listen <agent>' with no timeout blocks silently and receives messages."""
        agent = "silent_listener_bot"
        self.run_locutus(["open", agent, "testing"])

        received = []
        errors = []

        def listener_worker():
            try:
                # No timeout specified -> blocks indefinitely until message arrives
                res = self.run_locutus(["listen", agent])
                if res.returncode == 0 and res.stdout.strip():
                    received.append(json.loads(res.stdout))
                else:
                    errors.append(f"Unexpected listener exit: rc={res.returncode}, out='{res.stdout}', err='{res.stderr}'")
            except Exception as e:
                errors.append(str(e))

        t = threading.Thread(target=listener_worker)
        t.start()

        # Give it a moment to enter the blocking Redis wait
        time.sleep(0.5)

        # Still alive and waiting
        self.assertTrue(t.is_alive())
        self.assertEqual(len(received), 0)

        # Send a message to wake it up
        res_send = self.run_locutus([
            "send",
            "--to", agent,
            "--subject", "Zero Token Wakeup",
            "--body", "Payload delivered cleanly"
        ])
        self.assertEqual(res_send.returncode, 0)

        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "Listener should have exited upon receiving message")
        self.assertEqual(len(errors), 0, f"Listener encountered errors: {errors}")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["subject"], "Zero Token Wakeup")
        self.assertEqual(received[0]["body"], "Payload delivered cleanly")

        # Now test that explicit timeout on empty inbox returns 0 bytes cleanly
        res_empty = self.run_locutus(["listen", agent, "1"])
        self.assertEqual(res_empty.returncode, 0)
        self.assertEqual(res_empty.stdout.strip(), "")

        self.run_locutus(["close", agent])

    def test_37_send_with_listen_piggyback(self):
        """Test 'locutus send ... --listen' sends to recipient and immediately blocks on sender's inbox."""
        alice = "alice_piggyback"
        bob = "bob_piggyback"
        self.run_locutus(["open", alice, "dev"])
        self.run_locutus(["open", bob, "dev"])

        alice_res = []
        alice_err = []

        def alice_sender_listener():
            try:
                # Alice sends task to Bob and immediately listens for reply on her inbox
                res = self.run_locutus([
                    "send",
                    "--to", bob,
                    "--from", alice,
                    "--subject", "Task for Bob",
                    "--body", "Compute hash",
                    "--listen"
                ])
                alice_res.append(res)
            except Exception as e:
                alice_err.append(str(e))

        t = threading.Thread(target=alice_sender_listener)
        t.start()

        # Allow Alice to send and enter listener loop
        time.sleep(0.5)
        self.assertTrue(t.is_alive(), "Alice should be blocked waiting for incoming reply")

        # Bob drains his inbox
        bob_drain = self.run_locutus(["drain", "1", bob])
        self.assertEqual(bob_drain.returncode, 0)
        bob_msg = json.loads(bob_drain.stdout.strip())
        self.assertEqual(bob_msg["from"], alice)
        self.assertEqual(bob_msg["subject"], "Task for Bob")
        self.assertEqual(bob_msg["body"], "Compute hash")

        # Bob replies to Alice
        bob_reply = self.run_locutus([
            "send",
            "--to", alice,
            "--from", bob,
            "--type", "reply",
            "--subject", "Re: Task for Bob",
            "--body", "hash_result_abcdef"
        ])
        self.assertEqual(bob_reply.returncode, 0)

        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "Alice should have unblocked upon receiving Bob's reply")
        self.assertEqual(len(alice_err), 0)
        self.assertEqual(len(alice_res), 1)
        res_a = alice_res[0]
        self.assertEqual(res_a.returncode, 0)

        # Verify stdout is clean JSON and contains ONLY the reply message (no send confirmation noise)
        data = json.loads(res_a.stdout.strip())
        self.assertEqual(data["from"], bob)
        self.assertEqual(data["to"], alice)
        self.assertEqual(data["subject"], "Re: Task for Bob")
        self.assertEqual(data["body"], "hash_result_abcdef")

        # Verify stderr logged the send status and listener transition
        self.assertIn("[LOCUTUS BUS] Message sent to bob_piggyback", res_a.stderr)

        self.run_locutus(["close", alice])
        self.run_locutus(["close", bob])

    def test_38_reply_command_with_listen(self):
        """Test 'locutus reply ... --listen' sends type=reply with reply_to and blocks cleanly."""
        carol = "carol_worker"
        dave = "dave_worker"
        self.run_locutus(["open", carol, "team"])
        self.run_locutus(["open", dave, "team"])

        dave_res = []
        dave_err = []

        def dave_reply_listener():
            try:
                # Dave replies to Carol with --reply-to and --listen
                res = self.run_locutus([
                    "reply",
                    "--to", carol,
                    "--from", dave,
                    "--reply-to", "req_msg_999",
                    "--subject", "Task Complete",
                    "--body", "Built successfully",
                    "--listen"
                ])
                dave_res.append(res)
            except Exception as e:
                dave_err.append(str(e))

        t = threading.Thread(target=dave_reply_listener)
        t.start()

        time.sleep(0.5)
        self.assertTrue(t.is_alive(), "Dave should be blocked waiting for subsequent message")

        # Carol drains her inbox to verify reply structure
        carol_drain = self.run_locutus(["drain", "1", carol])
        self.assertEqual(carol_drain.returncode, 0)
        rep = json.loads(carol_drain.stdout.strip())
        self.assertEqual(rep["type"], "reply")
        self.assertEqual(rep["from"], dave)
        self.assertEqual(rep["to"], carol)
        self.assertEqual(rep["reply_to"], "req_msg_999")
        self.assertEqual(rep["subject"], "Task Complete")
        self.assertEqual(rep["body"], "Built successfully")

        # Carol sends next instruction to Dave
        carol_next = self.run_locutus([
            "send",
            "--to", dave,
            "--from", carol,
            "--subject", "Next Task",
            "--body", "Run deploy"
        ])
        self.assertEqual(carol_next.returncode, 0)

        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "Dave should have unblocked upon receiving Carol's next message")
        self.assertEqual(len(dave_err), 0)
        self.assertEqual(len(dave_res), 1)
        res_d = dave_res[0]
        self.assertEqual(res_d.returncode, 0)

        # Dave's stdout must be clean JSON
        next_msg = json.loads(res_d.stdout.strip())
        self.assertEqual(next_msg["from"], carol)
        self.assertEqual(next_msg["subject"], "Next Task")
        self.assertEqual(next_msg["body"], "Run deploy")

        # Verify timeout mode on send/reply with --listen-timeout
        timeout_res = self.run_locutus([
            "reply",
            "--to", carol,
            "--from", dave,
            "--subject", "Quick reply",
            "--body", "ack",
            "--listen-timeout", "1"
        ])
        self.assertEqual(timeout_res.returncode, 0)
        # Empty inbox after 1s should return 0 bytes stdout
        self.assertEqual(timeout_res.stdout.strip(), "")

        self.run_locutus(["close", carol])
        self.run_locutus(["close", dave])

    def test_39_prevent_stacked_listeners_piggyback(self):
        """Verify that 'send --listen' detects an existing active listener and does not stack duplicate listeners."""
        alice = "alice_stack_guard"
        bob = "bob_stack_guard"
        self.run_locutus(["open", alice, "dev"])
        self.run_locutus(["open", bob, "dev"])

        bob_received = []
        bob_err = []

        def original_listener():
            try:
                res = self.run_locutus(["listen", bob])
                if res.returncode == 0 and res.stdout.strip():
                    bob_received.append(json.loads(res.stdout.strip()))
                else:
                    bob_err.append(f"Unexpected exit: rc={res.returncode}, err={res.stderr}")
            except Exception as e:
                bob_err.append(str(e))

        t = threading.Thread(target=original_listener)
        t.start()

        # Give original listener time to claim the lock and block on BRPOP
        time.sleep(0.5)
        self.assertTrue(t.is_alive(), "Original listener should be actively waiting")

        # Now Bob calls send with --listen (e.g. intermediate status dispatch)
        # Because a listener is already active on Bob's inbox, this MUST NOT stack another listener
        send_res = self.run_locutus([
            "send",
            "--to", alice,
            "--from", bob,
            "--subject", "Intermediate update",
            "--body", "Processing chunk 1",
            "--listen"
        ])
        # Assert send succeeded and returned immediately (code 0) without hanging
        self.assertEqual(send_res.returncode, 0)
        self.assertIn("Active listener already running", send_res.stderr)
        self.assertIn("skipping duplicate listener", send_res.stderr)

        # Alice drains and confirms she received the message
        alice_drain = self.run_locutus(["drain", "1", alice])
        self.assertEqual(alice_drain.returncode, 0)
        a_msg = json.loads(alice_drain.stdout.strip())
        self.assertEqual(a_msg["subject"], "Intermediate update")
        self.assertEqual(a_msg["body"], "Processing chunk 1")

        # Original listener is still alive and waiting
        self.assertTrue(t.is_alive(), "Original listener should still be alive")

        # Now Alice replies to Bob, which should wake the original listener cleanly
        self.run_locutus([
            "send",
            "--to", bob,
            "--from", alice,
            "--subject", "Ack update",
            "--body", "Proceed to chunk 2"
        ])

        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "Original listener should have completed upon receiving message")
        self.assertEqual(len(bob_err), 0, f"Errors: {bob_err}")
        self.assertEqual(len(bob_received), 1)
        self.assertEqual(bob_received[0]["subject"], "Ack update")
        self.assertEqual(bob_received[0]["body"], "Proceed to chunk 2")

        self.run_locutus(["close", alice])
        self.run_locutus(["close", bob])

    def test_40_standalone_listen_rejects_duplicate(self):
        """Verify that running 'locutus listen' when one is already active fails with code 1, unless --force is used."""
        carol = "carol_dup_test"
        self.run_locutus(["open", carol, "dev"])

        stop_event = threading.Event()
        def background_listener():
            self.run_locutus(["listen", carol, "10"])

        t = threading.Thread(target=background_listener)
        t.start()
        time.sleep(0.5)

        # Attempt duplicate standalone listen without --force
        dup_res = self.run_locutus(["listen", carol, "2"])
        self.assertNotEqual(dup_res.returncode, 0)
        self.assertIn("Listener already active", dup_res.stderr)
        self.assertIn("Refusing to start duplicate listener", dup_res.stderr)

        # Standalone listen WITH --force bypasses the guard
        force_res = self.run_locutus(["listen", carol, "1", "--force"])
        self.assertEqual(force_res.returncode, 0)
        self.assertEqual(force_res.stdout.strip(), "")

        t.join(timeout=12)
        self.run_locutus(["close", carol])

    def test_41_stale_listener_self_healing(self):
        """Verify that a stale listener lock with a dead local PID is detected, cleared, and self-healed."""
        dave = "dave_stale_test"
        self.run_locutus(["open", dave, "dev"])

        # Determine hostname
        import socket
        host = socket.gethostname()

        # Inject fake stale listener lock with non-existent PID 9999999
        stale_record = json.dumps({"pid": 9999999, "host": host, "started": int(time.time())})
        subprocess.run(
            ["redis-cli", "-u", REDIS_URL, "SET", f"{TEST_PREFIX}listener:{dave}", stale_record, "EX", "150"],
            capture_output=True,
            check=True
        )

        # Run listen with 1s timeout - must detect dead PID, clear lock, and listen successfully
        res = self.run_locutus(["listen", dave, "1"])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "")

        # Verify key was cleaned up upon exit
        chk = subprocess.run(
            ["redis-cli", "-u", REDIS_URL, "GET", f"{TEST_PREFIX}listener:{dave}"],
            capture_output=True,
            text=True,
            check=True
        )
        self.assertEqual(chk.stdout.strip(), "")

        self.run_locutus(["close", dave])

    def test_42_listener_exit_does_not_delete_foreign_lock(self):
        """Verify that an exiting listener does not delete a replacement listener's lock if PID/host differ."""
        agent = "preempt_listener_test"
        self.run_locutus(["open", agent, "dev"])

        import socket
        host = socket.gethostname()

        # 1. Start listener in background with 2s timeout
        def bg_listen():
            self.run_locutus(["listen", agent, "2"])

        t = threading.Thread(target=bg_listen)
        t.start()
        time.sleep(0.4)

        # 2. Simulate preemption or replacement: overwrite lock with another PID (e.g. 88888)
        foreign_lock = json.dumps({"pid": 88888, "host": host, "started": int(time.time())})
        subprocess.run(
            ["redis-cli", "-u", REDIS_URL, "SET", f"{TEST_PREFIX}listener:{agent}", foreign_lock, "EX", "150"],
            check=True
        )

        # 3. Wait for original listener to exit after its 2s timeout
        t.join(timeout=5)
        self.assertFalse(t.is_alive())

        # 4. Crucial: The replacement lock with PID 88888 MUST still be present in Redis!
        chk = subprocess.run(
            ["redis-cli", "-u", REDIS_URL, "GET", f"{TEST_PREFIX}listener:{agent}"],
            capture_output=True,
            text=True,
            check=True
        )
        self.assertIn("88888", chk.stdout, "Exiting listener erroneously deleted foreign listener lock!")

        self.run_locutus(["close", agent])

    def test_43_worker_heartbeat_renewal(self):
        """Verify that 'locutus work' registers and refreshes worker heartbeat while waiting on queue."""
        worker = "queue_worker_bot"
        queue = "long_wait_queue"
        self.run_locutus(["open", worker, "workers"])

        received = []
        def worker_loop():
            # Wait for up to 3 seconds
            res = self.run_locutus(["work", queue, "3"], env_overrides={"LOCUTUS_AGENT_NAME": worker})
            if res.returncode == 0 and res.stdout.strip():
                received.append(json.loads(res.stdout.strip()))

        t = threading.Thread(target=worker_loop)
        t.start()
        time.sleep(0.5)

        # Verify worker is ACTIVE in directory while waiting on work queue
        res_who = self.run_locutus(["who", "-a", "--json"])
        self.assertEqual(res_who.returncode, 0)
        agents = json.loads(res_who.stdout.strip())
        match = [a for a in agents if a["agent"] == worker]
        self.assertEqual(len(match), 1, f"Expected {worker} to be active in who output while working")
        self.assertEqual(match[0]["status"], "ACTIVE")

        # Send work task to complete the test
        self.run_locutus(["enqueue", queue, "--subject", "Job 1", "--body", "Payload 1"])
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["subject"], "Job 1")

        self.run_locutus(["close", worker])


if __name__ == "__main__":
    unittest.main()



