import base64
import hashlib
import json
import os
import shutil
import subprocess
import signal
import re
import tempfile
import threading
import time
import unittest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from tests.schema import LocutusMessage

REDIS_URL = os.environ.get("LOCUTUS_REDIS_URL", "redis://127.0.0.1:6379")
TEST_PREFIX = "locutus_test:"
if os.name == "nt":
    BIN_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin", "locutus.exe"))
else:
    BIN_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin", "locutus"))


import pytest
import tripwire
from tests.tripwire_locutus import LocutusPlugin, LocutusSchemaError

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
        # 1. Verify --help output, exit code, and exhaustive subcommand manifest
        res = self.run_locutus(["--help"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("Locutus", res.stdout)
        self.assertIn("Nim Native", res.stdout)

        expected_subcommands = [
            "version", "open", "listen", "send", "reply", "broadcast", "request",
            "scatter", "enqueue", "work", "claim", "ack", "blackboard", "floor",
            "cancel", "ballot", "leader", "workflow", "status", "lock", "unlock",
            "pub", "sub", "who", "sweep", "tag", "drain", "close", "get-secret", "config"
        ]
        for subcmd in expected_subcommands:
            self.assertIn(f"locutus {subcmd}", res.stdout, f"Subcommand '{subcmd}' missing from --help output")

        expected_global_options = [
            "--version", "--profile", "--config", "--redis-url", "--prefix", "--project", "--encrypt", "--cluster"
        ]
        for opt in expected_global_options:
            self.assertIn(opt, res.stdout, f"Global option '{opt}' missing from --help output")

        # Negative control: unknown subcommand returns exit code 1 with descriptive error
        res_unknown = self.run_locutus(["nonexistent_subcommand_test"])
        self.assertEqual(res_unknown.returncode, 1)
        self.assertIn("Unknown subcommand: nonexistent_subcommand_test", res_unknown.stdout + res_unknown.stderr)

        # 2. Verify get-secret generates valid 64-character hex secret
        res_secret = self.run_locutus(["get-secret"])
        self.assertEqual(res_secret.returncode, 0)
        secret = res_secret.stdout.strip()
        self.assertEqual(len(secret), 64, f"Secret must be 64 characters hex (32 bytes), got {len(secret)}")
        int(secret, 16)  # Verifies valid hex without ValueError

        # 3. Verify isolated secret file creation, content, and strict 0600 POSIX permissions
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_secret_file = os.path.join(tmpdir, "isolated_locutus", "custom.secret")
            res_custom = self.run_locutus(
                ["get-secret"],
                env_overrides={"LOCUTUS_SECRET_FILE": custom_secret_file, "LOCUTUS_SECRET": ""}
            )
            self.assertEqual(res_custom.returncode, 0)
            custom_secret = res_custom.stdout.strip()
            self.assertEqual(len(custom_secret), 64)
            int(custom_secret, 16)

            # Assert file exists and matches stdout exactly
            self.assertTrue(os.path.isfile(custom_secret_file), f"Secret file was not created at {custom_secret_file}")
            with open(custom_secret_file, "r") as f:
                file_content = f.read().strip()
            self.assertEqual(file_content, custom_secret, "Secret file content does not match binary stdout")

            # Assert POSIX 0600 file permissions
            if os.name != "nt":
                file_mode = os.stat(custom_secret_file).st_mode & 0o777
                self.assertEqual(file_mode, 0o600, f"Expected 0600 file permissions, got {oct(file_mode)}")

            # Assert idempotency: subsequent run returns identical secret from file
            res_second = self.run_locutus(
                ["get-secret"],
                env_overrides={"LOCUTUS_SECRET_FILE": custom_secret_file, "LOCUTUS_SECRET": ""}
            )
            self.assertEqual(res_second.returncode, 0)
            self.assertEqual(res_second.stdout.strip(), custom_secret)

    def test_02_open_and_directory(self):
        agent = "nim_test_bot"
        # Pre-cleanup in case of prior dirty state
        self.run_locutus(["close", agent])

        # 1. Register agent with specific tags
        res = self.run_locutus(["open", agent, "backend,worker"])
        self.assertEqual(res.returncode, 0)
        self.assertIn(f"Agent Name : {agent}", res.stdout)
        self.assertIn("Registered Successfully", res.stdout)

        # 2. Text directory listing
        res_who = self.run_locutus(["who", "*"])
        self.assertEqual(res_who.returncode, 0)
        self.assertIn(agent, res_who.stdout)
        self.assertIn("ACTIVE", res_who.stdout)

        # 3. JSON directory listing and strict schema validation via LocutusPlugin
        res_json = self.run_locutus(["who", "*", "--json"])
        self.assertEqual(res_json.returncode, 0)
        agents = LocutusPlugin.validate_json_schema("who", res_json.stdout)
        self.assertIsInstance(agents, list)

        # Locate registered bot in directory list
        bot = next((a for a in agents if a.get("agent") == agent or a.get("name") == agent), None)
        self.assertIsNotNone(bot, f"Agent '{agent}' missing from 'who * --json' directory output: {agents}")
        self.assertEqual(bot["status"], "ACTIVE")
        self.assertIn("backend", bot["tags"])
        self.assertIn("worker", bot["tags"])
        self.assertEqual(bot["state"], "IDLE")
        self.assertEqual(bot["activity"], "")

        # 4. Filtered query by specific matching tag
        res_filter = self.run_locutus(["who", "backend", "--json"])
        self.assertEqual(res_filter.returncode, 0)
        filter_agents = LocutusPlugin.validate_json_schema("who", res_filter.stdout)
        self.assertTrue(any(a.get("agent") == agent or a.get("name") == agent for a in filter_agents))

        # 5. Negative control: query by non-matching tag must NOT return this agent
        res_unmatched = self.run_locutus(["who", "nonexistent_custom_tag_xyz", "--json"])
        self.assertEqual(res_unmatched.returncode, 0)
        unmatched_agents = LocutusPlugin.validate_json_schema("who", res_unmatched.stdout)
        self.assertFalse(any(a.get("agent") == agent or a.get("name") == agent for a in unmatched_agents))

        # 6. Cleanup and verify departure
        res_close = self.run_locutus(["close", agent])
        self.assertEqual(res_close.returncode, 0)

    def test_03_send_and_listen_authenticated(self):
        agent = "nim_receiver"
        # Pre-cleanup
        self.run_locutus(["close", agent])
        self.run_locutus(["open", agent, "worker"])

        # 1. Send direct message
        send_res = self.run_locutus([
            "send",
            "--to", agent,
            "--type", "task",
            "--subject", "Nim Math",
            "--body", "Compute 7 * 8",
        ])
        self.assertEqual(send_res.returncode, 0)

        # 2. Listen and validate wire envelope
        listen_res = self.run_locutus(["listen", agent, "2"])
        self.assertEqual(listen_res.returncode, 0)
        envelope = LocutusPlugin.validate_wire_envelope(listen_res.stdout.strip())
        self.assertEqual(envelope["to"], agent)
        self.assertEqual(envelope.get("subject"), "Nim Math")
        self.assertEqual(envelope["body"], "Compute 7 * 8")
        self.assertEqual(envelope["type"], "task")
        self.assertTrue(bool(envelope.get("sig")), "Authenticated message must include HMAC-SHA256 signature")

        # Pydantic validation
        msg = LocutusMessage.model_validate_json(listen_res.stdout.strip())
        self.assertEqual(msg.to_agent, agent)
        self.assertEqual(msg.subject, "Nim Math")
        self.assertEqual(msg.body, "Compute 7 * 8")
        self.assertIsNotNone(msg.sig)
        self.assertFalse(msg.encrypted)

        # 3. Negative control: inject forged message with invalid signature into inbox
        forged_payload = json.dumps({
            "id": "forged_msg_003",
            "from": "attacker",
            "to": agent,
            "type": "task",
            "reply_to": None,
            "tags": ["test_project"],
            "subject": "Forged Attack",
            "body": "MALICIOUS INJECTION",
            "timestamp": "2026-09-19T00:00:00Z",
            "sig": "0000000000000000000000000000000000000000000000000000000000000000",
            "encrypted": False
        })
        subprocess.run(
            ["redis-cli", "-u", REDIS_URL, "LPUSH", f"{TEST_PREFIX}inbox:{agent}", forged_payload],
            capture_output=True,
            check=True
        )

        # Listen must reject forged message and return empty payload on timeout
        listen_forged = self.run_locutus(["listen", agent, "1"])
        self.assertEqual(listen_forged.returncode, 0)
        self.assertEqual(listen_forged.stdout.strip(), "")
        self.assertIn("Dropping unauthenticated/tampered message", listen_forged.stderr)

        # 4. Resilience verification: subsequent legitimate authenticated message is received
        send_res2 = self.run_locutus([
            "send",
            "--to", agent,
            "--type", "task",
            "--subject", "Subsequent Task",
            "--body", "Post-firewall recovery verification",
        ])
        self.assertEqual(send_res2.returncode, 0)

        listen_res2 = self.run_locutus(["listen", agent, "2"])
        self.assertEqual(listen_res2.returncode, 0)
        envelope2 = LocutusPlugin.validate_wire_envelope(listen_res2.stdout.strip())
        self.assertEqual(envelope2["body"], "Post-firewall recovery verification")

        self.run_locutus(["close", agent])

    def test_04_prompt_injection_firewall_drops_forged(self):
        agent = "nim_victim"
        # Pre-cleanup
        self.run_locutus(["close", agent])
        self.run_locutus(["open", agent, "worker"])

        # 1. Negative control on wire envelope validator directly: forged signature must fail
        forged_dict = {
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
        }
        forged_payload = json.dumps(forged_dict)

        # Get current project secret
        res_secret = self.run_locutus(["get-secret"])
        current_secret = res_secret.stdout.strip()

        with self.assertRaises(LocutusSchemaError):
            LocutusPlugin.validate_wire_envelope(forged_payload, secret=current_secret)

        # 2. Inject forged message into Redis inbox directly
        subprocess.run(
            ["redis-cli", "-u", REDIS_URL, "LPUSH", f"{TEST_PREFIX}inbox:{agent}", forged_payload],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
            check=True
        )

        # 3. Listen should drop forged message, log exact security warning to stderr, and return empty stdout on timeout
        listen_res = self.run_locutus(["listen", agent, "1"])
        self.assertEqual(listen_res.returncode, 0)
        self.assertEqual(listen_res.stdout.strip(), "")
        expected_warning = "[LOCUTUS SECURITY] WARNING: Dropping unauthenticated/tampered message (ID: forged_attack_99)"
        self.assertIn(expected_warning, listen_res.stderr)

        # 4. Verify inbox is now drained/empty (the malicious payload was discarded, not re-queued)
        inbox_len = subprocess.run(
            ["redis-cli", "-u", REDIS_URL, "LLEN", f"{TEST_PREFIX}inbox:{agent}"],
            capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(int(inbox_len), 0, f"Expected victim inbox to be empty after drop, got {inbox_len}")

        # 5. Resilience: Send valid authenticated message and verify listener processes it
        send_res = self.run_locutus([
            "send",
            "--to", agent,
            "--type", "task",
            "--subject", "Legit Task",
            "--body", "Clean valid payload",
        ])
        self.assertEqual(send_res.returncode, 0)
        valid_listen = self.run_locutus(["listen", agent, "2"])
        self.assertEqual(valid_listen.returncode, 0)
        valid_env = LocutusPlugin.validate_wire_envelope(valid_listen.stdout.strip())
        self.assertEqual(valid_env["body"], "Clean valid payload")

        self.run_locutus(["close", agent])

    def test_05_end_to_end_encryption(self):
        agent = "nim_e2ee"
        self.run_locutus(["close", agent])
        self.run_locutus(["open", agent, "worker"])

        # Retrieve secret for independent decryption audit
        secret_res = self.run_locutus(["get-secret"])
        secret = secret_res.stdout.strip()

        original_body = "E2EE Payload Content with Special Chars: §±!@#$%^&*()_+"

        # 1. Send encrypted message with LOCUTUS_ENCRYPT=1
        send_res = self.run_locutus(
            ["send", "--to", agent, "--subject", "Top Secret", "--body", original_body],
            env_overrides={"LOCUTUS_ENCRYPT": "1"}
        )
        self.assertEqual(send_res.returncode, 0)

        # 2. Inspect raw Redis payload without dequeuing
        raw_redis = subprocess.run(
            ["redis-cli", "-u", REDIS_URL, "LINDEX", f"{TEST_PREFIX}inbox:{agent}", "0"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
            check=True
        ).stdout.strip()

        # Plaintext must not leak into Redis
        self.assertNotIn(original_body, raw_redis)

        raw_msg = json.loads(raw_redis)
        self.assertTrue(raw_msg.get("encrypted"))
        ciphertext_b64 = raw_msg.get("body")
        self.assertIsInstance(ciphertext_b64, str)

        # 3. Independent byte-level AES-256-CBC & PBKDF2 decryption in Python
        raw_bytes = base64.b64decode(ciphertext_b64)
        self.assertEqual(raw_bytes[:8], b"Salted__", "Encrypted payload must contain OpenSSL 'Salted__' magic header")
        salt = raw_bytes[8:16]
        self.assertEqual(len(salt), 8, "Salt must be 8 random bytes")
        ciphertext = raw_bytes[16:]

        # Derive 32-byte AES key and 16-byte IV via PBKDF2-HMAC-SHA256 (10,000 iterations)
        key_iv = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 10000, 48)
        aes_key, aes_iv = key_iv[:32], key_iv[32:48]

        cipher = Cipher(algorithms.AES(aes_key), modes.CBC(aes_iv))
        decryptor = cipher.decryptor()
        padded_plain = decryptor.update(ciphertext) + decryptor.finalize()

        # PKCS7 unpad
        pad_len = padded_plain[-1]
        self.assertGreaterEqual(pad_len, 1)
        self.assertLessEqual(pad_len, 16)
        decrypted_body = padded_plain[:-pad_len].decode("utf-8")
        self.assertEqual(decrypted_body, original_body, "Independent Python decryption failed to match original plaintext byte-for-byte")

        # 4. Negative control: wrong secret fails to produce original plaintext
        wrong_key_iv = hashlib.pbkdf2_hmac("sha256", b"attacker_wrong_secret", salt, 10000, 48)
        wrong_decryptor = Cipher(algorithms.AES(wrong_key_iv[:32]), modes.CBC(wrong_key_iv[32:48])).decryptor()
        wrong_padded = wrong_decryptor.update(ciphertext) + wrong_decryptor.finalize()
        self.assertNotEqual(wrong_padded, original_body.encode("utf-8"))

        # 5. Listen should decrypt before stdout and deliver unencrypted payload to consumer
        listen_res = self.run_locutus(["listen", agent, "2"], env_overrides={"LOCUTUS_ENCRYPT": "1"})
        self.assertEqual(listen_res.returncode, 0)
        payload = json.loads(listen_res.stdout.strip())
        self.assertEqual(payload["body"], original_body)
        self.assertFalse(payload["encrypted"])

        self.run_locutus(["close", agent])


    def test_08_active_agent_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Negative control: running listen in fresh empty directory without active agent exits 1
            empty_listen = self.run_locutus(["listen", "1"], cwd=tmpdir)
            self.assertEqual(empty_listen.returncode, 1)
            self.assertIn("Error: No agent name specified", empty_listen.stderr)

            # 2. When opening without explicit name, generates project-worker-XXXX and persists to .locutus.agent
            open_res = self.run_locutus(["open"], cwd=tmpdir)
            self.assertEqual(open_res.returncode, 0)
            self.assertIn("Agent Name :", open_res.stdout)

            # Extract generated agent name from stdout
            active_agent = ""
            for line in open_res.stdout.splitlines():
                if "Agent Name :" in line:
                    active_agent = line.split(":", 1)[1].strip()
                    break
            self.assertTrue(active_agent.startswith("test_project-worker-"))

            # 3. Assert exact .locutus.agent file existence, content, and strict 0600 POSIX permissions
            agent_file = os.path.join(tmpdir, ".locutus.agent")
            self.assertTrue(os.path.isfile(agent_file), f"Expected .locutus.agent file at {agent_file}")
            with open(agent_file, "r") as f:
                persisted_name = f.read().strip()
            self.assertEqual(persisted_name, active_agent)

            if os.name != "nt":
                file_mode = os.stat(agent_file).st_mode & 0o777
                self.assertEqual(file_mode, 0o600, f"Expected 0600 permissions on {agent_file}, got {oct(file_mode)}")

            # 4. Send to active agent without explicit --from (locutus infers sender from .locutus.agent)
            send_res = self.run_locutus([
                "send",
                "--to", active_agent,
                "--subject", "Self Ping",
                "--body", "Testing persistence",
            ], cwd=tmpdir)
            self.assertEqual(send_res.returncode, 0)

            # 5. Listen without specifying name (reads active agent from cwd .locutus.agent)
            listen_res = self.run_locutus(["listen", "2"], cwd=tmpdir)
            self.assertEqual(listen_res.returncode, 0)
            envelope = LocutusPlugin.validate_wire_envelope(listen_res.stdout.strip())
            self.assertEqual(envelope["from"], active_agent)
            self.assertEqual(envelope["to"], active_agent)
            self.assertEqual(envelope["body"], "Testing persistence")

            # 6. Close without specifying name (should close active agent and delete .locutus.agent)
            close_res = self.run_locutus(["close"], cwd=tmpdir)
            self.assertEqual(close_res.returncode, 0)
            self.assertFalse(os.path.isfile(agent_file), ".locutus.agent should be deleted after close")

    def test_09_multicast_broadcast_with_tags_routing(self):
        # Pre-cleanup
        for a in ["agent_qa", "agent_dev", "agent_ops"]:
            self.run_locutus(["close", a])

        # 1. Open 3 agents with distinct and overlapping tags
        self.run_locutus(["open", "agent_qa", "qa,backend"])
        self.run_locutus(["open", "agent_dev", "dev,backend"])
        self.run_locutus(["open", "agent_ops", "ops,infra"])

        # Negative control: verify all 3 inboxes are empty initially
        for a in ["agent_qa", "agent_dev", "agent_ops"]:
            qlen = int(subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "LLEN", f"{TEST_PREFIX}inbox:{a}"],
                capture_output=True, text=True, check=True
            ).stdout.strip() or 0)
            self.assertEqual(qlen, 0, f"Inbox for {a} must be empty before broadcast")

        # 2. Broadcast targeting specific tag 'qa'
        bcast_res1 = self.run_locutus([
            "broadcast",
            "--tags", "qa",
            "--subject", "QA Notice",
            "--body", "Only for QA",
        ])
        self.assertEqual(bcast_res1.returncode, 0)

        # Assert Redis queue isolation directly
        qa_len = int(subprocess.run(["redis-cli", "-u", REDIS_URL, "LLEN", f"{TEST_PREFIX}inbox:agent_qa"], capture_output=True, text=True).stdout.strip())
        dev_len = int(subprocess.run(["redis-cli", "-u", REDIS_URL, "LLEN", f"{TEST_PREFIX}inbox:agent_dev"], capture_output=True, text=True).stdout.strip())
        ops_len = int(subprocess.run(["redis-cli", "-u", REDIS_URL, "LLEN", f"{TEST_PREFIX}inbox:agent_ops"], capture_output=True, text=True).stdout.strip())
        self.assertEqual(qa_len, 1, "agent_qa should have exactly 1 message")
        self.assertEqual(dev_len, 0, "agent_dev must NOT receive qa broadcast")
        self.assertEqual(ops_len, 0, "agent_ops must NOT receive qa broadcast")

        # agent_qa consumes and validates message wire envelope
        res_qa = self.run_locutus(["listen", "agent_qa", "1"])
        self.assertEqual(res_qa.returncode, 0)
        env_qa = LocutusPlugin.validate_wire_envelope(res_qa.stdout.strip())
        self.assertEqual(env_qa["to"], "@qa")
        self.assertEqual(env_qa["subject"], "QA Notice")
        self.assertEqual(env_qa["body"], "Only for QA")

        # Non-matching agents timeout with empty stdout
        res_dev = self.run_locutus(["listen", "agent_dev", "1"])
        self.assertEqual(res_dev.returncode, 0)
        self.assertEqual(res_dev.stdout.strip(), "")

        res_ops = self.run_locutus(["listen", "agent_ops", "1"])
        self.assertEqual(res_ops.returncode, 0)
        self.assertEqual(res_ops.stdout.strip(), "")

        # 3. Multicast targeting shared tag 'backend' (should reach agent_qa AND agent_dev, but NOT agent_ops)
        bcast_res2 = self.run_locutus([
            "broadcast",
            "--tags", "backend",
            "--subject", "Backend Sync",
            "--body", "Shared backend update",
        ])
        self.assertEqual(bcast_res2.returncode, 0)

        # Both backend agents should receive it
        for agent_name in ["agent_qa", "agent_dev"]:
            rec_res = self.run_locutus(["listen", agent_name, "1"])
            self.assertEqual(rec_res.returncode, 0)
            env = LocutusPlugin.validate_wire_envelope(rec_res.stdout.strip())
            self.assertEqual(env["to"], "@backend")
            self.assertEqual(env["body"], "Shared backend update")

        # agent_ops still empty
        ops_rec = self.run_locutus(["listen", "agent_ops", "1"])
        self.assertEqual(ops_rec.returncode, 0)
        self.assertEqual(ops_rec.stdout.strip(), "")

        # 4. Negative control: broadcast to nonexistent tag delivers to 0 agents
        bcast_none = self.run_locutus([
            "broadcast",
            "--tags", "nonexistent_null_tag",
            "--subject", "Ghost",
            "--body", "Void",
        ])
        self.assertEqual(bcast_none.returncode, 0)
        for a in ["agent_qa", "agent_dev", "agent_ops"]:
            qlen = int(subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "LLEN", f"{TEST_PREFIX}inbox:{a}"],
                capture_output=True, text=True
            ).stdout.strip() or 0)
            self.assertEqual(qlen, 0, f"Ghost message must not be queued to {a}")

        # Cleanup
        for a in ["agent_qa", "agent_dev", "agent_ops"]:
            self.run_locutus(["close", a])

    def test_10_corrupted_encrypted_payload_dropped(self):
        agent = "agent_corrupt_test"
        self.run_locutus(["close", agent])
        self.run_locutus(["open", agent, "worker"])

        # 1. Construct an encrypted payload with valid HMAC signature but invalid ciphertext
        import hmac
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

        # 2. Listen should drop the undecryptable frame, log warning to stderr, and return empty stdout on timeout
        listen_res = self.run_locutus(["listen", agent, "1"])
        self.assertEqual(listen_res.returncode, 0)
        self.assertEqual(listen_res.stdout.strip(), "")
        self.assertIn("Dropping corrupted/undecryptable message", listen_res.stderr)

        # 3. Assert corrupted item was purged from Redis queue and not re-queued
        inbox_len = subprocess.run(
            ["redis-cli", "-u", REDIS_URL, "LLEN", f"{TEST_PREFIX}inbox:{agent}"],
            capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(int(inbox_len), 0, "Corrupted message must be purged from queue")

        # 4. Resilience verification: listener process remains healthy and processes subsequent valid encrypted payload
        send_res = self.run_locutus(
            ["send", "--to", agent, "--subject", "Healthy Message", "--body", "Valid Post-Corruption Payload"],
            env_overrides={"LOCUTUS_ENCRYPT": "1"}
        )
        self.assertEqual(send_res.returncode, 0)

        valid_listen = self.run_locutus(["listen", agent, "2"], env_overrides={"LOCUTUS_ENCRYPT": "1"})
        self.assertEqual(valid_listen.returncode, 0)
        valid_payload = json.loads(valid_listen.stdout.strip())
        self.assertEqual(valid_payload["body"], "Valid Post-Corruption Payload")
        self.assertFalse(valid_payload["encrypted"])

        self.run_locutus(["close", agent])

    def test_11_argument_validation_for_send_and_broadcast(self):
        # 1. Send missing --subject and --body
        res_no_body = self.run_locutus(["send", "--to", "nobody"])
        self.assertEqual(res_no_body.returncode, 1)
        self.assertIn("Error: Missing required arguments. --subject and --body are required.", res_no_body.stderr)
        self.assertIn("Usage: locutus send --to <recipient>", res_no_body.stderr)

        # 2. Send missing --to recipient
        res_no_to = self.run_locutus(["send", "--subject", "Task", "--body", "Details"])
        self.assertEqual(res_no_to.returncode, 1)
        self.assertIn("Error: Missing required argument '--to <recipient>'.", res_no_to.stderr)

        # 3. Broadcast missing --subject and --body
        res_b_empty = self.run_locutus(["broadcast"])
        self.assertEqual(res_b_empty.returncode, 1)
        self.assertIn("Error: Missing required arguments. --subject and --body are required.", res_b_empty.stderr)
        self.assertIn("Usage: locutus broadcast [--tags <tags>] --subject <subj> --body <body>", res_b_empty.stderr)

        # 4. Reply missing --to recipient
        res_rep_no_to = self.run_locutus(["reply", "--subject", "Re: Task", "--body", "Done"])
        self.assertEqual(res_rep_no_to.returncode, 1)
        self.assertIn("Error: Missing required argument '--to <recipient>'.", res_rep_no_to.stderr)
        self.assertIn("Usage: locutus reply --to <recipient>", res_rep_no_to.stderr)

        # 5. Reply missing body
        res_rep_no_body = self.run_locutus(["reply", "--to", "alice", "--subject", "Re: Task"])
        self.assertEqual(res_rep_no_body.returncode, 1)
        self.assertIn("Error: Missing required arguments. --subject and --body are required.", res_rep_no_body.stderr)

    def test_12_large_e2ee_payload_byte_for_byte(self):
        agent = "test_large_e2ee"
        self.run_locutus(["close", agent])
        self.run_locutus(["open", agent, "worker"])

        # 1. Generate 1 MB structured payload with entropy and verify pre-send hash
        chunk = "EntropyBlock:0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ\n"
        large_body = chunk * 13500  # ~1,012,500 bytes (over 1 MB)
        self.assertGreater(len(large_body), 1000000, f"Payload size must exceed 1MB, got {len(large_body)} bytes")
        expected_sha256 = hashlib.sha256(large_body.encode("utf-8")).hexdigest()

        # Negative control: verify inbox is initially empty
        inbox_pre = int(subprocess.run(
            ["redis-cli", "-u", REDIS_URL, "LLEN", f"{TEST_PREFIX}inbox:{agent}"],
            capture_output=True, text=True, check=True
        ).stdout.strip() or 0)
        self.assertEqual(inbox_pre, 0)

        # 2. Send 1MB payload with LOCUTUS_ENCRYPT=1
        res = self.run_locutus(
            ["send", "--to", agent, "--subject", "Large E2EE 1MB", "--body", large_body],
            env_overrides={"LOCUTUS_ENCRYPT": "1"}
        )
        self.assertEqual(res.returncode, 0)

        # 3. Verify Redis raw payload is encrypted and contains no plaintext leakage
        raw_redis = subprocess.run(
            ["redis-cli", "-u", REDIS_URL, "LINDEX", f"{TEST_PREFIX}inbox:{agent}", "0"],
            capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertNotIn("EntropyBlock:0123456789abcdef", raw_redis)
        raw_msg = json.loads(raw_redis)
        self.assertTrue(raw_msg.get("encrypted"))
        self.assertGreater(len(raw_msg.get("body", "")), 1000000)

        # 4. Listen should decrypt 1MB payload cleanly and match exact SHA-256 digest
        listen_res = self.run_locutus(["listen", agent, "5"], env_overrides={"LOCUTUS_ENCRYPT": "1"})
        self.assertEqual(listen_res.returncode, 0)
        envelope = LocutusPlugin.validate_wire_envelope(listen_res.stdout.strip())
        received_body = envelope.get("body", "")

        self.assertEqual(len(received_body), len(large_body))
        received_sha256 = hashlib.sha256(received_body.encode("utf-8")).hexdigest()
        self.assertEqual(received_sha256, expected_sha256, "SHA-256 digest mismatch on decrypted 1MB payload")
        self.assertEqual(received_body, large_body, "Decrypted 1MB payload is not byte-for-byte identical")

        # 5. Negative control: 1-byte corrupted body fails hash validation
        tampered_body = received_body[:-1] + "X"
        tampered_sha256 = hashlib.sha256(tampered_body.encode("utf-8")).hexdigest()
        self.assertNotEqual(tampered_sha256, expected_sha256)

        self.run_locutus(["close", agent])

    def test_13_config_show_and_json(self):
        # 1. Table output verification
        res = self.run_locutus(["config", "show"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("SETTING", res.stdout)
        self.assertIn("VALUE", res.stdout)
        self.assertIn("SOURCE", res.stdout)
        self.assertIn("DETAIL", res.stdout)
        for key in ["redis_url", "prefix", "project", "encrypt", "heartbeat_ttl", "message_ttl", "listen_timeout", "cluster"]:
            self.assertIn(key, res.stdout, f"Config table missing key row: {key}")

        # 2. JSON output and schema validation via LocutusPlugin
        res_j = self.run_locutus(["config", "show", "--json"])
        self.assertEqual(res_j.returncode, 0)
        data = LocutusPlugin.validate_json_schema("config", res_j.stdout)
        self.assertIsInstance(data, dict)

        # Assert full key presence and provenance types
        for req_key in ["redis_url", "prefix", "project", "encrypt", "heartbeat_ttl", "message_ttl", "listen_timeout", "cluster"]:
            self.assertIn(req_key, data, f"Config JSON missing key '{req_key}'")
            entry = data[req_key]
            self.assertIsInstance(entry, dict, f"Entry for '{req_key}' must be an object")
            self.assertIn("value", entry)
            self.assertIn("source", entry)
            self.assertIn("detail", entry)
            self.assertIsInstance(entry["value"], str)
            self.assertIsInstance(entry["source"], str)
            self.assertIsInstance(entry["detail"], str)

        self.assertEqual(data["redis_url"]["value"], REDIS_URL)
        self.assertEqual(data["prefix"]["value"], TEST_PREFIX)
        self.assertEqual(data["project"]["value"], "test_project")

        # 3. Negative control: unknown config subcommand returns exit code 1
        res_bad = self.run_locutus(["config", "nonexistent_subaction"])
        self.assertEqual(res_bad.returncode, 1)
        self.assertIn("Unknown config action: nonexistent_subaction", res_bad.stderr)
        self.assertIn("Usage: locutus config <show|get|path|init>", res_bad.stderr)

    def test_14_config_get(self):
        # 1. Positive queries across core config keys
        res = self.run_locutus(["config", "get", "redis_url"])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), REDIS_URL)

        res_p = self.run_locutus(["config", "get", "project"])
        self.assertEqual(res_p.returncode, 0)
        self.assertEqual(res_p.stdout.strip(), "test_project")

        res_pfx = self.run_locutus(["config", "get", "prefix"])
        self.assertEqual(res_pfx.returncode, 0)
        self.assertEqual(res_pfx.stdout.strip(), TEST_PREFIX)

        res_enc = self.run_locutus(["config", "get", "encrypt"])
        self.assertEqual(res_enc.returncode, 0)
        self.assertIn(res_enc.stdout.strip(), ["false", "true"])

        res_hb = self.run_locutus(["config", "get", "heartbeat_ttl"])
        self.assertEqual(res_hb.returncode, 0)
        self.assertGreater(int(res_hb.stdout.strip()), 0)

        # 2. Negative control: missing key argument exits 1 with usage instruction
        res_no_key = self.run_locutus(["config", "get"])
        self.assertEqual(res_no_key.returncode, 1)
        self.assertIn("Usage: locutus config get <key>", res_no_key.stderr)

        # 3. Negative control: non-existent key exits 1 with descriptive error
        res_err = self.run_locutus(["config", "get", "non_existent_key_xyz"])
        self.assertEqual(res_err.returncode, 1)
        self.assertIn("Error: Unknown configuration key: non_existent_key_xyz", res_err.stderr)

    def test_15_config_cli_overrides(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Setup workspace config file defining Tier 1 defaults
            cfg_file = os.path.join(tmpdir, ".locutus.toml")
            with open(cfg_file, "w", encoding="utf-8") as f:
                f.write('redis_url = "redis://file-tier:6379"\nprefix = "file_pfx:"\nproject = "file_proj"\n')

            # 1. Tier 1: Config file value active when env and CLI are absent
            res_tier1 = self.run_locutus(["config", "get", "redis_url"], env_overrides={"LOCUTUS_REDIS_URL": "", "LOCUTUS_REDIS_PREFIX": "", "LOCUTUS_PROJECT": ""}, cwd=tmpdir)
            self.assertEqual(res_tier1.returncode, 0)
            self.assertEqual(res_tier1.stdout.strip(), "redis://file-tier:6379")

            res_t1_j = self.run_locutus(["config", "show", "--json"], env_overrides={"LOCUTUS_REDIS_URL": "", "LOCUTUS_REDIS_PREFIX": "", "LOCUTUS_PROJECT": ""}, cwd=tmpdir)
            d1 = LocutusPlugin.validate_json_schema("config", res_t1_j.stdout)
            self.assertEqual(d1["redis_url"]["source"], "workspace config")

            # 2. Tier 2: Environment variable overrides config file
            env_tier2 = {"LOCUTUS_REDIS_URL": "redis://env-tier:6379", "LOCUTUS_REDIS_PREFIX": "env_pfx:", "LOCUTUS_PROJECT": "env_proj"}
            res_tier2 = self.run_locutus(["config", "get", "redis_url"], env_overrides=env_tier2, cwd=tmpdir)
            self.assertEqual(res_tier2.returncode, 0)
            self.assertEqual(res_tier2.stdout.strip(), "redis://env-tier:6379")

            res_t2_j = self.run_locutus(["config", "show", "--json"], env_overrides=env_tier2, cwd=tmpdir)
            d2 = LocutusPlugin.validate_json_schema("config", res_t2_j.stdout)
            self.assertEqual(d2["redis_url"]["source"], "environment")

            # 3. Tier 3: CLI flag overrides BOTH environment variable AND config file
            custom_url = "rediss://cli-tier.custom:6380"
            res_tier3 = self.run_locutus(["--redis-url", custom_url, "config", "get", "redis_url"], env_overrides=env_tier2, cwd=tmpdir)
            self.assertEqual(res_tier3.returncode, 0)
            self.assertEqual(res_tier3.stdout.strip(), custom_url)

            res_t3_j = self.run_locutus(["--redis-url", custom_url, "config", "show", "--json"], env_overrides=env_tier2, cwd=tmpdir)
            d3 = LocutusPlugin.validate_json_schema("config", res_t3_j.stdout)
            self.assertEqual(d3["redis_url"]["value"], custom_url)
            self.assertEqual(d3["redis_url"]["source"], "cli flag")

        # 4. Cluster mode auto-applies hash tags
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
                    'heartbeat_ttl = 300\n'
                    '\n'
                    '[profiles.staging]\n'
                    'redis_url = "rediss://staging.cluster:6380"\n'
                    'prefix = "ws_staging:"\n'
                    'project = "staging_proj"\n'
                    'encrypt = true\n'
                    '\n'
                    '[profiles.prod]\n'
                    'redis_url = "rediss://prod.cluster:6380"\n'
                    'prefix = "ws_prod:"\n'
                    'heartbeat_ttl = 3600\n'
                )

            clean_env = {
                "LOCUTUS_REDIS_URL": "",
                "LOCUTUS_REDIS_PREFIX": "",
                "LOCUTUS_PROJECT": "",
            }

            # 1. Default profile verification (no --profile specified)
            res_def_json = self.run_locutus(["config", "show", "--json"], env_overrides=clean_env, cwd=tmp_dir)
            self.assertEqual(res_def_json.returncode, 0)
            data_def = LocutusPlugin.validate_json_schema("config", res_def_json.stdout)
            self.assertEqual(data_def.get("active_profile"), "default")
            self.assertEqual(data_def["redis_url"]["value"], "redis://workspace-default:6379")
            self.assertEqual(data_def["prefix"]["value"], "ws_default:")
            self.assertEqual(data_def["project"]["value"], "ws_proj")
            self.assertEqual(data_def["heartbeat_ttl"]["value"], "300")

            # 2. Profile switching to 'staging' via --profile
            res_stag_json = self.run_locutus(["--profile", "staging", "config", "show", "--json"], env_overrides=clean_env, cwd=tmp_dir)
            self.assertEqual(res_stag_json.returncode, 0)
            data_stag = LocutusPlugin.validate_json_schema("config", res_stag_json.stdout)
            self.assertEqual(data_stag.get("active_profile"), "staging")
            self.assertEqual(data_stag["redis_url"]["value"], "rediss://staging.cluster:6380")
            self.assertEqual(data_stag["prefix"]["value"], "ws_staging:")
            self.assertEqual(data_stag["project"]["value"], "staging_proj")
            self.assertEqual(data_stag["encrypt"]["value"], "true")

            # 3. Profile switching to 'prod'
            res_prod_json = self.run_locutus(["--profile", "prod", "config", "show", "--json"], env_overrides=clean_env, cwd=tmp_dir)
            self.assertEqual(res_prod_json.returncode, 0)
            data_prod = LocutusPlugin.validate_json_schema("config", res_prod_json.stdout)
            self.assertEqual(data_prod.get("active_profile"), "prod")
            self.assertEqual(data_prod["redis_url"]["value"], "rediss://prod.cluster:6380")
            self.assertEqual(data_prod["prefix"]["value"], "ws_prod:")
            self.assertEqual(data_prod["heartbeat_ttl"]["value"], "3600")

            # 4. Negative control: switching to unconfigured profile falls back to default base settings
            res_none_json = self.run_locutus(["--profile", "unconfigured_xyz", "config", "show", "--json"], env_overrides=clean_env, cwd=tmp_dir)
            self.assertEqual(res_none_json.returncode, 0)
            data_none = LocutusPlugin.validate_json_schema("config", res_none_json.stdout)
            self.assertEqual(data_none.get("active_profile"), "unconfigured_xyz")
            self.assertEqual(data_none["redis_url"]["value"], "redis://workspace-default:6379")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_17_config_paths_and_init(self):
        # 1. Path hierarchy verification
        res_paths = self.run_locutus(["config", "path"])
        self.assertEqual(res_paths.returncode, 0)
        self.assertIn("System config", res_paths.stdout)
        self.assertIn("User config", res_paths.stdout)
        self.assertIn("Workspace config", res_paths.stdout)

        with tempfile.TemporaryDirectory() as tmp_dir:
            # 2. Initial config generation
            res_init = self.run_locutus(["config", "init"], cwd=tmp_dir)
            self.assertEqual(res_init.returncode, 0)
            self.assertIn("Initialized Locutus configuration at:", res_init.stdout)

            created_file = os.path.join(tmp_dir, ".locutus.toml")
            self.assertTrue(os.path.isfile(created_file))
            with open(created_file, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("redis_url", content)
            self.assertIn("prefix", content)
            self.assertIn("project", content)
            self.assertIn("[profiles.staging]", content)

            # Assert strict 0600 POSIX permissions
            if os.name != "nt":
                mode = os.stat(created_file).st_mode & 0o777
                self.assertEqual(mode, 0o600, f"Expected 0600 config file permissions, got {oct(mode)}")

            # 3. Negative control: duplicate init without --force fails with exit code 1
            res_dup = self.run_locutus(["config", "init"], cwd=tmp_dir)
            self.assertEqual(res_dup.returncode, 1)
            self.assertIn("Error: Config file already exists", res_dup.stderr)
            self.assertIn("--force", res_dup.stderr)

            # 4. Force overwrite with --force succeeds with exit code 0
            res_force = self.run_locutus(["config", "init", "--force"], cwd=tmp_dir)
            self.assertEqual(res_force.returncode, 0)
            self.assertIn("Initialized Locutus configuration at:", res_force.stdout)
            if os.name != "nt":
                mode_after = os.stat(created_file).st_mode & 0o777
                self.assertEqual(mode_after, 0o600)

    def test_18_non_json_payload_dropped(self):
        """Verify that malformed non-JSON messages in Redis are dropped to stderr and don't crash listener."""
        agent = "agent_malformed_test"
        self.run_locutus(["close", agent])
        self.run_locutus(["open", agent, "worker"])

        # 1. Inject multiple forms of invalid non-JSON raw text
        garbage_payloads = [
            "NOT_JSON_RAW_DATA_{{{",
            "{truncated_json: 'missing_closing_brace'",
            "   \t\r\n   ",
        ]
        for g in garbage_payloads:
            subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "LPUSH", f"{TEST_PREFIX}inbox:{agent}", g],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=15,
                check=True
            )

        # 2. Listen should drop non-JSON payloads to stderr and exit cleanly on timeout
        listen_res = self.run_locutus(["listen", agent, "1"])
        self.assertEqual(listen_res.returncode, 0)
        self.assertEqual(listen_res.stdout.strip(), "")
        self.assertIn("Dropping non-JSON payload from inbox", listen_res.stderr)

        # 3. Assert Redis queue was purged of all garbage entries
        inbox_len = subprocess.run(
            ["redis-cli", "-u", REDIS_URL, "LLEN", f"{TEST_PREFIX}inbox:{agent}"],
            capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(int(inbox_len), 0, f"Expected inbox to be empty after dropping garbage, got {inbox_len}")

        # 4. Resilience recovery: verify listener process remains operational and receives subsequent valid message
        send_res = self.run_locutus([
            "send",
            "--to", agent,
            "--subject", "Recovery Task",
            "--body", "Post-garbage healthy message",
        ])
        self.assertEqual(send_res.returncode, 0)

        listen_res2 = self.run_locutus(["listen", agent, "2"])
        self.assertEqual(listen_res2.returncode, 0)
        envelope = LocutusPlugin.validate_wire_envelope(listen_res2.stdout.strip())
        self.assertEqual(envelope["body"], "Post-garbage healthy message")

        self.run_locutus(["close", agent])

    def test_19_unreachable_redis_error_handling(self):
        """Verify that attempting to contact an unreachable Redis instance returns exit code 1 and clean error."""
        unreachable_url = "redis://127.0.0.1:1"

        # 1. Test unreachable Redis on 'send'
        res_send = self.run_locutus([
            "--redis-url", unreachable_url,
            "send",
            "--to", "nobody",
            "--subject", "Fail",
            "--body", "Payload"
        ])
        self.assertEqual(res_send.returncode, 1)
        output_send = res_send.stdout + res_send.stderr
        self.assertIn("Redis error: Could not connect to Redis", output_send)
        # Negative control: no raw Nim stack traces or panics dumped
        self.assertNotIn("Traceback (most recent call last)", output_send)
        self.assertNotIn("Error: unhandled exception", output_send)
        self.assertNotIn("SIGSEGV", output_send)

        # 2. Test unreachable Redis on 'who'
        res_who = self.run_locutus([
            "--redis-url", unreachable_url,
            "who"
        ])
        self.assertEqual(res_who.returncode, 1)
        output_who = res_who.stdout + res_who.stderr
        self.assertIn("Redis error: Could not connect to Redis", output_who)
        self.assertNotIn("Traceback (most recent call last)", output_who)

    def test_20_tag_argument_validation(self):
        """Verify that 'locutus tag' validates required arguments, actions, agent identity, and handles complex tag matrices."""
        # 1. Missing arguments
        res_missing = self.run_locutus(["tag"])
        self.assertEqual(res_missing.returncode, 1)
        self.assertIn("Error: Missing arguments for tag command.", res_missing.stderr)
        self.assertIn("Usage: locutus tag <add|remove|set> <tags> [name]", res_missing.stderr)

        res_missing_tags = self.run_locutus(["tag", "add"])
        self.assertEqual(res_missing_tags.returncode, 1)
        self.assertIn("Error: Missing arguments for tag command.", res_missing_tags.stderr)

        # 2. Invalid action
        res_invalid = self.run_locutus(["tag", "badaction", "mytag"])
        self.assertEqual(res_invalid.returncode, 1)
        self.assertIn("Error: Invalid tag action 'badaction'. Expected add, remove, or set.", res_invalid.stderr)

        # 3. Missing agent identity when no current agent exists
        with tempfile.TemporaryDirectory() as empty_dir:
            res_no_agent = self.run_locutus(["tag", "add", "mytag"], cwd=empty_dir, env_overrides={"LOCUTUS_AGENT_NAME": ""})
            self.assertEqual(res_no_agent.returncode, 1)
            self.assertIn("Error: No agent name specified", res_no_agent.stderr)

        # 4. Unregistered agent negative control
        res_unreg = self.run_locutus(["tag", "add", "mytag", "nonexistent_agent_999"])
        self.assertEqual(res_unreg.returncode, 1)
        self.assertIn("ERR agent 'nonexistent_agent_999' is not registered", res_unreg.stderr)

        # 5. Matrix of valid tag operations with special characters, colons, and whitespace trimming
        agent = "agent_tag_matrix_test"
        self.run_locutus(["close", agent])
        self.run_locutus(["open", agent, "worker"])

        try:
            # 5a. Add tags with whitespace padding and colons/special characters
            res_add = self.run_locutus(["tag", "add", "  team:backend , @infra/node-1 , worker_core  ", agent])
            self.assertEqual(res_add.returncode, 0)
            tags_stdout = res_add.stdout.strip()
            self.assertIn("@infra/node-1", tags_stdout)
            self.assertIn("team:backend", tags_stdout)
            self.assertIn("worker_core", tags_stdout)

            # Direct Redis inspection: verify agent added to tag set and agent hash updated
            for t in ["@infra/node-1", "team:backend", "worker_core"]:
                is_mem = subprocess.run(
                    ["redis-cli", "-u", REDIS_URL, "SISMEMBER", f"{TEST_PREFIX}tag:{t}", agent],
                    capture_output=True, text=True
                ).stdout.strip()
                self.assertEqual(is_mem, "1", f"Agent {agent} must be in set for tag {t}")

            # 5b. Remove tag
            res_rem = self.run_locutus(["tag", "remove", "team:backend", agent])
            self.assertEqual(res_rem.returncode, 0)
            self.assertNotIn("team:backend", res_rem.stdout)
            is_mem_rem = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "SISMEMBER", f"{TEST_PREFIX}tag:team:backend", agent],
                capture_output=True, text=True
            ).stdout.strip()
            self.assertEqual(is_mem_rem, "0", f"Agent {agent} must no longer be in tag:team:backend")

            # 5c. Set tags (overwrites existing tags)
            res_set = self.run_locutus(["tag", "set", "production,v2.0", agent])
            self.assertEqual(res_set.returncode, 0)
            self.assertEqual(res_set.stdout.strip(), "production,v2.0")

            # Former tags should be gone from Redis sets
            for old_t in ["@infra/node-1", "worker_core"]:
                is_mem_old = subprocess.run(
                    ["redis-cli", "-u", REDIS_URL, "SISMEMBER", f"{TEST_PREFIX}tag:{old_t}", agent],
                    capture_output=True, text=True
                ).stdout.strip()
                self.assertEqual(is_mem_old, "0", f"Old tag {old_t} must be cleared")

            # New tags should exist
            for new_t in ["production", "v2.0"]:
                is_mem_new = subprocess.run(
                    ["redis-cli", "-u", REDIS_URL, "SISMEMBER", f"{TEST_PREFIX}tag:{new_t}", agent],
                    capture_output=True, text=True
                ).stdout.strip()
                self.assertEqual(is_mem_new, "1", f"New tag {new_t} must be present")

            # 5d. Negative control: removing nonexistent tag is safe and idempotent
            res_rem_ghost = self.run_locutus(["tag", "remove", "ghost_tag_xyz", agent])
            self.assertEqual(res_rem_ghost.returncode, 0)
            self.assertEqual(res_rem_ghost.stdout.strip(), "production,v2.0")
        finally:
            self.run_locutus(["close", agent])

    def test_21_drain_argument_validation(self):
        """Verify that 'locutus drain' rejects non-integer counts, handles missing agent identities, and empties Redis queues."""
        # 1. Non-integer count rejection
        res_non_int = self.run_locutus(["drain", "not_a_number"])
        self.assertEqual(res_non_int.returncode, 1)
        self.assertIn("Error: Invalid count 'not_a_number' for drain command. Expected an integer.", res_non_int.stderr)
        self.assertIn("Usage: locutus drain [count] [name]", res_non_int.stderr)

        # 2. Missing agent identity when no current agent exists
        with tempfile.TemporaryDirectory() as empty_dir:
            res_no_agent = self.run_locutus(["drain", "10"], cwd=empty_dir, env_overrides={"LOCUTUS_AGENT_NAME": ""})
            self.assertEqual(res_no_agent.returncode, 1)
            self.assertIn("Error: No agent name specified", res_no_agent.stderr)

        # 3. Operational drain and Redis queue verification
        agent = "agent_drain_verify_test"
        self.run_locutus(["close", agent])
        self.run_locutus(["open", agent, "worker"])

        try:
            inbox_key = f"{TEST_PREFIX}inbox:{agent}"
            # Ensure queue is initially empty
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", inbox_key], check=True, capture_output=True)

            # Send 3 distinct messages
            for i in range(1, 4):
                res_send = self.run_locutus([
                    "send",
                    "--to", agent,
                    "--subject", f"Drain Task {i}",
                    "--body", f"Body {i}"
                ])
                self.assertEqual(res_send.returncode, 0)

            # Assert Redis queue has 3 messages
            llen_before = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "LLEN", inbox_key],
                capture_output=True, text=True
            ).stdout.strip()
            self.assertEqual(int(llen_before), 3, "Inbox must contain exactly 3 items before drain")

            # Partial drain: pop 2 items
            res_drain_2 = self.run_locutus(["drain", "2", agent])
            self.assertEqual(res_drain_2.returncode, 0)
            llen_after_2 = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "LLEN", inbox_key],
                capture_output=True, text=True
            ).stdout.strip()
            self.assertEqual(int(llen_after_2), 1, "Inbox must contain 1 item after draining 2")

            # Complete drain: pop remaining items
            res_drain_rem = self.run_locutus(["drain", "10", agent])
            self.assertEqual(res_drain_rem.returncode, 0)
            llen_empty = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "LLEN", inbox_key],
                capture_output=True, text=True
            ).stdout.strip()
            self.assertEqual(int(llen_empty), 0, "Inbox must be completely emptied (LLEN 0)")

            # Drain on empty queue returns exit code 0 and keeps LLEN at 0
            res_drain_empty = self.run_locutus(["drain", "10", agent])
            self.assertEqual(res_drain_empty.returncode, 0)
            llen_still_empty = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "LLEN", inbox_key],
                capture_output=True, text=True
            ).stdout.strip()
            self.assertEqual(int(llen_still_empty), 0)
        finally:
            self.run_locutus(["close", agent])

    def test_22_crypto_tmp_cleanup_guarantee(self):
        """Verify that encrypt/decrypt operations leave zero temporary files, even during induced crypto failures and reap stale files."""
        agent = "agent_crypto_cleanup_test"
        self.run_locutus(["close", agent])
        self.run_locutus(["open", agent, "worker"])

        tmp_dir = os.path.expanduser("~/.config/locutus/tmp")
        os.makedirs(tmp_dir, exist_ok=True)

        try:
            # 1. Normal encrypted send and decrypt
            res_send = self.run_locutus(
                ["send", "--to", agent, "--subject", "Cleanup Test", "--body", "Confidential Data 123"],
                env_overrides={"LOCUTUS_ENCRYPT": "1"}
            )
            self.assertEqual(res_send.returncode, 0)

            res_listen = self.run_locutus(["listen", agent, "2"], env_overrides={"LOCUTUS_ENCRYPT": "1"})
            self.assertEqual(res_listen.returncode, 0)
            self.assertEqual(json.loads(res_listen.stdout.strip())["body"], "Confidential Data 123")

            # Inspect tmp directory: zero .tmp files created
            tmp_files = [f for f in os.listdir(tmp_dir) if f.endswith(".tmp")]
            self.assertEqual(tmp_files, [], f"Leaked tmp files found after normal crypto: {tmp_files}")

            # 2. Negative control: Induced decryption failure (corrupted encrypted payload)
            corrupted_payload = json.dumps({
                "id": "msg_fail_cleanup",
                "from": "attacker",
                "to": agent,
                "type": "task",
                "reply_to": None,
                "tags": [],
                "subject": "Corrupt",
                "body": "Salted__CorruptedCiphertextThatCannotDecrypt",
                "timestamp": "2026-09-19T00:00:00Z",
                "sig": "",
                "encrypted": True
            })
            subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "LPUSH", f"{TEST_PREFIX}inbox:{agent}", corrupted_payload],
                check=True, capture_output=True
            )

            # Listen will attempt decryption, fail, and drop the message
            res_fail = self.run_locutus(["listen", agent, "1"], env_overrides={"LOCUTUS_ENCRYPT": "1"})
            self.assertEqual(res_fail.returncode, 0)

            # Assert zero temp files left behind after failure
            tmp_files_after_fail = [f for f in os.listdir(tmp_dir) if f.endswith(".tmp")]
            self.assertEqual(tmp_files_after_fail, [], f"Leaked tmp files after decryption failure: {tmp_files_after_fail}")

            # 3. Negative control: Stale tmp reaping via cleanupOldTmpFiles()
            stale_tmp = os.path.join(tmp_dir, "stale_test_artifact.tmp")
            fresh_tmp = os.path.join(tmp_dir, "fresh_test_artifact.tmp")
            with open(stale_tmp, "w") as f:
                f.write("old data")
            with open(fresh_tmp, "w") as f:
                f.write("new data")

            # Set mtime of stale file to 2 hours ago (7200s in the past)
            past_time = time.time() - 7200
            os.utime(stale_tmp, (past_time, past_time))

            # Running 'open' triggers cleanupOldTmpFiles()
            self.run_locutus(["open", f"{agent}_reaper", "worker"])
            self.run_locutus(["close", f"{agent}_reaper"])

            # Stale file must be purged; fresh file must remain intact
            self.assertFalse(os.path.exists(stale_tmp), "Stale tmp file (>1h old) must be purged by cleanupOldTmpFiles")
            self.assertTrue(os.path.exists(fresh_tmp), "Fresh tmp file (<1h old) must not be purged")

            # Clean up the fresh test file
            if os.path.exists(fresh_tmp):
                os.remove(fresh_tmp)
        finally:
            self.run_locutus(["close", agent])

    def test_23_readme_example_config_comprehensive(self):
        """Fact-check: verify that the exact example .locutus.toml in README.md is extracted from docs and parsed correctly."""
        import re
        readme_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "README.md"))
        self.assertTrue(os.path.isfile(readme_path), f"README.md not found at {readme_path}")
        with open(readme_path, "r", encoding="utf-8") as f:
            readme_text = f.read()

        # Dynamically extract example toml snippet from README.md to guarantee 0 documentation drift
        match = re.search(r"### Example `\.locutus\.toml`\s+```toml\n(.*?)```", readme_text, re.DOTALL)
        self.assertIsNotNone(match, "README.md must contain ### Example `.locutus.toml` TOML code block")
        extracted_toml = match.group(1).strip()
        self.assertIn("redis_url", extracted_toml)
        self.assertIn("[profiles.staging]", extracted_toml)
        self.assertIn("[profiles.prod]", extracted_toml)

        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, ".locutus.toml")
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write(extracted_toml + "\n")

            clean_env = {
                "LOCUTUS_REDIS_URL": "",
                "LOCUTUS_REDIS_PREFIX": "",
                "LOCUTUS_PROJECT": "",
                "LOCUTUS_ENCRYPT": "",
                "LOCUTUS_CLUSTER": "",
            }

            # 1. Root default profile verification with schema validation
            res = self.run_locutus(["config", "show", "--json"], env_overrides=clean_env, cwd=tmp_dir)
            self.assertEqual(res.returncode, 0)
            data = LocutusPlugin.validate_json_schema("config", res.stdout)

            self.assertEqual(data["redis_url"]["value"], "redis://127.0.0.1:6379")
            self.assertEqual(data["redis_url"]["source"], "workspace config")
            self.assertEqual(data["prefix"]["value"], "locutus:")
            self.assertEqual(data["project"]["value"], "my-project")
            self.assertEqual(data["encrypt"]["value"], "false")
            self.assertEqual(data["cluster"]["value"], "false")
            self.assertEqual(data["heartbeat_ttl"]["value"], "150")
            self.assertEqual(data["message_ttl"]["value"], "604800")
            self.assertEqual(data["listen_timeout"]["value"], "90")
            self.assertEqual(os.path.normpath(data["secret_file"]["value"]), os.path.normpath(os.path.expanduser("~/.config/locutus/secret")))

            # 2. Staging profile verification with schema validation
            res_stg = self.run_locutus(["--profile", "staging", "config", "show", "--json"], env_overrides=clean_env, cwd=tmp_dir)
            self.assertEqual(res_stg.returncode, 0)
            stg_data = LocutusPlugin.validate_json_schema("config", res_stg.stdout)
            self.assertEqual(stg_data["redis_url"]["value"], "rediss://staging.internal:6380")
            self.assertEqual(stg_data["prefix"]["value"], "stg:locutus:")
            self.assertEqual(stg_data["encrypt"]["value"], "true")
            self.assertEqual(stg_data.get("active_profile"), "staging")

            # 3. Prod profile verification (cluster mode hashtag auto-applied)
            res_prod = self.run_locutus(["--profile", "prod", "config", "show", "--json"], env_overrides=clean_env, cwd=tmp_dir)
            self.assertEqual(res_prod.returncode, 0)
            prod_data = LocutusPlugin.validate_json_schema("config", res_prod.stdout)
            self.assertEqual(prod_data["redis_url"]["value"], "rediss://prod-cluster.internal:6379")
            self.assertEqual(prod_data["cluster"]["value"], "true")
            self.assertEqual(prod_data["prefix"]["value"], "{locutus:my-project}:")
            self.assertEqual(prod_data["encrypt"]["value"], "true")
            self.assertEqual(prod_data.get("active_profile"), "prod")

            # 4. Negative control: Unconfigured profile fallback to workspace defaults
            res_none = self.run_locutus(["--profile", "unconfigured_demo", "config", "show", "--json"], env_overrides=clean_env, cwd=tmp_dir)
            self.assertEqual(res_none.returncode, 0)
            none_data = LocutusPlugin.validate_json_schema("config", res_none.stdout)
            self.assertEqual(none_data.get("active_profile"), "unconfigured_demo")
            self.assertEqual(none_data["redis_url"]["value"], "redis://127.0.0.1:6379")

    def test_24_all_runtime_config_options_behavior(self):
        """Verify that every individual configuration option directly alters runtime Redis operations and TTL bounds."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            custom_secret_file = os.path.join(tmp_dir, "custom_secret.key")
            custom_secret_val = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            with open(custom_secret_file, "w") as f:
                f.write(custom_secret_val + "\n")
            if os.name != "nt":
                os.chmod(custom_secret_file, 0o600)

            cfg_content = f"""redis_url = "{REDIS_URL}"
prefix = "{TEST_PREFIX}"
project = "cfg_test_proj"
agent_name = "configured_agent_99"
heartbeat_ttl = 45
message_ttl = 75
listen_timeout = 1
secret_file = "{custom_secret_file}"
"""
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

            agent = "agent_heartbeat_ttl_test"
            self.run_locutus(["close", agent], env_overrides=clean_env, cwd=tmp_dir)

            try:
                # B. Test heartbeat_ttl strictly sets Redis EX TTL (bounded within 40..45s)
                self.run_locutus(["open", agent, "worker"], env_overrides=clean_env, cwd=tmp_dir)
                ttl_res = subprocess.run(
                    ["redis-cli", "-u", REDIS_URL, "TTL", f"{TEST_PREFIX}heartbeat:{agent}"],
                    capture_output=True,
                    text=True,
                    check=True
                )
                hb_ttl = int(ttl_res.stdout.strip())
                self.assertTrue(38 <= hb_ttl <= 45, f"Expected heartbeat TTL strictly between 38 and 45, got {hb_ttl}")

                # C. Test message_ttl strictly sets inbox TTL on send (bounded within 68..75s)
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
                self.assertTrue(68 <= msg_ttl <= 75, f"Expected inbox TTL strictly between 68 and 75, got {msg_ttl}")

                # D. Test listen_timeout: drain message, then listen with empty inbox (should timeout in 1.0s to 3.0s)
                self.run_locutus(["drain", "1", agent], env_overrides=clean_env, cwd=tmp_dir)
                start_t = time.time()
                res_listen = self.run_locutus(["listen", agent], env_overrides=clean_env, cwd=tmp_dir)
                elapsed = time.time() - start_t
                self.assertEqual(res_listen.returncode, 0)
                self.assertEqual(res_listen.stdout.strip(), "")
                self.assertTrue(0.9 <= elapsed <= 3.5, f"Listen with listen_timeout=1 took unexpected duration: {elapsed}s")

                # E. Test inline secret configuration
                cfg_inline_secret = f"""redis_url = "{REDIS_URL}"
secret = "my_inline_secret_test_555"
"""
                cfg_inline_path = os.path.join(tmp_dir, "inline.toml")
                with open(cfg_inline_path, "w", encoding="utf-8") as f:
                    f.write(cfg_inline_secret)

                res_inline = self.run_locutus(["--config", cfg_inline_path, "get-secret"], env_overrides=clean_env, cwd=tmp_dir)
                self.assertEqual(res_inline.stdout.strip(), "my_inline_secret_test_555")

                # F. Negative control: change heartbeat_ttl to 12s in config and assert Redis TTL reflects exact new bounds
                with open(cfg_path, "w", encoding="utf-8") as f:
                    f.write(cfg_content.replace("heartbeat_ttl = 45", "heartbeat_ttl = 12"))

                agent_alt = "agent_heartbeat_alt_test"
                self.run_locutus(["open", agent_alt, "worker"], env_overrides=clean_env, cwd=tmp_dir)
                alt_ttl_res = subprocess.run(
                    ["redis-cli", "-u", REDIS_URL, "TTL", f"{TEST_PREFIX}heartbeat:{agent_alt}"],
                    capture_output=True,
                    text=True,
                    check=True
                )
                alt_ttl = int(alt_ttl_res.stdout.strip())
                self.assertTrue(8 <= alt_ttl <= 12, f"Expected modified heartbeat TTL between 8 and 12, got {alt_ttl}")
                self.run_locutus(["close", agent_alt], env_overrides=clean_env, cwd=tmp_dir)
            finally:
                self.run_locutus(["close", agent], env_overrides=clean_env, cwd=tmp_dir)

    def test_25_config_init_targets(self):
        """Verify that 'locutus config init' generates valid TOML templates for --project and --user targets with strict permissions and overwrite guards."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            # 1. Project target initialization (--project)
            res_proj = self.run_locutus(["config", "init", "--project"], cwd=tmp_dir)
            self.assertEqual(res_proj.returncode, 0)
            self.assertIn("Initialized Locutus configuration at:", res_proj.stdout)

            proj_file = os.path.join(tmp_dir, ".locutus.toml")
            self.assertTrue(os.path.isfile(proj_file))
            if os.name != "nt":
                self.assertEqual(os.stat(proj_file).st_mode & 0o777, 0o600, "Project config must have 0600 permissions")

            with open(proj_file, "r", encoding="utf-8") as f:
                proj_content = f.read()

            # Assert template comments and default keys
            self.assertIn("# Locutus Configuration File (.locutus.toml)", proj_content)
            self.assertIn("# Redis connection endpoint", proj_content)
            self.assertIn('redis_url = "redis://127.0.0.1:6379"', proj_content)
            self.assertIn('prefix = "locutus:"', proj_content)
            self.assertIn("encrypt = false", proj_content)
            self.assertIn("cluster = false", proj_content)
            self.assertIn("heartbeat_ttl = 150", proj_content)
            self.assertIn("message_ttl = 604800", proj_content)
            self.assertIn("listen_timeout = 0", proj_content)
            self.assertIn("# [profiles.staging]", proj_content)

            # Validate that generated template parses as valid config schema
            clean_env = {
                "LOCUTUS_REDIS_URL": "",
                "LOCUTUS_REDIS_PREFIX": "",
                "LOCUTUS_PROJECT": "",
                "LOCUTUS_ENCRYPT": "",
                "LOCUTUS_CLUSTER": "",
            }
            res_show = self.run_locutus(["config", "show", "--json"], env_overrides=clean_env, cwd=tmp_dir)
            self.assertEqual(res_show.returncode, 0)
            data = LocutusPlugin.validate_json_schema("config", res_show.stdout)
            self.assertEqual(data["redis_url"]["value"], "redis://127.0.0.1:6379")
            self.assertEqual(data["prefix"]["value"], "locutus:")

            # 2. Negative control: Duplicate init without --force fails with exit code 1
            res_dup = self.run_locutus(["config", "init", "--project"], cwd=tmp_dir)
            self.assertEqual(res_dup.returncode, 1)
            self.assertIn("Error: Config file already exists at", res_dup.stderr)
            self.assertIn("(use --force to overwrite)", res_dup.stderr)

            # 3. Overwrite with --force succeeds
            with open(proj_file, "w", encoding="utf-8") as f:
                f.write("corrupted_content = true\n")
            res_force = self.run_locutus(["config", "init", "--project", "--force"], cwd=tmp_dir)
            self.assertEqual(res_force.returncode, 0)
            with open(proj_file, "r", encoding="utf-8") as f:
                self.assertIn("# Locutus Configuration File", f.read())

            # 4. User target initialization (--user) with isolated HOME and XDG_CONFIG_HOME
            with tempfile.TemporaryDirectory() as user_home:
                user_env = {
                    "HOME": user_home,
                    "XDG_CONFIG_HOME": os.path.join(user_home, ".config")
                }
                res_user = self.run_locutus(["config", "init", "--user"], env_overrides=user_env)
                self.assertEqual(res_user.returncode, 0)
                user_file = os.path.join(user_home, ".config", "locutus", "config.toml")
                self.assertTrue(os.path.isfile(user_file), f"Expected user config at {user_file}")
                if os.name != "nt":
                    self.assertEqual(os.stat(user_file).st_mode & 0o777, 0o600, "User config must have 0600 permissions")

                with open(user_file, "r", encoding="utf-8") as f:
                    user_content = f.read()
                self.assertIn("# Locutus Configuration File (.locutus.toml)", user_content)

                # Negative control for user target without --force
                res_user_dup = self.run_locutus(["config", "init", "--user"], env_overrides=user_env)
                self.assertEqual(res_user_dup.returncode, 1)
                self.assertIn("Error: Config file already exists", res_user_dup.stderr)

                # Overwrite with --force succeeds for user target
                with open(user_file, "w", encoding="utf-8") as f:
                    f.write("modified = true\n")
                res_user_force = self.run_locutus(["config", "init", "--user", "--force"], env_overrides=user_env)
                self.assertEqual(res_user_force.returncode, 0)
                with open(user_file, "r", encoding="utf-8") as f:
                    self.assertIn("# Locutus Configuration File", f.read())

    def test_26_status_and_directory_state(self):
        """Test 'locutus status' updates state and activity, asserts last_seen freshness within 2s, and validates schema."""
        # 1. Negative controls: argument validation
        res_missing = self.run_locutus(["status"])
        self.assertEqual(res_missing.returncode, 1)
        self.assertIn("Error: Missing state argument for status command.", res_missing.stderr)
        self.assertIn("Usage: locutus status <idle|busy|error> [activity_text] [name]", res_missing.stderr)

        with tempfile.TemporaryDirectory() as empty_dir:
            res_no_agent = self.run_locutus(["status", "busy", "Working"], cwd=empty_dir, env_overrides={"LOCUTUS_AGENT_NAME": ""})
            self.assertEqual(res_no_agent.returncode, 1)
            self.assertIn("Error: No agent name specified", res_no_agent.stderr)

        agent = "status_worker_test"
        self.run_locutus(["close", agent])
        self.run_locutus(["open", agent, "backend,worker"])

        try:
            # 2. Update status to 'busy'
            now_pre = time.time()
            res_stat = self.run_locutus(["status", "busy", "Compiling LLVM", agent])
            self.assertEqual(res_stat.returncode, 0)
            self.assertEqual(res_stat.stdout.strip(), "OK")

            # 3. Direct Redis inspection: state, activity, and last_seen freshness within 2 seconds
            meta = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "HMGET", f"{TEST_PREFIX}agent:{agent}", "state", "activity", "last_seen"],
                capture_output=True, text=True, check=True
            ).stdout.strip().splitlines()
            self.assertEqual(len(meta), 3)
            self.assertEqual(meta[0], "busy")
            self.assertEqual(meta[1], "Compiling LLVM")
            last_seen = int(meta[2])
            self.assertTrue(abs(last_seen - now_pre) <= 2.5, f"last_seen ({last_seen}) must be within 2.5s of current time ({now_pre})")

            # 4. Check directory via who --json with schema validation
            res_who_json = self.run_locutus(["who", "--json", "*"])
            self.assertEqual(res_who_json.returncode, 0)
            agents_list = LocutusPlugin.validate_json_schema("directory", res_who_json.stdout)
            matching = [a for a in agents_list if a["agent"] == agent]
            self.assertEqual(len(matching), 1, f"Agent {agent} must appear in directory")
            self.assertEqual(matching[0]["state"], "BUSY")
            self.assertEqual(matching[0]["activity"], "Compiling LLVM")
            self.assertEqual(matching[0]["status"], "ACTIVE")

            # 5. Update status to 'idle'
            now_idle = time.time()
            res_stat2 = self.run_locutus(["status", "idle", "Awaiting jobs", agent])
            self.assertEqual(res_stat2.returncode, 0)
            self.assertEqual(res_stat2.stdout.strip(), "OK")

            meta_idle = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "HMGET", f"{TEST_PREFIX}agent:{agent}", "state", "activity", "last_seen"],
                capture_output=True, text=True, check=True
            ).stdout.strip().splitlines()
            self.assertEqual(meta_idle[0], "idle")
            self.assertEqual(meta_idle[1], "Awaiting jobs")
            last_seen_idle = int(meta_idle[2])
            self.assertTrue(abs(last_seen_idle - now_idle) <= 2.5)

            # Re-verify directory JSON schema
            res_who_json2 = self.run_locutus(["who", "--json", "*"])
            self.assertEqual(res_who_json2.returncode, 0)
            agents_list2 = LocutusPlugin.validate_json_schema("directory", res_who_json2.stdout)
            matching2 = [a for a in agents_list2 if a["agent"] == agent]
            self.assertEqual(len(matching2), 1)
            self.assertEqual(matching2[0]["state"], "IDLE")
            self.assertEqual(matching2[0]["activity"], "Awaiting jobs")
        finally:
            self.run_locutus(["close", agent])

    def test_27_distributed_locking(self):
        """Test 'locutus lock' acquisition, mutual exclusion, unauthorized unlock rejection, TTL bounds, and fencing tokens."""
        lock_name = "deploy_mutex_test"
        lock_key = f"{TEST_PREFIX}lock:{{{lock_name}}}"
        fencing_key = f"{TEST_PREFIX}lock:fencing:{{{lock_name}}}"

        # Clean slate in Redis
        subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", lock_key, fencing_key], check=True, capture_output=True)

        # 1. Missing argument negative controls
        res_no_name = self.run_locutus(["lock"])
        self.assertEqual(res_no_name.returncode, 1)
        self.assertIn("Error: Missing lock name.", res_no_name.stderr)

        res_no_unlock_name = self.run_locutus(["unlock"])
        self.assertEqual(res_no_unlock_name.returncode, 1)
        self.assertIn("Error: Missing lock name.", res_no_unlock_name.stderr)
        self.assertIn("Usage: locutus unlock <lock_name>", res_no_unlock_name.stderr)

        try:
            # 2. Acquire lock with owner 'agent_lock_owner'
            res_lock = self.run_locutus(["--agent-name=agent_lock_owner", "lock", lock_name, "10"])
            self.assertEqual(res_lock.returncode, 0)
            self.assertIn(f"LOCKED {lock_name} by agent_lock_owner", res_lock.stdout)

            # Direct Redis inspection: verify owner and TTL bounds
            owner_in_redis = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "GET", lock_key],
                capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertEqual(owner_in_redis, "agent_lock_owner")

            ttl_in_redis = int(subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "TTL", lock_key],
                capture_output=True, text=True, check=True
            ).stdout.strip())
            self.assertTrue(7 <= ttl_in_redis <= 10, f"Expected lock TTL between 7 and 10, got {ttl_in_redis}")

            # 3. Second process ('intruder_agent') attempts to acquire held lock -> must fail with exit code 1
            res_conflict = self.run_locutus(["--agent-name=intruder_agent", "lock", lock_name, "10"])
            self.assertEqual(res_conflict.returncode, 1)
            self.assertIn("Error: Lock 'deploy_mutex_test' is already held.", res_conflict.stderr)

            # Lock owner in Redis must remain 'agent_lock_owner'
            self.assertEqual(subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "GET", lock_key],
                capture_output=True, text=True, check=True
            ).stdout.strip(), "agent_lock_owner")

            # 4. Unauthorized unlock negative control: intruder cannot release lock held by agent_lock_owner
            res_unlock_bad = self.run_locutus(["--agent-name=intruder_agent", "unlock", lock_name])
            self.assertEqual(res_unlock_bad.returncode, 1)
            self.assertIn("Error: Cannot unlock 'deploy_mutex_test': not owner or lock not found.", res_unlock_bad.stderr)

            # Lock must still be held
            self.assertEqual(subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "EXISTS", lock_key],
                capture_output=True, text=True, check=True
            ).stdout.strip(), "1")

            # 5. Authorized unlock by owner succeeds
            res_unlock = self.run_locutus(["--agent-name=agent_lock_owner", "unlock", lock_name])
            self.assertEqual(res_unlock.returncode, 0)
            self.assertIn(f"UNLOCKED {lock_name}", res_unlock.stdout)

            # Lock key must be deleted in Redis
            self.assertEqual(subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "EXISTS", lock_key],
                capture_output=True, text=True, check=True
            ).stdout.strip(), "0")

            # 6. Re-unlock negative control: unlocking an already released lock returns exit code 1
            res_reunlock = self.run_locutus(["--agent-name=agent_lock_owner", "unlock", lock_name])
            self.assertEqual(res_reunlock.returncode, 1)
            self.assertIn("not owner or lock not found", res_reunlock.stderr)

            # 7. Monotonic fencing tokens test
            res_fencing_1 = self.run_locutus(["--agent-name=agent_lock_owner", "lock", lock_name, "10", "--fencing", "--raw"])
            self.assertEqual(res_fencing_1.returncode, 0)
            token_1 = int(res_fencing_1.stdout.strip())
            self.run_locutus(["--agent-name=agent_lock_owner", "unlock", lock_name])

            res_fencing_2 = self.run_locutus(["--agent-name=agent_lock_owner", "lock", lock_name, "10", "--fencing", "--raw"])
            self.assertEqual(res_fencing_2.returncode, 0)
            token_2 = int(res_fencing_2.stdout.strip())
            self.run_locutus(["--agent-name=agent_lock_owner", "unlock", lock_name])

            self.assertEqual(token_2, token_1 + 1, "Fencing tokens must be strictly monotonic consecutive integers")
        finally:
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", lock_key, fencing_key], check=True, capture_output=True)

    def test_28_task_queue_enqueue_and_work(self):
        """Test competing-consumers task queue: argument validation, 5-task FIFO ordering, wire envelope & Pydantic validation."""
        qname = "render_farm_jobs"
        qkey = f"{TEST_PREFIX}queue:{{{qname}}}"

        # 1. Argument validation negative controls
        res_no_q = self.run_locutus(["enqueue"])
        self.assertEqual(res_no_q.returncode, 1)
        self.assertIn("Error: Missing queue name.", res_no_q.stderr)

        res_no_work_q = self.run_locutus(["work"])
        self.assertEqual(res_no_work_q.returncode, 1)
        self.assertIn("Error: Missing queue name.", res_no_work_q.stderr)

        # 2. Clean slate in Redis
        subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", qkey], check=True, capture_output=True)

        try:
            # 3. Enqueue 5 distinct tasks
            enqueued_tasks = []
            for i in range(1, 6):
                subj = f"Render Frame {i * 10}"
                bdy = f"blender -b project.blend -f {i * 10} --output /tmp/frame_{i}.png"
                res_enq = self.run_locutus([
                    "enqueue", qname,
                    "--subject", subj,
                    "--body", bdy,
                    "--type", "task",
                    "--from", "scheduler_agent",
                ])
                self.assertEqual(res_enq.returncode, 0)
                msg_id = res_enq.stdout.strip()
                self.assertTrue(msg_id.startswith("msg_"))
                enqueued_tasks.append((msg_id, subj, bdy))

            # 4. Assert Redis queue length is exactly 5
            qlen_5 = int(subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "LLEN", qkey],
                capture_output=True, text=True, check=True
            ).stdout.strip())
            self.assertEqual(qlen_5, 5, "Queue must contain exactly 5 tasks")

            # 5. Consume tasks one by one and assert strict FIFO ordering
            for idx, (exp_id, exp_subj, exp_bdy) in enumerate(enqueued_tasks, start=1):
                res_work = self.run_locutus(["work", qname, "5"])
                self.assertEqual(res_work.returncode, 0)

                # Validate wire envelope schema
                env = LocutusPlugin.validate_wire_envelope(res_work.stdout.strip())
                self.assertEqual(env["id"], exp_id)
                self.assertEqual(env["from"], "scheduler_agent")
                self.assertEqual(env["to"], f"queue:{qname}")
                self.assertEqual(env["subject"], exp_subj)
                self.assertEqual(env["body"], exp_bdy)

                # Validate Pydantic schema
                msg = LocutusMessage.model_validate_json(res_work.stdout)
                self.assertEqual(msg.id, exp_id)
                self.assertEqual(msg.from_agent, "scheduler_agent")
                self.assertEqual(msg.to_agent, f"queue:{qname}")
                self.assertEqual(msg.subject, exp_subj)
                self.assertEqual(msg.body, exp_bdy)

                # Verify queue decrements monotonically in Redis
                qlen_curr = int(subprocess.run(
                    ["redis-cli", "-u", REDIS_URL, "LLEN", qkey],
                    capture_output=True, text=True, check=True
                ).stdout.strip())
                self.assertEqual(qlen_curr, 5 - idx)

            # 6. Negative control: Work on empty queue with 1s timeout returns empty stdout in ~1s
            start_t = time.time()
            res_empty = self.run_locutus(["work", qname, "1"])
            elapsed = time.time() - start_t
            self.assertEqual(res_empty.returncode, 0)
            self.assertEqual(res_empty.stdout.strip(), "")
            self.assertTrue(0.8 <= elapsed <= 2.5, f"Empty work timeout took unexpected time: {elapsed}s")
        finally:
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", qkey], check=True, capture_output=True)

    def test_29_synchronous_request_rpc(self):
        """Test synchronous RPC 'locutus request': correlation matching, ephemeral queue deletion, raw mode, and timeout negative control."""
        # 1. Argument validation negative controls
        res_missing_all = self.run_locutus(["request"])
        self.assertEqual(res_missing_all.returncode, 1)
        self.assertIn("Error: Missing required arguments. --to, --subject, and --body are required.", res_missing_all.stderr)

        res_missing_body = self.run_locutus(["request", "--to", "some_agent", "--subject", "Hello"])
        self.assertEqual(res_missing_body.returncode, 1)
        self.assertIn("Error: Missing required arguments.", res_missing_body.stderr)

        ts = int(time.time() * 1000)
        server_agent = f"rpc_srv_{ts}"
        client_agent = f"rpc_cli_{ts}"
        self.run_locutus(["close", server_agent])
        self.run_locutus(["open", server_agent, "rpc"])

        try:
            captured_reply_to = []

            def server_loop():
                req_res = self.run_locutus(["listen", server_agent, "5"])
                if req_res.returncode == 0 and req_res.stdout.strip() not in ["", "(nil)"]:
                    data = LocutusPlugin.validate_wire_envelope(req_res.stdout.strip())
                    reply_to = data.get("reply_to")
                    # Assert strict correlation ID matching: reply_to must equal 'reply:' + request_id
                    assert reply_to == f"reply:{data['id']}", f"Correlation failure: reply_to {reply_to} != reply:{data['id']}"
                    captured_reply_to.append(reply_to)
                    self.run_locutus([
                        "--agent-name=" + server_agent,
                        "send",
                        "--to", reply_to,
                        "--type", "reply",
                        "--subject", "RPC Result",
                        "--body", "answer:42"
                    ])

            # 2. Synchronous request execution
            t = threading.Thread(target=server_loop)
            t.start()
            time.sleep(0.3)

            res_req = self.run_locutus([
                f"--agent-name={client_agent}",
                "request",
                "--to", server_agent,
                "--subject", "Math Question",
                "--body", "What is 6 * 7?",
                "--timeout", "8"
            ])
            t.join(timeout=10)

            self.assertEqual(res_req.returncode, 0)
            LocutusPlugin.validate_wire_envelope(res_req.stdout.strip())
            reply_msg = LocutusMessage.model_validate_json(res_req.stdout)
            self.assertEqual(reply_msg.body, "answer:42")
            self.assertEqual(reply_msg.subject, "RPC Result")

            # 3. Verify ephemeral queue was destroyed in Redis after read
            self.assertTrue(len(captured_reply_to) > 0, "Server must have captured reply_to")
            ephemeral_key = f"{TEST_PREFIX}inbox:{captured_reply_to[0]}"
            exists_in_redis = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "EXISTS", ephemeral_key],
                capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertEqual(exists_in_redis, "0", f"Ephemeral queue {ephemeral_key} must be deleted after BRPOP read")

            # 4. Test --raw mode
            t2 = threading.Thread(target=server_loop)
            t2.start()
            time.sleep(0.3)

            res_raw = self.run_locutus([
                f"--agent-name={client_agent}",
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

            # 5. Negative control: Timeout when server does not respond
            start_to = time.time()
            res_timeout = self.run_locutus([
                f"--agent-name={client_agent}",
                "request",
                "--to", server_agent,
                "--subject", "Unanswered Ping",
                "--body", "Nobody home",
                "--timeout", "1"
            ])
            elapsed_to = time.time() - start_to
            self.assertEqual(res_timeout.returncode, 1)
            self.assertIn(f"Error: Request timed out waiting for reply from {server_agent}", res_timeout.stderr)
            self.assertTrue(0.8 <= elapsed_to <= 3.0)
        finally:
            self.run_locutus(["close", server_agent])

    def test_30_ephemeral_pub_sub(self):
        """Test ephemeral streaming with 'locutus pub' and 'locutus sub': argument validation, live receipt, and late-subscriber non-persistence."""
        # 1. Negative controls: argument validation
        res_pub_missing = self.run_locutus(["pub"])
        self.assertEqual(res_pub_missing.returncode, 1)
        self.assertIn("Error: Missing arguments for pub command.", res_pub_missing.stderr)
        self.assertIn("Usage: locutus pub <channel> <message>", res_pub_missing.stderr)

        res_sub_missing = self.run_locutus(["sub"])
        self.assertEqual(res_sub_missing.returncode, 1)
        self.assertIn("Error: Missing channel name for sub command.", res_sub_missing.stderr)
        self.assertIn("Usage: locutus sub <channel> [timeout_sec]", res_sub_missing.stderr)

        channel = f"telemetry_{int(time.time() * 1000)}"
        message = "METRIC:cpu_temp=48C"

        # 2. Live subscriber receives message in real-time
        received = []
        def sub_worker():
            res_sub = self.run_locutus(["sub", channel, "5"])
            if res_sub.returncode == 0:
                received.append(res_sub.stdout.strip())

        t = threading.Thread(target=sub_worker)
        t.start()
        time.sleep(0.4)

        res_pub = self.run_locutus(["pub", channel, message])
        self.assertEqual(res_pub.returncode, 0)
        t.join(timeout=6)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0], message)

        # 3. Non-persistence semantics: Late subscriber receives 0 messages
        # Publish message when no subscriber is active
        res_pub_ghost = self.run_locutus(["pub", channel, "GHOST_METRIC:unheard=99"])
        self.assertEqual(res_pub_ghost.returncode, 0)

        # Late subscriber arrives after publication
        start_late = time.time()
        res_late = self.run_locutus(["sub", channel, "1"])
        elapsed_late = time.time() - start_late
        self.assertEqual(res_late.returncode, 0)
        self.assertEqual(res_late.stdout.strip(), "", "Late subscriber must receive 0 past messages (non-persistence guarantee)")
        self.assertTrue(0.8 <= elapsed_late <= 2.5)

        # 4. Silent channel timeout verification
        start_silent = time.time()
        res_silent = self.run_locutus(["sub", f"silent_{int(time.time()*1000)}", "1"])
        elapsed_silent = time.time() - start_silent
        self.assertEqual(res_silent.returncode, 0)
        self.assertEqual(res_silent.stdout.strip(), "")
        self.assertTrue(0.8 <= elapsed_silent <= 2.5)

    def test_31_who_flags_and_json(self):
        """Test 'locutus who' supports -a, --all, and --json output with full schema validation and tag filtering."""
        agent = "who_test_bot"
        self.run_locutus(["close", agent])
        self.run_locutus(["open", agent, "backend,testgroup"])

        try:
            # 1. Test -a flag (table output formatting)
            res_a = self.run_locutus(["who", "-a"])
            self.assertEqual(res_a.returncode, 0)
            self.assertIn("AGENT", res_a.stdout)
            self.assertIn("STATUS", res_a.stdout)
            self.assertIn("STATE", res_a.stdout)
            self.assertIn("TAGS", res_a.stdout)
            self.assertIn("ACTIVITY", res_a.stdout)
            self.assertIn(agent, res_a.stdout)
            self.assertIn("ACTIVE", res_a.stdout)

            # 2. Test --all flag
            res_all = self.run_locutus(["who", "--all"])
            self.assertEqual(res_all.returncode, 0)
            self.assertIn(agent, res_all.stdout)

            # 3. Test --json flag with complete schema validation
            res_json = self.run_locutus(["who", "--json", "-a"])
            self.assertEqual(res_json.returncode, 0)
            agents = LocutusPlugin.validate_json_schema("directory", res_json.stdout)
            self.assertIsInstance(agents, list)

            # Validate structural integrity of every agent entry in the directory
            for a_entry in agents:
                self.assertIn("agent", a_entry)
                self.assertIn("status", a_entry)
                self.assertIn("tags", a_entry)
                self.assertIn("state", a_entry)
                self.assertIn("activity", a_entry)
                self.assertIn(a_entry["status"], ["ACTIVE", "EXPIRED"])
                self.assertIsInstance(a_entry["tags"], list)
                self.assertIsInstance(a_entry["state"], str)
                self.assertIsInstance(a_entry["activity"], str)

            match = [a for a in agents if a["agent"] == agent]
            self.assertEqual(len(match), 1)
            self.assertEqual(match[0]["status"], "ACTIVE")
            self.assertEqual(match[0]["state"], "IDLE")
            self.assertIn("backend", match[0]["tags"])
            self.assertIn("testgroup", match[0]["tags"])

            # 4. Tag filtering via who <tag> --json
            res_filtered = self.run_locutus(["who", "backend", "--json"])
            self.assertEqual(res_filtered.returncode, 0)
            filtered_agents = LocutusPlugin.validate_json_schema("directory", res_filtered.stdout)
            match_filtered = [a for a in filtered_agents if a["agent"] == agent]
            self.assertEqual(len(match_filtered), 1)

            # 5. Negative control: Non-matching tag returns empty list
            res_empty_tag = self.run_locutus(["who", "nonexistent_tag_xyz999", "--json"])
            self.assertEqual(res_empty_tag.returncode, 0)
            empty_agents = LocutusPlugin.validate_json_schema("directory", res_empty_tag.stdout)
            self.assertEqual(empty_agents, [])
        finally:
            self.run_locutus(["close", agent])

    def test_32_listen_auto_registers_in_directory(self):
        """Test that listening with an agent name auto-registers it in the active directory and renews heartbeats."""
        agent = "auto_listener_bot"
        self.run_locutus(["close", agent])

        try:
            # 1. Send a message to the agent while it is offline
            res_send = self.run_locutus([
                "send", "--from", "sender_bot", "--to", agent, "--subject", "Wake", "--body", "Wakeup"
            ])
            self.assertEqual(res_send.returncode, 0)

            # 2. Agent listens for 1 second (consuming the message)
            res_listen = self.run_locutus(["listen", agent, "1"])
            self.assertEqual(res_listen.returncode, 0)

            # Wire envelope and Pydantic validation
            parsed_msg = json.loads(res_listen.stdout.strip())
            LocutusPlugin.validate_wire_envelope(parsed_msg)
            msg_obj = LocutusMessage.model_validate(parsed_msg)
            self.assertEqual(msg_obj.to_agent, agent)
            self.assertEqual(msg_obj.subject, "Wake")
            self.assertEqual(msg_obj.body, "Wakeup")

            # 3. Verify agent is immediately visible as ACTIVE in locutus who with valid directory schema
            res_who = self.run_locutus(["who", "-a", "--json"])
            self.assertEqual(res_who.returncode, 0)
            agents = LocutusPlugin.validate_json_schema("directory", res_who.stdout)
            match = [a for a in agents if a["agent"] == agent]
            self.assertEqual(len(match), 1, f"Expected {agent} to be registered in who output")
            self.assertEqual(match[0]["status"], "ACTIVE")

            # Assert Redis membership and initial heartbeat key existence
            chk_mem = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "SISMEMBER", f"{TEST_PREFIX}active_agents", agent],
                capture_output=True, text=True, check=True
            )
            self.assertEqual(chk_mem.stdout.strip(), "1", f"{agent} missing from active_agents set")

            chk_hb = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "EXISTS", f"{TEST_PREFIX}heartbeat:{agent}"],
                capture_output=True, text=True, check=True
            )
            self.assertEqual(chk_hb.stdout.strip(), "1", f"Heartbeat key missing for {agent}")

            # 4. Extended listen heartbeat renewal verification
            renew_agent = f"renew_bot_{int(time.time() * 1000)}"
            renew_hb_key = f"{TEST_PREFIX}heartbeat:{renew_agent}"
            self.run_locutus(["close", renew_agent])

            with tempfile.TemporaryDirectory() as hb_tmp:
                hb_cfg_path = os.path.join(hb_tmp, "heartbeat.toml")
                with open(hb_cfg_path, "w", encoding="utf-8") as f:
                    f.write(f'redis_url = "{REDIS_URL}"\nprefix = "{TEST_PREFIX}"\nheartbeat_ttl = 4\n')

                # Start listener process with short heartbeat TTL (4s) so renewal chunk is 2s (4 // 2)
                p = subprocess.Popen(
                    [BIN_PATH, "--config", hb_cfg_path, "listen", renew_agent, "5"],
                    env=self.env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )

                try:
                    # Wait for listener to register initial heartbeat
                    registered = False
                    for _ in range(20):
                        time.sleep(0.1)
                        chk_start = subprocess.run(
                            ["redis-cli", "-u", REDIS_URL, "EXISTS", renew_hb_key],
                            capture_output=True, text=True, check=True
                        )
                        if chk_start.stdout.strip() == "1":
                            registered = True
                            break
                    self.assertTrue(registered, f"Listener {renew_agent} failed to register heartbeat in Redis")

                    # Initial TTL must be <= 4 and >= 2
                    ttl1 = int(subprocess.run(
                        ["redis-cli", "-u", REDIS_URL, "TTL", renew_hb_key],
                        capture_output=True, text=True, check=True
                    ).stdout.strip())
                    self.assertGreaterEqual(ttl1, 2)
                    self.assertLessEqual(ttl1, 4)

                    # Wait 2.6s (past the 2.0s poll chunk boundary).
                    # Without heartbeat renewal, remaining TTL would be <= 1.4s.
                    # With renewal at the 2.0s boundary, TTL was refreshed to 4s, so TTL > 2.0s.
                    time.sleep(2.6)
                    ttl2 = int(subprocess.run(
                        ["redis-cli", "-u", REDIS_URL, "TTL", renew_hb_key],
                        capture_output=True, text=True, check=True
                    ).stdout.strip())
                    self.assertGreaterEqual(
                        ttl2, 2,
                        f"Heartbeat renewal failed: expected TTL >= 2 after 2.6s, but got {ttl2} (key would expire)"
                    )

                    # Wait for listener process to complete its 5s timeout
                    stdout, stderr = p.communicate(timeout=5)
                    self.assertEqual(p.returncode, 0)
                    self.assertEqual(stdout.strip(), "", "Listener must exit silently with empty stdout on timeout")
                finally:
                    if p.poll() is None:
                        p.kill()
                        p.wait()
                    self.run_locutus(["close", renew_agent])
                    chk_renew_del = subprocess.run(
                        ["redis-cli", "-u", REDIS_URL, "EXISTS", renew_hb_key],
                        capture_output=True, text=True, check=True
                    )
                    self.assertEqual(chk_renew_del.stdout.strip(), "0")

            # 5. Negative control: unregistered agent does not appear in active who directory
            res_who_active = self.run_locutus(["who", "--json"])
            self.assertEqual(res_who_active.returncode, 0)
            active_agents = LocutusPlugin.validate_json_schema("directory", res_who_active.stdout)
            match_fake = [a for a in active_agents if a["agent"] == "nonexistent_fake_agent_999"]
            self.assertEqual(match_fake, [])
        finally:
            self.run_locutus(["close", agent])
            chk_del = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "EXISTS", f"{TEST_PREFIX}heartbeat:{agent}"],
                capture_output=True, text=True, check=True
            )
            self.assertEqual(chk_del.stdout.strip(), "0")

    def test_33_multi_agent_workspace_and_env_isolation(self):
        """Test that workspace-scoped .locutus.agent, LOCUTUS_AGENT_NAME, and project namespaces isolate agents."""
        tmp1 = tempfile.mkdtemp(prefix="locutus_ws1_")
        tmp2 = tempfile.mkdtemp(prefix="locutus_ws2_")
        proj1 = tempfile.mkdtemp(prefix="locutus_proj1_")
        proj2 = tempfile.mkdtemp(prefix="locutus_proj2_")

        try:
            # 1. Open agent 1 in directory 1
            res1 = self.run_locutus(["open", "agent_one_ws", "teamA"], cwd=tmp1)
            self.assertEqual(res1.returncode, 0)
            agent_file1 = os.path.join(tmp1, ".locutus.agent")
            self.assertTrue(os.path.isfile(agent_file1))
            with open(agent_file1, "r", encoding="utf-8") as f:
                self.assertEqual(f.read().strip(), "agent_one_ws")
            if os.name != "nt":
                self.assertEqual(os.stat(agent_file1).st_mode & 0o777, 0o600)

            # 2. Open agent 2 in directory 2
            res2 = self.run_locutus(["open", "agent_two_ws", "teamB"], cwd=tmp2)
            self.assertEqual(res2.returncode, 0)
            agent_file2 = os.path.join(tmp2, ".locutus.agent")
            self.assertTrue(os.path.isfile(agent_file2))
            with open(agent_file2, "r", encoding="utf-8") as f:
                self.assertEqual(f.read().strip(), "agent_two_ws")
            if os.name != "nt":
                self.assertEqual(os.stat(agent_file2).st_mode & 0o777, 0o600)

            # 3. Negative control: Send message ONLY to agent_two_ws
            self.run_locutus([
                "send", "--from", "sender_bot", "--to", "agent_two_ws",
                "--subject", "Dir2Only", "--body", "PayloadTwo"
            ])

            # In dir1, listen without passing name with 1s timeout: MUST NOT receive agent_two_ws message
            listen_iso = self.run_locutus(["listen", "1"], cwd=tmp1)
            self.assertEqual(listen_iso.returncode, 0)
            self.assertEqual(listen_iso.stdout.strip(), "", "Workspace 1 must not consume Workspace 2 messages")

            # 4. In dir1, send message to agent_one_ws and consume it
            self.run_locutus([
                "send", "--from", "sender_bot", "--to", "agent_one_ws",
                "--subject", "Dir1", "--body", "Payload1"
            ])
            listen1 = self.run_locutus(["listen", "2"], cwd=tmp1)
            self.assertEqual(listen1.returncode, 0)
            data1 = json.loads(listen1.stdout.strip())
            LocutusPlugin.validate_wire_envelope(data1)
            msg1 = LocutusMessage.model_validate(data1)
            self.assertEqual(msg1.to_agent, "agent_one_ws")
            self.assertEqual(msg1.from_agent, "sender_bot")
            self.assertEqual(msg1.body, "Payload1")

            # 5. In dir2, consume the pending message for agent_two_ws
            listen2 = self.run_locutus(["listen", "2"], cwd=tmp2)
            self.assertEqual(listen2.returncode, 0)
            data2 = json.loads(listen2.stdout.strip())
            LocutusPlugin.validate_wire_envelope(data2)
            msg2 = LocutusMessage.model_validate(data2)
            self.assertEqual(msg2.to_agent, "agent_two_ws")
            self.assertEqual(msg2.from_agent, "sender_bot")
            self.assertEqual(msg2.body, "PayloadTwo")

            # 6. LOCUTUS_AGENT_NAME env var overrides workspace directory file
            env_override = {"LOCUTUS_AGENT_NAME": "agent_override_env"}
            self.run_locutus([
                "send", "--from", "sender_bot", "--to", "agent_override_env",
                "--subject", "Env", "--body", "EnvPayload"
            ])
            listen_env = self.run_locutus(["listen", "2"], cwd=tmp1, env_overrides=env_override)
            self.assertEqual(listen_env.returncode, 0)
            data_env = json.loads(listen_env.stdout.strip())
            LocutusPlugin.validate_wire_envelope(data_env)
            msg_env = LocutusMessage.model_validate(data_env)
            self.assertEqual(msg_env.to_agent, "agent_override_env")
            self.assertEqual(msg_env.body, "EnvPayload")

            # 7. Cross-project isolation: verify directory queries are scoped to active project
            with open(os.path.join(proj1, ".locutus.toml"), "w", encoding="utf-8") as f:
                f.write(f'redis_url = "{REDIS_URL}"\nprefix = "{TEST_PREFIX}"\nproject = "projAlpha"\n')
            with open(os.path.join(proj2, ".locutus.toml"), "w", encoding="utf-8") as f:
                f.write(f'redis_url = "{REDIS_URL}"\nprefix = "{TEST_PREFIX}"\nproject = "projBeta"\n')

            no_env_proj = {"LOCUTUS_PROJECT": ""}
            self.run_locutus(["open", "alpha_bot", "worker"], cwd=proj1, env_overrides=no_env_proj)
            self.run_locutus(["open", "beta_bot", "worker"], cwd=proj2, env_overrides=no_env_proj)

            # In proj1, 'locutus who --json' filters by projAlpha
            who1 = self.run_locutus(["who", "--json"], cwd=proj1, env_overrides=no_env_proj)
            self.assertEqual(who1.returncode, 0)
            agents1 = LocutusPlugin.validate_json_schema("directory", who1.stdout)
            match1_alpha = [a for a in agents1 if a["agent"] == "alpha_bot"]
            match1_beta = [a for a in agents1 if a["agent"] == "beta_bot"]
            self.assertEqual(len(match1_alpha), 1, "alpha_bot must appear in projAlpha who directory")
            self.assertEqual(len(match1_beta), 0, "beta_bot must NOT leak into projAlpha who directory")

            # In proj2, 'locutus who --json' filters by projBeta
            who2 = self.run_locutus(["who", "--json"], cwd=proj2, env_overrides=no_env_proj)
            self.assertEqual(who2.returncode, 0)
            agents2 = LocutusPlugin.validate_json_schema("directory", who2.stdout)
            match2_beta = [a for a in agents2 if a["agent"] == "beta_bot"]
            match2_alpha = [a for a in agents2 if a["agent"] == "alpha_bot"]
            self.assertEqual(len(match2_beta), 1, "beta_bot must appear in projBeta who directory")
            self.assertEqual(len(match2_alpha), 0, "alpha_bot must NOT leak into projBeta who directory")

        finally:
            shutil.rmtree(tmp1, ignore_errors=True)
            shutil.rmtree(tmp2, ignore_errors=True)
            shutil.rmtree(proj1, ignore_errors=True)
            shutil.rmtree(proj2, ignore_errors=True)
            self.run_locutus(["close", "agent_one_ws"])
            self.run_locutus(["close", "agent_two_ws"])
            self.run_locutus(["close", "agent_override_env"])
            self.run_locutus(["close", "alpha_bot"])
            self.run_locutus(["close", "beta_bot"])

    def test_34_listen_requires_agent_identity(self):
        """Test that calling 'locutus listen' without an agent name or configured identity fails fast with exact error."""
        clean_dir = tempfile.mkdtemp(prefix="locutus_clean_ws_")
        clean_env = {
            "LOCUTUS_AGENT_NAME": "",
            "A2A_NAME": "",
            "MY_NAME": "",
            "HOME": clean_dir,
            "XDG_CONFIG_HOME": clean_dir,
            "USERPROFILE": clean_dir,
        }
        exact_err = (
            "Error: No agent name specified. Run 'locutus open <name>', "
            "pass the agent name ('locutus listen <name>'), or export LOCUTUS_AGENT_NAME=<name>."
        )

        try:
            # 1. No arguments: 'locutus listen'
            res_bare = self.run_locutus(["listen"], cwd=clean_dir, env_overrides=clean_env)
            self.assertEqual(res_bare.returncode, 1)
            self.assertEqual(res_bare.stdout, "")
            self.assertIn(exact_err, res_bare.stderr.strip())

            # 2. Timeout argument only: 'locutus listen 1'
            res_timeout = self.run_locutus(["listen", "1"], cwd=clean_dir, env_overrides=clean_env)
            self.assertEqual(res_timeout.returncode, 1)
            self.assertEqual(res_timeout.stdout, "")
            self.assertIn(exact_err, res_timeout.stderr.strip())

            # 3. Flag argument only: 'locutus listen --force'
            res_flag = self.run_locutus(["listen", "--force"], cwd=clean_dir, env_overrides=clean_env)
            self.assertEqual(res_flag.returncode, 1)
            self.assertEqual(res_flag.stdout, "")
            self.assertIn(exact_err, res_flag.stderr.strip())

            # 4. Flag and timeout argument: 'locutus listen -f 1'
            res_flag_to = self.run_locutus(["listen", "-f", "1"], cwd=clean_dir, env_overrides=clean_env)
            self.assertEqual(res_flag_to.returncode, 1)
            self.assertEqual(res_flag_to.stdout, "")
            self.assertIn(exact_err, res_flag_to.stderr.strip())

            # 5. Positive control: Providing explicit name succeeds (silent timeout exit 0)
            res_named = self.run_locutus(["listen", "explicit_probe_bot", "1"], cwd=clean_dir, env_overrides=clean_env)
            self.assertEqual(res_named.returncode, 0)
            self.assertEqual(res_named.stdout.strip(), "")
            self.run_locutus(["close", "explicit_probe_bot"])

            # 6. Positive control: Providing identity via LOCUTUS_AGENT_NAME succeeds
            env_with_name = {**clean_env, "LOCUTUS_AGENT_NAME": "env_probe_bot"}
            res_env = self.run_locutus(["listen", "1"], cwd=clean_dir, env_overrides=env_with_name)
            self.assertEqual(res_env.returncode, 0)
            self.assertEqual(res_env.stdout.strip(), "")
            self.run_locutus(["close", "env_probe_bot"])

        finally:
            shutil.rmtree(clean_dir, ignore_errors=True)

    def test_35_version_flags(self):
        """Test locutus --version, -v, and version subcommand output strict SemVer matching project files."""
        # Read canonical version from pyproject.toml
        pyproject_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "pyproject.toml")
        with open(pyproject_path, "r", encoding="utf-8") as f:
            pyproject_content = f.read()
        m_pyproject = re.search(r'version\s*=\s*"([^"]+)"', pyproject_content)
        self.assertIsNotNone(m_pyproject, "Could not locate version in pyproject.toml")
        canonical_version = m_pyproject.group(1)

        # SemVer strict regex (major.minor.patch with optional pre-release / build metadata)
        semver_pattern = r"^locutus\s+(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$"

        # 1. Test all positive version invocations
        for flag in [["--version"], ["-v"], ["version"]]:
            res = self.run_locutus(flag)
            self.assertEqual(res.returncode, 0, f"Failed on flag: {flag}")
            self.assertEqual(res.stderr, "", f"Stderr must be empty on version flag: {flag}")
            out = res.stdout.strip()

            # Strict SemVer regex match
            match = re.match(semver_pattern, out)
            self.assertIsNotNone(match, f"Output '{out}' for {flag} did not match SemVer pattern {semver_pattern}")
            major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
            self.assertGreaterEqual(major, 0)
            self.assertGreaterEqual(minor, 0)
            self.assertGreaterEqual(patch, 0)

            # Assert exact match against pyproject.toml canonical version
            self.assertEqual(out, f"locutus {canonical_version}")

        # 2. Negative controls: invalid version variations
        res_bad_flag = self.run_locutus(["--version-extra"])
        self.assertEqual(res_bad_flag.returncode, 1)
        self.assertIn("Unknown subcommand: --version-extra", res_bad_flag.stdout + res_bad_flag.stderr)

        res_bad_case = self.run_locutus(["-V"])
        self.assertEqual(res_bad_case.returncode, 1)
        self.assertIn("Unknown subcommand: -v", res_bad_case.stdout + res_bad_case.stderr)

    def test_36_listen_default_blocks_silently(self):
        """Test that 'locutus listen <agent>' with no timeout blocks silently and receives messages with 0 byte timeout guarantee."""
        agent = "silent_listener_bot"
        self.run_locutus(["close", agent])
        self.run_locutus(["open", agent, "testing"])

        received = []
        errors = []

        def listener_worker():
            try:
                # No timeout specified -> blocks indefinitely until message arrives
                res = self.run_locutus(["listen", agent])
                if res.returncode == 0 and res.stdout.strip():
                    data = json.loads(res.stdout)
                    LocutusPlugin.validate_wire_envelope(data)
                    msg = LocutusMessage.model_validate(data)
                    received.append(msg)
                else:
                    errors.append(f"Unexpected listener exit: rc={res.returncode}, out='{res.stdout}', err='{res.stderr}'")
            except Exception as e:
                errors.append(str(e))

        t = threading.Thread(target=listener_worker)
        t.start()

        try:
            # Give it a moment to enter the blocking Redis wait
            time.sleep(0.5)

            # Still alive and waiting
            self.assertTrue(t.is_alive())
            self.assertEqual(len(received), 0)

            # Send a message to wake it up
            res_send = self.run_locutus([
                "send",
                "--from", "sender_bot",
                "--to", agent,
                "--subject", "Zero Token Wakeup",
                "--body", "Payload delivered cleanly"
            ])
            self.assertEqual(res_send.returncode, 0)

            t.join(timeout=5)
            self.assertFalse(t.is_alive(), "Listener should have exited upon receiving message")
            self.assertEqual(len(errors), 0, f"Listener encountered errors: {errors}")
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].subject, "Zero Token Wakeup")
            self.assertEqual(received[0].body, "Payload delivered cleanly")
            self.assertEqual(received[0].to_agent, agent)
            self.assertEqual(received[0].from_agent, "sender_bot")

            # Test that explicit timeout on empty inbox returns strictly 0 bytes (zero-token exit)
            res_empty = self.run_locutus(["listen", agent, "1"])
            self.assertEqual(res_empty.returncode, 0)
            self.assertEqual(
                len(res_empty.stdout), 0,
                f"Expected strictly 0 bytes stdout on timeout, got {len(res_empty.stdout)}: {res_empty.stdout!r}"
            )
            self.assertEqual(
                len(res_empty.stderr), 0,
                f"Expected strictly 0 bytes stderr on timeout, got {len(res_empty.stderr)}: {res_empty.stderr!r}"
            )

            # Negative control: Message sent to another agent does not wake or leak into this agent
            self.run_locutus([
                "send", "--from", "sender_bot", "--to", "other_silent_bot",
                "--subject", "Other Msg", "--body", "Do Not Deliver Here"
            ])
            res_iso = self.run_locutus(["listen", agent, "1"])
            self.assertEqual(res_iso.returncode, 0)
            self.assertEqual(
                len(res_iso.stdout), 0,
                "Listener must not receive message destined for other_silent_bot"
            )
            self.run_locutus(["close", "other_silent_bot"])

        finally:
            self.run_locutus(["close", agent])

    def test_37_send_with_listen_piggyback(self):
        """Test 'locutus send ... --listen' sends to recipient and immediately blocks on sender's inbox in same PID."""
        alice = "alice_piggyback"
        bob = "bob_piggyback"
        self.run_locutus(["close", alice])
        self.run_locutus(["close", bob])
        self.run_locutus(["open", alice, "dev"])
        self.run_locutus(["open", bob, "dev"])

        try:
            # 1. Start Alice process with send --listen
            alice_proc = subprocess.Popen(
                [
                    BIN_PATH, "send",
                    "--to", bob,
                    "--from", alice,
                    "--subject", "Task for Bob",
                    "--body", "Compute hash",
                    "--listen"
                ],
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            # Wait for Alice to send and register as active listener in Redis
            alice_lock_key = f"{TEST_PREFIX}listener:{alice}"
            registered = False
            lock_info = None
            for _ in range(30):
                time.sleep(0.1)
                chk = subprocess.run(
                    ["redis-cli", "-u", REDIS_URL, "GET", alice_lock_key],
                    capture_output=True, text=True, check=True
                )
                raw_lock = chk.stdout.strip()
                if raw_lock and raw_lock != "(nil)":
                    try:
                        lock_info = json.loads(raw_lock)
                        registered = True
                        break
                    except json.JSONDecodeError:
                        pass
            self.assertTrue(registered, "Alice failed to register listener lock in Redis")
            # Prove same PID: Redis listener lock PID MUST exactly match alice_proc.pid!
            self.assertEqual(
                lock_info.get("pid"), alice_proc.pid,
                f"Listener PID in Redis ({lock_info.get('pid')}) does not match process PID ({alice_proc.pid})"
            )

            # 2. Bob drains his inbox and verifies message
            bob_drain = self.run_locutus(["drain", "1", bob])
            self.assertEqual(bob_drain.returncode, 0)
            bob_data = json.loads(bob_drain.stdout.strip())
            LocutusPlugin.validate_wire_envelope(bob_data)
            bob_msg = LocutusMessage.model_validate(bob_data)
            self.assertEqual(bob_msg.from_agent, alice)
            self.assertEqual(bob_msg.to_agent, bob)
            self.assertEqual(bob_msg.subject, "Task for Bob")
            self.assertEqual(bob_msg.body, "Compute hash")

            # 3. Bob sends reply to Alice
            bob_reply = self.run_locutus([
                "send",
                "--to", alice,
                "--from", bob,
                "--type", "reply",
                "--subject", "Re: Task for Bob",
                "--body", "hash_result_abcdef"
            ])
            self.assertEqual(bob_reply.returncode, 0)

            # Alice process must complete upon receiving reply
            stdout_a, stderr_a = alice_proc.communicate(timeout=5)
            self.assertEqual(alice_proc.returncode, 0)

            # Validate Alice stdout is clean wire envelope with reply payload
            data_a = json.loads(stdout_a.strip())
            LocutusPlugin.validate_wire_envelope(data_a)
            msg_a = LocutusMessage.model_validate(data_a)
            self.assertEqual(msg_a.from_agent, bob)
            self.assertEqual(msg_a.to_agent, alice)
            self.assertEqual(msg_a.subject, "Re: Task for Bob")
            self.assertEqual(msg_a.body, "hash_result_abcdef")
            self.assertEqual(msg_a.type, "reply")

            self.assertIn("[LOCUTUS BUS] Message sent to bob_piggyback", stderr_a)

            # Assert listener lock was cleanly deleted from Redis upon exit
            chk_lock_after = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "EXISTS", alice_lock_key],
                capture_output=True, text=True, check=True
            )
            self.assertEqual(chk_lock_after.stdout.strip(), "0", "Alice listener lock was not cleaned up on exit")

            # 4. Negative control: send --listen with timeout when no reply arrives exits 0 with 0 bytes stdout
            res_to = self.run_locutus([
                "--timeout", "1", "send",
                "--to", bob,
                "--from", alice,
                "--subject", "Timeout Test",
                "--body", "No Reply Coming",
                "--listen"
            ])
            self.assertEqual(res_to.returncode, 0)
            self.assertEqual(
                len(res_to.stdout), 0,
                f"Expected 0 bytes on timeout, got: {res_to.stdout!r}"
            )
            # Drain bob's inbox so it doesn't linger
            self.run_locutus(["drain", "1", bob])

        finally:
            if 'alice_proc' in locals() and alice_proc.poll() is None:
                alice_proc.kill()
                alice_proc.wait()
            self.run_locutus(["close", alice])
            self.run_locutus(["close", bob])

    def test_38_reply_command_with_listen(self):
        """Test 'locutus reply ... --listen' sends type=reply with reply_to and blocks cleanly in same PID."""
        carol = "carol_worker"
        dave = "dave_worker"
        self.run_locutus(["close", carol])
        self.run_locutus(["close", dave])
        self.run_locutus(["open", carol, "team"])
        self.run_locutus(["open", dave, "team"])

        try:
            # 1. Negative controls: missing required arguments on reply command
            res_no_to = self.run_locutus(["reply", "--subject", "Subj", "--body", "Body"])
            self.assertEqual(res_no_to.returncode, 1)
            self.assertIn("Error: Missing required argument '--to <recipient>'", res_no_to.stderr)

            res_no_body = self.run_locutus(["reply", "--to", carol, "--subject", "Subj"])
            self.assertEqual(res_no_body.returncode, 1)
            self.assertIn("Error: Missing required arguments. --subject and --body are required.", res_no_body.stderr)

            # 2. Dave replies to Carol with --reply-to and --listen via subprocess
            dave_proc = subprocess.Popen(
                [
                    BIN_PATH, "reply",
                    "--to", carol,
                    "--from", dave,
                    "--reply-to", "req_msg_999",
                    "--subject", "Task Complete",
                    "--body", "Built successfully",
                    "--listen"
                ],
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            # Wait for Dave to register listener lock in Redis
            dave_lock_key = f"{TEST_PREFIX}listener:{dave}"
            registered = False
            lock_info = None
            for _ in range(30):
                time.sleep(0.1)
                chk = subprocess.run(
                    ["redis-cli", "-u", REDIS_URL, "GET", dave_lock_key],
                    capture_output=True, text=True, check=True
                )
                raw_lock = chk.stdout.strip()
                if raw_lock and raw_lock != "(nil)":
                    try:
                        lock_info = json.loads(raw_lock)
                        registered = True
                        break
                    except json.JSONDecodeError:
                        pass
            self.assertTrue(registered, "Dave failed to register listener lock in Redis")
            self.assertEqual(
                lock_info.get("pid"), dave_proc.pid,
                f"Listener PID in Redis ({lock_info.get('pid')}) does not match Dave process PID ({dave_proc.pid})"
            )

            # 3. Carol drains her inbox to verify reply structure
            carol_drain = self.run_locutus(["drain", "1", carol])
            self.assertEqual(carol_drain.returncode, 0)
            rep_data = json.loads(carol_drain.stdout.strip())
            LocutusPlugin.validate_wire_envelope(rep_data)
            rep_msg = LocutusMessage.model_validate(rep_data)
            self.assertEqual(rep_msg.type, "reply")
            self.assertEqual(rep_msg.reply_to, "req_msg_999")
            self.assertEqual(rep_msg.from_agent, dave)
            self.assertEqual(rep_msg.to_agent, carol)
            self.assertEqual(rep_msg.subject, "Task Complete")
            self.assertEqual(rep_msg.body, "Built successfully")

            # 4. Carol sends next instruction to Dave
            carol_next = self.run_locutus([
                "send",
                "--to", dave,
                "--from", carol,
                "--subject", "Next Task",
                "--body", "Run deploy"
            ])
            self.assertEqual(carol_next.returncode, 0)

            # Dave unblocks upon receiving Carol's next message
            stdout_d, stderr_d = dave_proc.communicate(timeout=5)
            self.assertEqual(dave_proc.returncode, 0)

            # Dave's stdout must be clean JSON wire envelope
            next_data = json.loads(stdout_d.strip())
            LocutusPlugin.validate_wire_envelope(next_data)
            next_msg = LocutusMessage.model_validate(next_data)
            self.assertEqual(next_msg.from_agent, carol)
            self.assertEqual(next_msg.to_agent, dave)
            self.assertEqual(next_msg.subject, "Next Task")
            self.assertEqual(next_msg.body, "Run deploy")

            # Verify listener lock deleted from Redis
            chk_dave_lock = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "EXISTS", dave_lock_key],
                capture_output=True, text=True, check=True
            )
            self.assertEqual(chk_dave_lock.stdout.strip(), "0")

            # 5. Verify timeout mode on reply with --listen-timeout
            timeout_res = self.run_locutus([
                "reply",
                "--to", carol,
                "--from", dave,
                "--subject", "Quick reply",
                "--body", "ack",
                "--listen-timeout", "1"
            ])
            self.assertEqual(timeout_res.returncode, 0)
            self.assertEqual(
                len(timeout_res.stdout), 0,
                f"Expected strictly 0 bytes stdout on timeout, got: {timeout_res.stdout!r}"
            )
            # Drain carol's inbox
            self.run_locutus(["drain", "1", carol])

        finally:
            if 'dave_proc' in locals() and dave_proc.poll() is None:
                dave_proc.kill()
                dave_proc.wait()
            self.run_locutus(["close", carol])
            self.run_locutus(["close", dave])

    def test_39_prevent_stacked_listeners_piggyback(self):
        """Verify that 'send --listen' detects an existing active listener, logs warning with exact PID, and skips without exit 1."""
        alice = "alice_stack_guard"
        bob = "bob_stack_guard"
        self.run_locutus(["close", alice])
        self.run_locutus(["close", bob])
        self.run_locutus(["open", alice, "dev"])
        self.run_locutus(["open", bob, "dev"])

        bob_lock_key = f"{TEST_PREFIX}listener:{bob}"

        # 1. Start original listener process for Bob
        orig_p = subprocess.Popen(
            [BIN_PATH, "listen", bob, "10"],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        try:
            # Wait for original listener to register its PID in Redis
            registered = False
            lock_info = None
            for _ in range(30):
                time.sleep(0.1)
                chk = subprocess.run(
                    ["redis-cli", "-u", REDIS_URL, "GET", bob_lock_key],
                    capture_output=True, text=True, check=True
                )
                raw = chk.stdout.strip()
                if raw and raw != "(nil)":
                    try:
                        lock_info = json.loads(raw)
                        registered = True
                        break
                    except json.JSONDecodeError:
                        pass
            self.assertTrue(registered, "Original listener failed to register in Redis")
            self.assertEqual(lock_info.get("pid"), orig_p.pid)

            # 2. Second process: Bob calls send with --listen
            # Must detect existing active listener, log warning with orig_p.pid, and exit 0 immediately
            send_res = self.run_locutus([
                "send",
                "--to", alice,
                "--from", bob,
                "--subject", "Intermediate update",
                "--body", "Processing chunk 1",
                "--listen"
            ])
            self.assertEqual(send_res.returncode, 0, f"Second send --listen failed: {send_res.stderr}")
            self.assertIn(f"Active listener already running for {bob} (PID {orig_p.pid}", send_res.stderr)
            self.assertIn("skipping duplicate listener", send_res.stderr)

            # Assert Redis lock key was completely preserved and still owned by orig_p.pid
            chk_lock_still = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "GET", bob_lock_key],
                capture_output=True, text=True, check=True
            )
            lock_after = json.loads(chk_lock_still.stdout.strip())
            self.assertEqual(lock_after.get("pid"), orig_p.pid, "Original lock PID was overwritten!")

            # 3. Alice drains message and validates payload
            alice_drain = self.run_locutus(["drain", "1", alice])
            self.assertEqual(alice_drain.returncode, 0)
            a_data = json.loads(alice_drain.stdout.strip())
            LocutusPlugin.validate_wire_envelope(a_data)
            a_msg = LocutusMessage.model_validate(a_data)
            self.assertEqual(a_msg.from_agent, bob)
            self.assertEqual(a_msg.to_agent, alice)
            self.assertEqual(a_msg.subject, "Intermediate update")
            self.assertEqual(a_msg.body, "Processing chunk 1")

            # 4. Alice replies to Bob to unblock the original listener cleanly
            self.run_locutus([
                "send",
                "--to", bob,
                "--from", alice,
                "--subject", "Ack update",
                "--body", "Proceed to chunk 2"
            ])

            orig_stdout, orig_stderr = orig_p.communicate(timeout=5)
            self.assertEqual(orig_p.returncode, 0)
            bob_data = json.loads(orig_stdout.strip())
            LocutusPlugin.validate_wire_envelope(bob_data)
            bob_msg = LocutusMessage.model_validate(bob_data)
            self.assertEqual(bob_msg.from_agent, alice)
            self.assertEqual(bob_msg.to_agent, bob)
            self.assertEqual(bob_msg.subject, "Ack update")
            self.assertEqual(bob_msg.body, "Proceed to chunk 2")

            # Verify lock cleanup after exit
            chk_del = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "EXISTS", bob_lock_key],
                capture_output=True, text=True, check=True
            )
            self.assertEqual(chk_del.stdout.strip(), "0")

        finally:
            if orig_p.poll() is None:
                orig_p.kill()
                orig_p.wait()
            self.run_locutus(["close", alice])
            self.run_locutus(["close", bob])

    def test_40_standalone_listen_rejects_duplicate(self):
        """Verify that running 'locutus listen' when one is already active fails with code 1, preserves lock, unless --force is used."""
        carol = "carol_dup_test"
        self.run_locutus(["close", carol])
        self.run_locutus(["open", carol, "dev"])

        carol_lock_key = f"{TEST_PREFIX}listener:{carol}"

        # 1. Start original listener process with 10s timeout
        orig_p = subprocess.Popen(
            [BIN_PATH, "listen", carol, "10"],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        try:
            # Wait for original listener to register in Redis
            registered = False
            lock_orig = None
            for _ in range(30):
                time.sleep(0.1)
                chk = subprocess.run(
                    ["redis-cli", "-u", REDIS_URL, "GET", carol_lock_key],
                    capture_output=True, text=True, check=True
                )
                raw = chk.stdout.strip()
                if raw and raw != "(nil)":
                    try:
                        lock_orig = json.loads(raw)
                        registered = True
                        break
                    except json.JSONDecodeError:
                        pass
            self.assertTrue(registered, "Original listener failed to register lock in Redis")
            self.assertEqual(lock_orig.get("pid"), orig_p.pid)

            # 2. Attempt duplicate standalone listen without --force
            # Must exit with code 1, empty stdout, and exact error containing original PID
            dup_res = self.run_locutus(["listen", carol, "2"])
            self.assertEqual(dup_res.returncode, 1)
            self.assertEqual(dup_res.stdout, "", "Duplicate listener must produce 0 bytes stdout")
            self.assertIn(
                f"Error: Listener already active for agent '{carol}' (PID {orig_p.pid} on ",
                dup_res.stderr
            )
            self.assertIn("Refusing to start duplicate listener.", dup_res.stderr)

            # 3. Assert original listener lock in Redis is 100% preserved and unmodified
            chk_after = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "GET", carol_lock_key],
                capture_output=True, text=True, check=True
            )
            lock_after = json.loads(chk_after.stdout.strip())
            self.assertEqual(lock_after, lock_orig, "Redis listener lock was mutated by duplicate listen attempt")

            # 4. Standalone listen WITH --force bypasses the guard
            force_res = self.run_locutus(["listen", carol, "1", "--force"])
            self.assertEqual(force_res.returncode, 0)
            self.assertEqual(force_res.stdout.strip(), "")

        finally:
            if orig_p.poll() is None:
                orig_p.terminate()
                try:
                    orig_p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    orig_p.kill()
                    orig_p.wait()
            self.run_locutus(["close", carol])

    def test_41_stale_listener_self_healing(self):
        """Verify that a stale listener lock with a dead local PID is self-healed, while living local PID and remote locks are preserved."""
        dave = "dave_stale_test"
        self.run_locutus(["close", dave])
        self.run_locutus(["open", dave, "dev"])

        import socket
        host = socket.gethostname()
        lock_key = f"{TEST_PREFIX}listener:{dave}"

        try:
            # 1. Stale lock with dead local PID (9999999) -> self-heals, starts listener, exits 0
            stale_record = json.dumps({"pid": 9999999, "host": host, "started": int(time.time())})
            subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "SET", lock_key, stale_record, "EX", "150"],
                capture_output=True, check=True
            )

            res = self.run_locutus(["listen", dave, "1"])
            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.stdout.strip(), "")

            # Verify key was cleaned up upon exit
            chk = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "GET", lock_key],
                capture_output=True, text=True, check=True
            )
            self.assertEqual(chk.stdout.strip(), "")

            # 2. Negative control: Living local PID on same host MUST NOT be overwritten or self-healed
            my_pid = os.getpid()
            live_record = json.dumps({"pid": my_pid, "host": host, "started": int(time.time())})
            subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "SET", lock_key, live_record, "EX", "150"],
                capture_output=True, check=True
            )

            res_live = self.run_locutus(["listen", dave, "1"])
            self.assertEqual(res_live.returncode, 1, "Listener must NOT overwrite a living local PID")
            self.assertIn(f"Listener already active for agent '{dave}' (PID {my_pid} on {host})", res_live.stderr)
            self.assertIn("Refusing to start duplicate listener.", res_live.stderr)

            # Assert Redis lock was untouched
            chk_live = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "GET", lock_key],
                capture_output=True, text=True, check=True
            )
            self.assertEqual(json.loads(chk_live.stdout.strip())["pid"], my_pid)

            # 3. Negative control: Remote host lock MUST NOT be cleared even if PID would be dead locally
            remote_host = "remote-worker-node-99.cluster.local"
            remote_record = json.dumps({"pid": 9999999, "host": remote_host, "started": int(time.time())})
            subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "SET", lock_key, remote_record, "EX", "150"],
                capture_output=True, check=True
            )

            res_remote = self.run_locutus(["listen", dave, "1"])
            self.assertEqual(res_remote.returncode, 1, "Listener must NOT clear foreign host listener lock")
            self.assertIn(f"Listener already active for agent '{dave}' (PID 9999999 on {remote_host})", res_remote.stderr)

            # Assert Redis lock was untouched
            chk_remote = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "GET", lock_key],
                capture_output=True, text=True, check=True
            )
            self.assertEqual(json.loads(chk_remote.stdout.strip())["host"], remote_host)

        finally:
            self.run_locutus(["close", dave])
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", lock_key], capture_output=True)

    def test_42_listener_exit_does_not_delete_foreign_lock(self):
        """Verify that an exiting listener does not delete a foreign/preempted lock, but cleans its own lock normally."""
        agent = "preempt_listener_test"
        self.run_locutus(["close", agent])
        self.run_locutus(["open", agent, "dev"])

        import socket
        host = socket.gethostname()
        lock_key = f"{TEST_PREFIX}listener:{agent}"

        # 1. Start listener in background with 2s timeout
        orig_p = subprocess.Popen(
            [BIN_PATH, "listen", agent, "2"],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        try:
            # Wait for original listener to register in Redis
            registered = False
            for _ in range(30):
                time.sleep(0.1)
                chk = subprocess.run(
                    ["redis-cli", "-u", REDIS_URL, "GET", lock_key],
                    capture_output=True, text=True, check=True
                )
                raw = chk.stdout.strip()
                if raw and raw != "(nil)":
                    try:
                        node = json.loads(raw)
                        if node.get("pid") == orig_p.pid:
                            registered = True
                            break
                    except json.JSONDecodeError:
                        pass
            self.assertTrue(registered, f"Original listener {orig_p.pid} failed to claim lock in Redis")

            # 2. Simulate preemption: overwrite lock with another PID on same host
            foreign_lock_payload = {
                "pid": 88888,
                "host": host,
                "started": int(time.time()),
                "owner": "preempting_worker_99"
            }
            subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "SET", lock_key, json.dumps(foreign_lock_payload), "EX", "150"],
                capture_output=True, check=True
            )

            # 3. Wait for original listener to exit after its 2s timeout
            stdout, stderr = orig_p.communicate(timeout=5)
            self.assertEqual(orig_p.returncode, 0)
            self.assertEqual(stdout, "", "Listener must exit silently with 0 bytes stdout on timeout")

            # 4. Crucial: The replacement lock with PID 88888 MUST still be 100% intact in Redis!
            chk_after = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "GET", lock_key],
                capture_output=True, text=True, check=True
            )
            data_after = json.loads(chk_after.stdout.strip())
            self.assertEqual(data_after["pid"], 88888, "Exiting listener erroneously modified foreign PID")
            self.assertEqual(data_after["owner"], "preempting_worker_99", "Exiting listener corrupted foreign lock metadata")

            # 5. Contrast negative control: Normal exit cleans up its own lock
            # Clear foreign lock
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", lock_key], capture_output=True, check=True)

            # Start listener with 1s timeout without foreign preemption
            normal_res = self.run_locutus(["listen", agent, "1"])
            self.assertEqual(normal_res.returncode, 0)
            self.assertEqual(normal_res.stdout.strip(), "")

            # Verify that normal exit DID clean up its own lock
            chk_normal = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "EXISTS", lock_key],
                capture_output=True, text=True, check=True
            )
            self.assertEqual(chk_normal.stdout.strip(), "0", "Normal listener exit failed to clean up its own lock")

        finally:
            if orig_p.poll() is None:
                orig_p.kill()
                orig_p.wait()
            self.run_locutus(["close", agent])
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", lock_key], capture_output=True)

    def test_43_worker_heartbeat_renewal(self):
        """Verify that 'locutus work' registers and refreshes worker heartbeat, and prunes after process termination."""
        worker = "queue_worker_bot"
        queue = "long_wait_queue"
        self.run_locutus(["close", worker])
        self.run_locutus(["open", worker, "workers"])

        hb_key = f"{TEST_PREFIX}heartbeat:{worker}"

        try:
            with tempfile.TemporaryDirectory() as hb_tmp:
                hb_cfg_path = os.path.join(hb_tmp, "heartbeat.toml")
                with open(hb_cfg_path, "w", encoding="utf-8") as f:
                    f.write(f'redis_url = "{REDIS_URL}"\nprefix = "{TEST_PREFIX}"\nheartbeat_ttl = 4\n')

                # 1. Start worker process with short heartbeat TTL (4s) so renewal chunk is 2s
                env_worker = {**self.env, "LOCUTUS_AGENT_NAME": worker}
                p = subprocess.Popen(
                    [BIN_PATH, "--config", hb_cfg_path, "work", queue, "10"],
                    env=env_worker,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )

                try:
                    # Wait for worker to register initial heartbeat
                    registered = False
                    for _ in range(30):
                        time.sleep(0.1)
                        chk_start = subprocess.run(
                            ["redis-cli", "-u", REDIS_URL, "EXISTS", hb_key],
                            capture_output=True, text=True, check=True
                        )
                        if chk_start.stdout.strip() == "1":
                            registered = True
                            break
                    self.assertTrue(registered, f"Worker {worker} failed to register heartbeat in Redis")

                    # Verify worker is ACTIVE in who directory with valid directory schema
                    res_who = self.run_locutus(["who", "-a", "--json"])
                    self.assertEqual(res_who.returncode, 0)
                    agents = LocutusPlugin.validate_json_schema("directory", res_who.stdout)
                    match = [a for a in agents if a["agent"] == worker]
                    self.assertEqual(len(match), 1, f"Expected {worker} to be active in who output while working")
                    self.assertEqual(match[0]["status"], "ACTIVE")

                    # Initial TTL check (2..4s)
                    ttl1 = int(subprocess.run(
                        ["redis-cli", "-u", REDIS_URL, "TTL", hb_key],
                        capture_output=True, text=True, check=True
                    ).stdout.strip())
                    self.assertGreaterEqual(ttl1, 2)
                    self.assertLessEqual(ttl1, 4)

                    # Wait 2.6s (past 2.0s renewal chunk boundary)
                    time.sleep(2.6)
                    ttl2 = int(subprocess.run(
                        ["redis-cli", "-u", REDIS_URL, "TTL", hb_key],
                        capture_output=True, text=True, check=True
                    ).stdout.strip())
                    self.assertGreaterEqual(
                        ttl2, 2,
                        f"Worker heartbeat renewal failed: expected TTL >= 2 after 2.6s, but got {ttl2}"
                    )

                    # 2. Kill worker process abruptly to test heartbeat expiration without exit cleanup
                    p.kill()
                    p.wait()

                    # Wait for heartbeat TTL to expire (ttl was ~3s, wait 3.5s)
                    time.sleep(3.5)

                    # Assert heartbeat key in Redis has vanished
                    chk_dead_hb = subprocess.run(
                        ["redis-cli", "-u", REDIS_URL, "EXISTS", hb_key],
                        capture_output=True, text=True, check=True
                    )
                    self.assertEqual(
                        chk_dead_hb.stdout.strip(), "0",
                        f"Heartbeat key {hb_key} should have expired in Redis after killing worker"
                    )

                    # Run sweep to clean up dead agent
                    self.run_locutus(["sweep"])

                    # Verify worker is no longer ACTIVE in who directory
                    res_who_dead = self.run_locutus(["who", "--json"])
                    self.assertEqual(res_who_dead.returncode, 0)
                    active_now = LocutusPlugin.validate_json_schema("directory", res_who_dead.stdout)
                    match_dead = [a for a in active_now if a["agent"] == worker]
                    self.assertEqual(len(match_dead), 0, f"Dead worker {worker} should not be in active who directory")

                finally:
                    if p.poll() is None:
                        p.kill()
                        p.wait()

            # 3. Positive worker task execution: consume task and validate envelope
            self.run_locutus(["open", worker, "workers"])
            self.run_locutus(["enqueue", queue, "--subject", "Job 1", "--body", "Payload 1"])

            res_work = self.run_locutus(["work", queue, "2"], env_overrides={"LOCUTUS_AGENT_NAME": worker})
            self.assertEqual(res_work.returncode, 0)
            data_work = json.loads(res_work.stdout.strip())
            LocutusPlugin.validate_wire_envelope(data_work)
            msg_work = LocutusMessage.model_validate(data_work)
            self.assertEqual(msg_work.subject, "Job 1")
            self.assertEqual(msg_work.body, "Payload 1")

        finally:
            self.run_locutus(["close", worker])
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", f"{TEST_PREFIX}queue:{queue}:items"], capture_output=True)
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", hb_key], capture_output=True)

    def test_44_scatter_gather_quorum(self):
        """Test 'locutus scatter' with quorum aggregation, wire envelope validation, and ephemeral queue cleanup."""
        w1 = "scatter_w1"
        w2 = "scatter_w2"
        self.run_locutus(["close", w1])
        self.run_locutus(["close", w2])
        self.run_locutus(["open", w1, "scatter_team"])
        self.run_locutus(["open", w2, "scatter_team"])

        captured_reply_queue = []

        try:
            # 1. Negative control: missing required arguments
            res_bad = self.run_locutus(["scatter", "--subject", "No Targets", "--body", "Missing"])
            self.assertEqual(res_bad.returncode, 1)
            self.assertIn("Error: Missing required arguments for scatter.", res_bad.stderr)

            # 2. Negative control: scatter to empty tag returns empty array without hang
            res_empty_scatter = self.run_locutus([
                "scatter",
                "--targets", "@nonexistent_scatter_tag_999",
                "--subject", "Ghost",
                "--body", "Ghost Body",
                "--quorum", "1",
                "--timeout", "1"
            ])
            self.assertEqual(res_empty_scatter.returncode, 0)
            self.assertEqual(json.loads(res_empty_scatter.stdout.strip()), [])

            # 3. Quorum scatter with distinct payload replies
            def worker_loop(w_name, vote_val):
                res = self.run_locutus(["listen", w_name, "5"])
                if res.returncode == 0 and res.stdout.strip():
                    data = json.loads(res.stdout.strip())
                    reply_to = data.get("reply_to")
                    if reply_to:
                        captured_reply_queue.append(reply_to)
                        self.run_locutus([
                            "--agent-name=" + w_name,
                            "reply",
                            "--to", data.get("from", "scatter_lead"),
                            "--reply-to", reply_to,
                            "--subject", "Vote Response",
                            "--body", f"vote:{vote_val}"
                        ])

            t1 = threading.Thread(target=worker_loop, args=(w1, "approve"))
            t2 = threading.Thread(target=worker_loop, args=(w2, "reject"))
            t1.start()
            t2.start()
            time.sleep(0.3)

            # Scatter by tag with quorum=2
            res_scatter = self.run_locutus([
                "--agent-name=scatter_lead",
                "scatter",
                "--targets", "@scatter_team",
                "--subject", "Release Vote",
                "--body", "Vote for v1.0",
                "--quorum", "2",
                "--timeout", "5"
            ])
            t1.join(timeout=5)
            t2.join(timeout=5)

            self.assertEqual(res_scatter.returncode, 0, f"Scatter failed: {res_scatter.stderr}")
            replies = json.loads(res_scatter.stdout.strip())
            self.assertEqual(len(replies), 2)

            # Wire envelope and Pydantic validation for each reply
            by_sender = {}
            for r in replies:
                LocutusPlugin.validate_wire_envelope(r)
                msg = LocutusMessage.model_validate(r)
                self.assertEqual(msg.type, "reply")
                self.assertEqual(msg.subject, "Vote Response")
                self.assertEqual(msg.to_agent, "scatter_lead")
                by_sender[msg.from_agent] = msg.body

            self.assertEqual(by_sender[w1], "vote:approve")
            self.assertEqual(by_sender[w2], "vote:reject")

            # 4. Ephemeral reply queue cleanup assertion: Redis inbox must be deleted after scatter
            if captured_reply_queue:
                rq_key = f"{TEST_PREFIX}inbox:{captured_reply_queue[0]}"
                chk_rq = subprocess.run(
                    ["redis-cli", "-u", REDIS_URL, "EXISTS", rq_key],
                    capture_output=True, text=True, check=True
                )
                self.assertEqual(
                    chk_rq.stdout.strip(), "0",
                    f"Ephemeral reply queue {rq_key} was not deleted from Redis after scatter completed"
                )

        finally:
            self.run_locutus(["close", w1])
            self.run_locutus(["close", w2])

    def test_45_scatter_explicit_targets_and_raw(self):
        """Test 'locutus scatter' with explicit named targets and --raw mode, asserting target isolation."""
        w1 = "scatter_raw_w1"
        w2 = "scatter_raw_w2"
        w_innocent = "scatter_raw_innocent"
        self.run_locutus(["close", w1])
        self.run_locutus(["close", w2])
        self.run_locutus(["close", w_innocent])
        self.run_locutus(["open", w1, "calc"])
        self.run_locutus(["open", w2, "calc"])
        self.run_locutus(["open", w_innocent, "calc"])

        try:
            def worker_loop(w_name, res_val):
                res = self.run_locutus(["listen", w_name, "5"])
                if res.returncode == 0 and res.stdout.strip():
                    data = json.loads(res.stdout.strip())
                    LocutusPlugin.validate_wire_envelope(data)
                    msg = LocutusMessage.model_validate(data)
                    self.assertEqual(msg.subject, "Compute")
                    self.assertEqual(msg.body, "Run calculation")
                    reply_to = data.get("reply_to")
                    if reply_to:
                        self.run_locutus([
                            "--agent-name=" + w_name,
                            "reply",
                            "--to", data.get("from", "calc_lead"),
                            "--reply-to", reply_to,
                            "--subject", "Result",
                            "--body", res_val
                        ])

            t1 = threading.Thread(target=worker_loop, args=(w1, "42"))
            t2 = threading.Thread(target=worker_loop, args=(w2, "100"))
            t1.start()
            t2.start()
            time.sleep(0.3)

            # Scatter with explicit comma-separated target list and --raw mode
            res_scatter = self.run_locutus([
                "--agent-name=calc_lead",
                "scatter",
                "--targets", f"{w1},{w2}",
                "--subject", "Compute",
                "--body", "Run calculation",
                "--quorum", "2",
                "--timeout", "5",
                "--raw"
            ])
            t1.join(timeout=5)
            t2.join(timeout=5)

            self.assertEqual(res_scatter.returncode, 0, f"Scatter raw failed: {res_scatter.stderr}")
            lines = set(res_scatter.stdout.strip().splitlines())
            self.assertEqual(lines, {"42", "100"})

            # Negative control: Target isolation verification
            # w_innocent was NOT in the targets list, so its inbox must be strictly 0
            chk_innocent = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "LLEN", f"{TEST_PREFIX}inbox:{w_innocent}"],
                capture_output=True, text=True, check=True
            )
            self.assertEqual(
                chk_innocent.stdout.strip(), "0",
                f"Non-targeted agent {w_innocent} received scatter task in inbox!"
            )
            # Listening on innocent worker returns 0 bytes
            res_iso = self.run_locutus(["listen", w_innocent, "1"])
            self.assertEqual(res_iso.returncode, 0)
            self.assertEqual(len(res_iso.stdout), 0, "Non-targeted agent received leaked message")

        finally:
            self.run_locutus(["close", w1])
            self.run_locutus(["close", w2])
            self.run_locutus(["close", w_innocent])

    def test_45b_scatter_quorum_clamping(self):
        """Test that scatter clamps quorum to delivered count when quorum > delivered."""
        w1 = f"clamp_w1_{int(time.time() * 1000)}"
        self.run_locutus(["open", w1, "clamp_test"])

        def worker_loop():
            res = self.run_locutus(["listen", w1, "5"])
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout.strip())
                reply_to = data.get("reply_to")
                if reply_to:
                    self.run_locutus([
                        "--agent-name=" + w1,
                        "reply",
                        "--to", data.get("from", "lead"),
                        "--reply-to", reply_to,
                        "--subject", "Done",
                        "--body", "finished"
                    ])

        t = threading.Thread(target=worker_loop)
        t.start()
        time.sleep(0.3)

        # Scatter to single target w1, but request --quorum 10 and --timeout 6
        t_start = time.time()
        res_scatter = self.run_locutus([
            "--agent-name=lead",
            "scatter",
            "--targets", w1,
            "--subject", "Task",
            "--body", "Go",
            "--quorum", "10",
            "--timeout", "6",
            "--raw"
        ])
        elapsed = time.time() - t_start
        t.join(timeout=5)
        self.run_locutus(["close", w1])

        self.assertEqual(res_scatter.returncode, 0)
        self.assertEqual(res_scatter.stdout.strip(), "finished")
        # Quorum must have been clamped from 10 down to 1 (delivered), so it exits immediately (< 3s)
        self.assertLess(elapsed, 3.0, "Scatter did not clamp quorum and hung waiting for phantom targets")

    def test_46_reliable_queue_claim_ack_and_dlq(self):
        """Test 'locutus claim' with lease, 'locutus ack', and DLQ auto-reclaim after 3 retries."""
        q = f"reliable_q_{int(time.time() * 1000)}"
        agent = "claim_bot"
        self.run_locutus(["open", agent, "workers"])

        queue_key = f"{TEST_PREFIX}queue:{{{q}}}"
        leases_key = f"{TEST_PREFIX}leases:{{{q}}}"
        attempts_key = f"{TEST_PREFIX}attempts:{{{q}}}"
        dlq_key = f"{TEST_PREFIX}queue:dlq:{{{q}}}"

        # 0. Missing argument negative controls
        res_no_q = self.run_locutus(["claim"])
        self.assertEqual(res_no_q.returncode, 1)
        self.assertIn("Error: Missing queue name.", res_no_q.stderr)

        res_no_ack = self.run_locutus(["ack"])
        self.assertEqual(res_no_ack.returncode, 1)
        self.assertIn("Error: Missing queue name or task ID.", res_no_ack.stderr)

        try:
            # 1. Enqueue and Claim successfully with ACK
            self.run_locutus(["enqueue", q, "--subject", "Task 1", "--body", "data:123"])
            res_claim = self.run_locutus(["claim", q, "--lease", "5"], env_overrides={"LOCUTUS_AGENT_NAME": agent})
            self.assertEqual(res_claim.returncode, 0, f"Claim failed: {res_claim.stderr}")
            task1 = json.loads(res_claim.stdout.strip())
            LocutusPlugin.validate_wire_envelope(task1)
            msg1 = LocutusMessage.model_validate(task1)
            self.assertEqual(msg1.subject, "Task 1")
            self.assertEqual(msg1.body, "data:123")
            self.assertEqual(msg1.to_agent, f"queue:{q}")
            task1_id = task1["id"]

            # Direct Redis inspection: check attempt counter is 1 and active lease exists
            att1 = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "HGET", attempts_key, task1_id],
                capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertEqual(att1, "1", f"Expected attempt count 1 for task {task1_id}, got {att1}")

            act1 = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "EXISTS", f"{TEST_PREFIX}active:{{{q}}}:{task1_id}"],
                capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertEqual(act1, "1", f"Active lease key for {task1_id} should exist")

            # Ack task 1
            res_ack = self.run_locutus(["ack", q, task1_id])
            self.assertEqual(res_ack.returncode, 0, f"Ack failed: {res_ack.stderr}")
            self.assertIn(task1_id, res_ack.stdout)

            # Direct Redis inspection after ACK: keys must be cleaned up
            act1_post = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "EXISTS", f"{TEST_PREFIX}active:{{{q}}}:{task1_id}"],
                capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertEqual(act1_post, "0")

            att1_post = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "HEXISTS", attempts_key, task1_id],
                capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertEqual(att1_post, "0")

            # 2. Enqueue a task that will expire and fail 3 times -> moves to DLQ
            self.run_locutus(["enqueue", q, "--subject", "Failing Task", "--body", "fail_data"])

            # Attempt 1: Claim with 1s lease, do not ACK
            res_c1 = self.run_locutus(["claim", q, "--lease", "1"], env_overrides={"LOCUTUS_AGENT_NAME": agent})
            self.assertEqual(res_c1.returncode, 0)
            t_fail1 = json.loads(res_c1.stdout.strip())
            LocutusPlugin.validate_wire_envelope(t_fail1)
            msg_fail1 = LocutusMessage.model_validate(t_fail1)
            self.assertEqual(msg_fail1.subject, "Failing Task")
            self.assertEqual(msg_fail1.body, "fail_data")
            t_fail_id = t_fail1["id"]

            att_fail1 = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "HGET", attempts_key, t_fail_id],
                capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertEqual(att_fail1, "1")
            time.sleep(1.2)  # Wait for lease to expire

            # Attempt 2: Claim again (auto-reclaim from expired lease, attempts increments to 2)
            res_c2 = self.run_locutus(["claim", q, "--lease", "1"], env_overrides={"LOCUTUS_AGENT_NAME": agent})
            self.assertEqual(res_c2.returncode, 0)
            t_fail2 = json.loads(res_c2.stdout.strip())
            LocutusPlugin.validate_wire_envelope(t_fail2)
            msg_fail2 = LocutusMessage.model_validate(t_fail2)
            self.assertEqual(msg_fail2.id, t_fail_id)
            self.assertEqual(msg_fail2.subject, "Failing Task")

            att_fail2 = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "HGET", attempts_key, t_fail_id],
                capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertEqual(att_fail2, "2", f"Expected attempt count 2, got {att_fail2}")
            time.sleep(1.2)  # Wait for lease to expire

            # Attempt 3: Claim again (third attempt, attempts increments to 3)
            res_c3 = self.run_locutus(["claim", q, "--lease", "1"], env_overrides={"LOCUTUS_AGENT_NAME": agent})
            self.assertEqual(res_c3.returncode, 0)
            t_fail3 = json.loads(res_c3.stdout.strip())
            LocutusPlugin.validate_wire_envelope(t_fail3)
            msg_fail3 = LocutusMessage.model_validate(t_fail3)
            self.assertEqual(msg_fail3.id, t_fail_id)

            att_fail3 = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "HGET", attempts_key, t_fail_id],
                capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertEqual(att_fail3, "3", f"Expected attempt count 3, got {att_fail3}")
            time.sleep(1.2)  # Wait for lease to expire

            # Attempt 4: Next claim should move the task to DLQ, clean attempts, and return empty
            res_c4 = self.run_locutus(["claim", q, "1", "--lease", "1"], env_overrides={"LOCUTUS_AGENT_NAME": agent})
            self.assertEqual(res_c4.returncode, 0)
            self.assertEqual(res_c4.stdout.strip(), "")

            # Direct Redis assertions: attempts deleted, main queue empty, DLQ has 1 item
            att_fail4 = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "HEXISTS", attempts_key, t_fail_id],
                capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertEqual(att_fail4, "0", "Attempts hash entry should be removed upon moving to DLQ")

            q_len = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "LLEN", queue_key],
                capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertEqual(q_len, "0")

            dlq_len = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "LLEN", dlq_key],
                capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertEqual(dlq_len, "1", "DLQ should contain exactly 1 failed task")

            # Verify DLQ contains the failed task with full wire envelope and correct task_id
            res_dlq = self.run_locutus(["work", "dlq:" + q, "1"])
            self.assertEqual(res_dlq.returncode, 0)
            dlq_data = json.loads(res_dlq.stdout.strip())
            LocutusPlugin.validate_wire_envelope(dlq_data)
            dlq_msg = LocutusMessage.model_validate(dlq_data)
            self.assertEqual(dlq_msg.id, t_fail_id)
            self.assertEqual(dlq_msg.subject, "Failing Task")
            self.assertEqual(dlq_msg.body, "fail_data")

        finally:
            self.run_locutus(["close", agent])
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", queue_key, leases_key, attempts_key, dlq_key], capture_output=True)

    def test_46b_claim_lease_renewal(self):
        """Test in-flight claim lease extension ('locutus claim renew')."""
        q = f"renew_q_{int(time.time() * 1000)}"
        self.run_locutus(["enqueue", q, "--subject", "Long Task", "--body", "heavy_workload"])

        # Claim with short 2s lease
        res_claim = self.run_locutus(["claim", q, "--lease", "2"])
        self.assertEqual(res_claim.returncode, 0)
        task = json.loads(res_claim.stdout.strip())
        task_id = task["id"]

        # Renew lease for 60s
        res_renew = self.run_locutus(["claim", "renew", q, task_id, "--lease", "60"])
        self.assertEqual(res_renew.returncode, 0)
        renew_data = json.loads(res_renew.stdout.strip())
        self.assertTrue(renew_data.get("renewed"))
        self.assertEqual(renew_data.get("lease_sec"), 60)

        # Sleep past initial 2s lease
        time.sleep(2.5)

        # Another worker attempts to claim; should get nothing because lease was renewed
        res_other = self.run_locutus(["claim", q, "1"])
        self.assertEqual(res_other.returncode, 0)
        self.assertEqual(res_other.stdout.strip(), "")

        # Acknowledge task
        res_ack = self.run_locutus(["ack", q, task_id])
        self.assertEqual(res_ack.returncode, 0)

    def test_46c_poison_pill_tail_requeue(self):
        """Test that expired tasks are re-queued to tail (LPUSH) avoiding head-of-line blocking."""
        q = f"poison_q_{int(time.time() * 1000)}"

        # Enqueue Task 1 then Task 2
        self.run_locutus(["enqueue", q, "--subject", "Task 1 Failing", "--body", "data1"])
        self.run_locutus(["enqueue", q, "--subject", "Task 2 Healthy", "--body", "data2"])

        # Claim Task 1 with 1s lease
        res_c1 = self.run_locutus(["claim", q, "--lease", "1"])
        self.assertEqual(res_c1.returncode, 0)
        self.assertIn("Task 1 Failing", res_c1.stdout)

        # Let lease expire
        time.sleep(1.2)

        # Claim next: Task 1 should be re-queued to tail (LPUSH), so Task 2 is popped next!
        res_c2 = self.run_locutus(["claim", q, "--lease", "10"])
        self.assertEqual(res_c2.returncode, 0)
        self.assertIn("Task 2 Healthy", res_c2.stdout)

    def test_46d_worker_cancellation_awareness(self):
        """Test --run-id cancellation interceptor in 'locutus work' and 'locutus claim'."""
        q = f"cancel_aware_q_{int(time.time() * 1000)}"
        run_id = f"run_worker_{int(time.time() * 1000)}"

        # Cancel the run
        res_c = self.run_locutus(["cancel", run_id, "--reason", "Workflow aborted by user"])
        self.assertEqual(res_c.returncode, 0)

        # Enqueue a task to the queue
        self.run_locutus(["enqueue", q, "--subject", "Cancelled Work", "--body", "should_not_run"])

        # 'work' with --run-id should detect cancellation and exit cleanly without processing
        t_start = time.time()
        res_work = self.run_locutus(["work", q, "10", "--run-id", run_id])
        elapsed = time.time() - t_start
        self.assertEqual(res_work.returncode, 0)
        self.assertLess(elapsed, 3.0, "Worker did not exit immediately upon detecting cancellation")
        self.assertIn("cancelled", (res_work.stderr + res_work.stdout).lower())

        # 'claim' with --run-id should also detect cancellation and exit cleanly
        t_start2 = time.time()
        res_claim = self.run_locutus(["claim", q, "10", "--run-id", run_id])
        elapsed2 = time.time() - t_start2
        self.assertEqual(res_claim.returncode, 0)
        self.assertLess(elapsed2, 3.0, "Claim did not exit immediately upon detecting cancellation")
        self.assertIn("cancelled", (res_claim.stderr + res_claim.stdout).lower())

    def test_46e_claim_socket_reuse_and_backoff(self):
        """Test socket reuse during claim polling and dynamic backoff on empty queue (TASK-15)."""
        def get_total_conns():
            res = subprocess.run(["redis-cli", "info", "stats"], capture_output=True, text=True, check=True)
            for line in res.stdout.splitlines():
                if line.startswith("total_connections_received:"):
                    return int(line.split(":")[1].strip())
            return 0

        q = f"reuse_q_{int(time.time() * 1000)}"

        # Record total connections received before running claim
        conns_before = get_total_conns()

        t0 = time.time()
        # Run claim with 2s timeout on empty queue
        res = self.run_locutus(["claim", q, "2"])
        elapsed = time.time() - t0

        conns_after = get_total_conns()
        # Note: 1 connection was used by our get_total_conns() check itself
        conns_delta = conns_after - conns_before - 1

        self.assertEqual(res.returncode, 0)
        self.assertGreaterEqual(elapsed, 1.8)
        # Without socket reuse, 2 seconds of 250ms polling opens ~8-16 connections.
        # With socket reuse, only 1 connection is opened by locutus claim.
        self.assertLessEqual(conns_delta, 2, f"Expected socket reuse (<= 2 connections), but got {conns_delta} connections")

    def test_47_blackboard_kv_append_and_snapshot(self):
        """Test 'locutus blackboard' (set, get, append, snapshot, delete, clear) with strict schema and Redis checks."""
        room = f"room_{int(time.time() * 1000)}"

        kv_key = f"{TEST_PREFIX}blackboard:{{{room}}}:kv"
        lists_index = f"{TEST_PREFIX}blackboard:{{{room}}}:lists"
        list_tasks_key = f"{TEST_PREFIX}blackboard:{{{room}}}:list:tasks"

        # 0. Argument validation negative controls
        res_no_args = self.run_locutus(["blackboard"])
        self.assertEqual(res_no_args.returncode, 1)
        self.assertIn("Usage: locutus blackboard", res_no_args.stderr)

        res_no_key = self.run_locutus(["blackboard", "set", room])
        self.assertEqual(res_no_key.returncode, 1)
        self.assertIn("Usage: locutus blackboard set", res_no_key.stderr)

        res_no_get_key = self.run_locutus(["blackboard", "get", room])
        self.assertEqual(res_no_get_key.returncode, 1)
        self.assertIn("Usage: locutus blackboard get", res_no_get_key.stderr)

        res_no_app_val = self.run_locutus(["blackboard", "append", room, "tasks"])
        self.assertEqual(res_no_app_val.returncode, 1)
        self.assertIn("Usage: locutus blackboard append", res_no_app_val.stderr)

        res_no_del_key = self.run_locutus(["blackboard", "delete", room])
        self.assertEqual(res_no_del_key.returncode, 1)
        self.assertIn("Usage: locutus blackboard delete", res_no_del_key.stderr)

        res_unknown = self.run_locutus(["blackboard", "bogus_action", room])
        self.assertEqual(res_unknown.returncode, 1)
        self.assertIn("Unknown blackboard action: bogus_action", res_unknown.stderr)

        try:
            # 1. Set key-value and get
            res_set = self.run_locutus(["blackboard", "set", room, "spec", '{"version": 1, "arch": "arm64"}'])
            self.assertEqual(res_set.returncode, 0)
            self.assertEqual(res_set.stdout.strip(), "OK")

            # Direct Redis inspection: check HGET on kv_key and TTL
            kv_val = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "HGET", kv_key, "spec"],
                capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertEqual(kv_val, '{"version": 1, "arch": "arm64"}')

            kv_ttl = int(subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "TTL", kv_key],
                capture_output=True, text=True, check=True
            ).stdout.strip())
            self.assertGreater(kv_ttl, 0)

            res_get = self.run_locutus(["blackboard", "get", room, "spec"])
            self.assertEqual(res_get.returncode, 0)
            self.assertEqual(json.loads(res_get.stdout.strip()), {"version": 1, "arch": "arm64"})

            # 2. Append to list and get list
            res_app1 = self.run_locutus(["blackboard", "append", room, "tasks", "Task A"])
            self.assertEqual(res_app1.returncode, 0)
            self.assertEqual(res_app1.stdout.strip(), "1")

            res_app2 = self.run_locutus(["blackboard", "append", room, "tasks", "Task B"])
            self.assertEqual(res_app2.returncode, 0)
            self.assertEqual(res_app2.stdout.strip(), "2")

            # Direct Redis inspection: check SISMEMBER in lists index and LRANGE on list key
            in_idx = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "SISMEMBER", lists_index, "tasks"],
                capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertEqual(in_idx, "1")

            list_items = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "LRANGE", list_tasks_key, "0", "-1"],
                capture_output=True, text=True, check=True
            ).stdout.strip().splitlines()
            self.assertEqual(list_items, ["Task A", "Task B"])

            res_get_list = self.run_locutus(["blackboard", "get", room, "tasks"])
            self.assertEqual(res_get_list.returncode, 0)
            self.assertEqual(json.loads(res_get_list.stdout.strip()), ["Task A", "Task B"])

            # 3. Snapshot whole room with LocutusPlugin schema validation
            res_snap = self.run_locutus(["blackboard", "snapshot", room])
            self.assertEqual(res_snap.returncode, 0)
            snap = LocutusPlugin.validate_json_schema("blackboard", res_snap.stdout)
            self.assertEqual(snap["room"], room)
            self.assertEqual(snap["kv"]["spec"], '{"version": 1, "arch": "arm64"}')
            self.assertEqual(snap["lists"]["tasks"], ["Task A", "Task B"])

            # Cluster slot affinity verification across all blackboard keys
            tag = LocutusPlugin.check_cluster_affinity([kv_key, lists_index, list_tasks_key])
            self.assertEqual(tag, room)

            # 4. Delete single key
            res_del = self.run_locutus(["blackboard", "delete", room, "spec"])
            self.assertEqual(res_del.returncode, 0)
            self.assertEqual(res_del.stdout.strip(), "OK")

            # Direct Redis inspection: spec key deleted from hash
            spec_exists = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "HEXISTS", kv_key, "spec"],
                capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertEqual(spec_exists, "0")

            res_get_after = self.run_locutus(["blackboard", "get", room, "spec"])
            self.assertEqual(res_get_after.returncode, 0)
            self.assertEqual(res_get_after.stdout.strip(), "")

            # Re-snapshot: kv empty, list preserved
            res_snap_mid = self.run_locutus(["blackboard", "snapshot", room])
            snap_mid = LocutusPlugin.validate_json_schema("blackboard", res_snap_mid.stdout)
            self.assertEqual(snap_mid["kv"], {})
            self.assertEqual(snap_mid["lists"]["tasks"], ["Task A", "Task B"])

            # 5. Clear entire room
            res_clear = self.run_locutus(["blackboard", "clear", room])
            self.assertEqual(res_clear.returncode, 0)
            self.assertEqual(res_clear.stdout.strip(), "OK")

            # Direct Redis inspection: keys completely removed
            chk_kv = subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", kv_key], capture_output=True, text=True, check=True)
            self.assertEqual(chk_kv.stdout.strip(), "0")
            chk_idx = subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", lists_index], capture_output=True, text=True, check=True)
            self.assertEqual(chk_idx.stdout.strip(), "0")
            chk_list = subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", list_tasks_key], capture_output=True, text=True, check=True)
            self.assertEqual(chk_list.stdout.strip(), "0")

            res_snap_after = self.run_locutus(["blackboard", "snapshot", room])
            self.assertEqual(res_snap_after.returncode, 0)
            snap_empty = LocutusPlugin.validate_json_schema("blackboard", res_snap_after.stdout)
            self.assertEqual(snap_empty["room"], room)
            self.assertEqual(snap_empty["kv"], {})
            self.assertEqual(snap_empty["lists"], {})

        finally:
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", kv_key, lists_index, list_tasks_key], capture_output=True)

    def test_47b_blackboard_encryption_and_tamper_detection(self):
        """Test blackboard transparent encryption with LOCUTUS_ENCRYPT=1 and HMAC verification."""
        room = f"enc_room_{int(time.time() * 1000)}"
        enc_env = {"LOCUTUS_ENCRYPT": "1"}

        # 1. Set key-value with encryption
        res_set = self.run_locutus(["blackboard", "set", room, "secret_cfg", '{"db_pass": "supersecret"}'], env_overrides=enc_env)
        self.assertEqual(res_set.returncode, 0)

        # Verify stored value in Redis is encrypted (not plaintext) and has aes256 header
        raw_res = subprocess.run(["redis-cli", "-u", REDIS_URL, "HGET", f"{TEST_PREFIX}blackboard:{{{room}}}:kv", "secret_cfg"], capture_output=True, text=True, check=True)
        raw_in_redis = raw_res.stdout.strip()
        self.assertNotIn("supersecret", raw_in_redis)
        self.assertTrue(raw_in_redis.startswith("aes256:"))

        # Verify get transparently decrypts
        res_get = self.run_locutus(["blackboard", "get", room, "secret_cfg"], env_overrides=enc_env)
        self.assertEqual(res_get.returncode, 0)
        self.assertEqual(json.loads(res_get.stdout.strip()), {"db_pass": "supersecret"})

        # 2. Append to list with encryption
        res_app1 = self.run_locutus(["blackboard", "append", room, "items", "encrypted_item_1"], env_overrides=enc_env)
        self.assertEqual(res_app1.returncode, 0)
        res_app2 = self.run_locutus(["blackboard", "append", room, "items", "encrypted_item_2"], env_overrides=enc_env)
        self.assertEqual(res_app2.returncode, 0)

        # Verify Redis raw list items are encrypted
        raw_list = subprocess.run(["redis-cli", "-u", REDIS_URL, "LRANGE", f"{TEST_PREFIX}blackboard:{{{room}}}:list:items", "0", "-1"], capture_output=True, text=True, check=True)
        self.assertNotIn("encrypted_item_1", raw_list.stdout)
        self.assertIn("aes256:", raw_list.stdout)

        # Verify get list decrypts items
        res_get_list = self.run_locutus(["blackboard", "get", room, "items"], env_overrides=enc_env)
        self.assertEqual(res_get_list.returncode, 0)
        self.assertEqual(json.loads(res_get_list.stdout.strip()), ["encrypted_item_1", "encrypted_item_2"])

        # 3. Snapshot decrypts kv and lists
        res_snap = self.run_locutus(["blackboard", "snapshot", room], env_overrides=enc_env)
        self.assertEqual(res_snap.returncode, 0)
        snap = json.loads(res_snap.stdout.strip())
        self.assertEqual(snap["kv"]["secret_cfg"], '{"db_pass": "supersecret"}')
        self.assertEqual(snap["lists"]["items"], ["encrypted_item_1", "encrypted_item_2"])

        # 4. Tamper detection: modify signature or payload in Redis
        parts = raw_in_redis.split(":")
        # Corrupt the ciphertext
        corrupted = f"{parts[0]}:{parts[1]}:bad_ciphertext"
        subprocess.run(["redis-cli", "-u", REDIS_URL, "HSET", f"{TEST_PREFIX}blackboard:{{{room}}}:kv", "secret_cfg", corrupted], check=True)
        res_tampered = self.run_locutus(["blackboard", "get", room, "secret_cfg"], env_overrides=enc_env)
        # Should fail with nonzero exit code or tamper warning
        self.assertNotEqual(res_tampered.returncode, 0)
        self.assertIn("tampered", (res_tampered.stderr + res_tampered.stdout).lower())

    def test_48_floor_control_ring(self):
        """Test 'locutus floor' (request, yield, pass, status, waiter queue, and timeout dequeue)."""
        room = f"floor_room_{int(time.time() * 1000)}"
        a1 = "speaker_alice"
        a2 = "speaker_bob"
        charlie = "speaker_charlie"
        dana = "speaker_dana"
        evan = "speaker_evan"

        holder_key = f"{TEST_PREFIX}floor:{{{room}}}:holder"
        waiters_key = f"{TEST_PREFIX}floor:{{{room}}}:waiters"

        # 0. Argument validation negative controls
        res_no_args = self.run_locutus(["floor"])
        self.assertEqual(res_no_args.returncode, 1)
        self.assertIn("Usage: locutus floor", res_no_args.stderr)

        res_no_room = self.run_locutus(["floor", "request"])
        self.assertEqual(res_no_room.returncode, 1)
        self.assertIn("Usage: locutus floor", res_no_room.stderr)

        res_pass_no_target = self.run_locutus(["floor", "pass", room], env_overrides={"LOCUTUS_AGENT_NAME": a1})
        self.assertEqual(res_pass_no_target.returncode, 1)
        self.assertIn("Error: Missing target agent for floor pass. Use --to <agent>.", res_pass_no_target.stderr)

        try:
            # 1. Initial status: room floor is free
            res_st0 = self.run_locutus(["floor", "status", room])
            self.assertEqual(res_st0.returncode, 0)
            st0 = json.loads(res_st0.stdout.strip())
            self.assertEqual(st0["room"], room)
            self.assertEqual(st0["holder"], "")
            self.assertEqual(st0["waiters"], [])

            # 2. Alice acquires the floor
            res_req1 = self.run_locutus(["floor", "request", room, "--lease", "10"], env_overrides={"LOCUTUS_AGENT_NAME": a1})
            self.assertEqual(res_req1.returncode, 0)
            self.assertIn(f"ACQUIRED: {a1} holds floor in {room}", res_req1.stdout)

            # Direct Redis inspection: verify holder key and TTL
            holder_in_redis = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "GET", holder_key],
                capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertEqual(holder_in_redis, a1)

            ttl_in_redis = int(subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "TTL", holder_key],
                capture_output=True, text=True, check=True
            ).stdout.strip())
            self.assertTrue(7 <= ttl_in_redis <= 10, f"Expected floor lease TTL between 7 and 10, got {ttl_in_redis}")

            # 3. Bob tries to acquire immediately without waiting -> negative control: must fail (BUSY)
            res_req2 = self.run_locutus(["floor", "request", room], env_overrides={"LOCUTUS_AGENT_NAME": a2})
            self.assertEqual(res_req2.returncode, 1)
            self.assertIn(f"Floor in {room} is held by {a1}", res_req2.stderr)

            # Unauthorized yield negative control: Bob cannot yield floor held by Alice
            res_unauth_yield = self.run_locutus(["floor", "yield", room], env_overrides={"LOCUTUS_AGENT_NAME": a2})
            self.assertEqual(res_unauth_yield.returncode, 1)
            self.assertIn(f"Floor is held by {a1}", res_unauth_yield.stderr)

            # Holder in Redis must still be Alice
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "GET", holder_key], capture_output=True, text=True, check=True).stdout.strip(), a1)

            # 4. Check status while held
            res_st = self.run_locutus(["floor", "status", room])
            self.assertEqual(res_st.returncode, 0)
            st = json.loads(res_st.stdout.strip())
            self.assertEqual(st["holder"], a1)
            self.assertEqual(st["room"], room)
            self.assertGreater(st["ttl"], 0)
            self.assertEqual(st["waiters"], [])

            # 5. Alice explicitly passes the floor to Bob
            res_pass = self.run_locutus(["floor", "pass", room, "--to", a2], env_overrides={"LOCUTUS_AGENT_NAME": a1})
            self.assertEqual(res_pass.returncode, 0)
            self.assertIn(f"PASSED:{a2}", res_pass.stdout)

            # Direct Redis inspection: holder is now Bob
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "GET", holder_key], capture_output=True, text=True, check=True).stdout.strip(), a2)

            res_st2 = self.run_locutus(["floor", "status", room])
            st2 = json.loads(res_st2.stdout.strip())
            self.assertEqual(st2["holder"], a2)

            # 6. Multi-waiter FIFO speaker ring handoff sequence:
            # While Bob holds the floor, Charlie and Dana request floor with 5s wait timeout in background threads
            charlie_res = []
            dana_res = []

            def charlie_waiter():
                r = self.run_locutus(["floor", "request", room, "5", "--lease", "5"], env_overrides={"LOCUTUS_AGENT_NAME": charlie})
                charlie_res.append(r)

            def dana_waiter():
                r = self.run_locutus(["floor", "request", room, "5", "--lease", "5"], env_overrides={"LOCUTUS_AGENT_NAME": dana})
                dana_res.append(r)

            t_c = threading.Thread(target=charlie_waiter)
            t_d = threading.Thread(target=dana_waiter)
            t_c.start()
            time.sleep(0.15)
            t_d.start()
            time.sleep(0.3)

            # Direct Redis inspection: waiters queue contains [charlie, dana] in FIFO order
            waiters = subprocess.run(
                ["redis-cli", "-u", REDIS_URL, "LRANGE", waiters_key, "0", "-1"],
                capture_output=True, text=True, check=True
            ).stdout.strip().splitlines()
            self.assertEqual(waiters, [charlie, dana], f"Expected waiters [{charlie}, {dana}], got {waiters}")

            # Bob yields -> floor passes to Charlie (first in FIFO queue)
            res_y_bob = self.run_locutus(["floor", "yield", room], env_overrides={"LOCUTUS_AGENT_NAME": a2})
            self.assertEqual(res_y_bob.returncode, 0)
            t_c.join(timeout=5)

            self.assertEqual(len(charlie_res), 1)
            self.assertEqual(charlie_res[0].returncode, 0)
            self.assertIn(f"ACQUIRED: {charlie} holds floor in {room}", charlie_res[0].stdout)

            # Direct Redis inspection: holder is now Charlie; remaining waiter is [dana]
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "GET", holder_key], capture_output=True, text=True, check=True).stdout.strip(), charlie)
            waiters_rem = subprocess.run(["redis-cli", "-u", REDIS_URL, "LRANGE", waiters_key, "0", "-1"], capture_output=True, text=True, check=True).stdout.strip().splitlines()
            self.assertEqual(waiters_rem, [dana])

            # Charlie yields -> floor passes to Dana
            res_y_charlie = self.run_locutus(["floor", "yield", room], env_overrides={"LOCUTUS_AGENT_NAME": charlie})
            self.assertEqual(res_y_charlie.returncode, 0)
            t_d.join(timeout=5)

            self.assertEqual(len(dana_res), 1)
            self.assertEqual(dana_res[0].returncode, 0)
            self.assertIn(f"ACQUIRED: {dana} holds floor in {room}", dana_res[0].stdout)

            # Direct Redis inspection: holder is now Dana; waiters queue is empty
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "GET", holder_key], capture_output=True, text=True, check=True).stdout.strip(), dana)
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "LLEN", waiters_key], capture_output=True, text=True, check=True).stdout.strip(), "0")

            # 7. Waiter timeout dequeue under load:
            # While Dana holds floor, Evan waits 1s, times out, and is cleanly dequeued
            res_evan_timeout = self.run_locutus(["floor", "request", room, "1", "--lease", "5"], env_overrides={"LOCUTUS_AGENT_NAME": evan})
            self.assertEqual(res_evan_timeout.returncode, 1)
            self.assertIn("Timeout waiting for floor", res_evan_timeout.stderr)

            # Verify Evan is NOT left in waiters queue in Redis
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "LLEN", waiters_key], capture_output=True, text=True, check=True).stdout.strip(), "0")

            # Dana yields -> no remaining waiters, so floor becomes FREE / YIELDED
            res_y_dana = self.run_locutus(["floor", "yield", room], env_overrides={"LOCUTUS_AGENT_NAME": dana})
            self.assertEqual(res_y_dana.returncode, 0)
            self.assertIn("YIELDED", res_y_dana.stdout)

            # Direct Redis inspection: holder key deleted
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", holder_key], capture_output=True, text=True, check=True).stdout.strip(), "0")

            # Verify Redis Cluster hash tag slot affinity between holder and waiters keys
            tag = LocutusPlugin.check_cluster_affinity([holder_key, waiters_key])
            self.assertEqual(tag, room)

        finally:
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", holder_key, waiters_key], capture_output=True)

    def test_49_cancellation_tokens(self):
        """Test global run cancellation tokens ('locutus cancel') with PubSub broadcast verification."""
        run_id = f"run_{int(time.time() * 1000)}"
        cancel_key = f"{TEST_PREFIX}cancel:{run_id}"
        global_chan = f"{TEST_PREFIX}channel:cancellations"
        run_chan = f"{TEST_PREFIX}channel:cancel:{run_id}"

        # 0. Missing argument negative controls
        res_no_args = self.run_locutus(["cancel"])
        self.assertEqual(res_no_args.returncode, 1)
        self.assertIn("Error: Missing run_id or cancel subcommand.", res_no_args.stderr)

        res_no_check_id = self.run_locutus(["cancel", "check"])
        self.assertEqual(res_no_check_id.returncode, 1)
        self.assertIn("Error: Missing run_id.", res_no_check_id.stderr)

        try:
            # 1. Uncancelled run check: returns empty stdout and code 0
            res_chk_empty = self.run_locutus(["cancel", "check", run_id])
            self.assertEqual(res_chk_empty.returncode, 0)
            self.assertEqual(res_chk_empty.stdout.strip(), "")

            # 2. Check with --exit-code on uncancelled run: returns exit code 1
            res_chk_exit1 = self.run_locutus(["cancel", "check", run_id, "--exit-code"])
            self.assertEqual(res_chk_exit1.returncode, 1)

            # 3. Subscribe to Redis broadcast channels in background process
            sub_proc = subprocess.Popen(
                ["redis-cli", "-u", REDIS_URL, "SUBSCRIBE", global_chan, run_chan],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            time.sleep(0.3)  # Wait for subscriptions to establish

            # 4. Cancel the run with a reason
            res_cancel = self.run_locutus([
                "cancel", run_id,
                "--reason", "User requested abort",
                "--by", "lead_agent"
            ])
            self.assertEqual(res_cancel.returncode, 0)
            c_data = json.loads(res_cancel.stdout.strip())
            self.assertTrue(c_data.get("cancelled"))
            self.assertEqual(c_data.get("run_id"), run_id)
            self.assertEqual(c_data.get("reason"), "User requested abort")
            self.assertEqual(c_data.get("by"), "lead_agent")
            self.assertIn("sig", c_data)
            self.assertEqual(len(c_data["sig"]), 64)

            # 5. Verify PubSub broadcast receipt on subscriber
            time.sleep(0.2)
            sub_proc.terminate()
            sub_stdout, _ = sub_proc.communicate(timeout=3)

            # Find published JSON payloads in subscriber output
            received_broadcasts = []
            for line in sub_stdout.splitlines():
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        received_broadcasts.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

            self.assertGreaterEqual(len(received_broadcasts), 1, f"No broadcast message received on {global_chan}. Output: {sub_stdout}")
            b_msg = received_broadcasts[0]
            self.assertEqual(b_msg.get("run_id"), run_id)
            self.assertEqual(b_msg.get("reason"), "User requested abort")
            self.assertEqual(b_msg.get("by"), "lead_agent")
            self.assertEqual(b_msg.get("sig"), c_data["sig"])
            self.assertTrue(b_msg.get("cancelled"))

            # Direct Redis inspection: check cancellation key payload and TTL
            raw_stored = subprocess.run(["redis-cli", "-u", REDIS_URL, "GET", cancel_key], capture_output=True, text=True, check=True).stdout.strip()
            stored_obj = json.loads(raw_stored)
            self.assertEqual(stored_obj["run_id"], run_id)
            self.assertEqual(stored_obj["sig"], c_data["sig"])

            key_ttl = int(subprocess.run(["redis-cli", "-u", REDIS_URL, "TTL", cancel_key], capture_output=True, text=True, check=True).stdout.strip())
            self.assertGreater(key_ttl, 0)

            # 6. Check on cancelled run: returns JSON and code 0
            res_chk = self.run_locutus(["cancel", "check", run_id])
            self.assertEqual(res_chk.returncode, 0)
            chk_data = json.loads(res_chk.stdout.strip())
            self.assertTrue(chk_data.get("cancelled"))
            self.assertEqual(chk_data.get("reason"), "User requested abort")
            self.assertEqual(chk_data.get("sig"), c_data["sig"])

            # 7. Check with --raw: prints bare reason
            res_raw = self.run_locutus(["cancel", "check", run_id, "--raw"])
            self.assertEqual(res_raw.returncode, 0)
            self.assertEqual(res_raw.stdout.strip(), "User requested abort")

            # 8. Check with --exit-code on cancelled run: returns code 0
            res_chk_exit0 = self.run_locutus(["cancel", "check", run_id, "--exit-code"])
            self.assertEqual(res_chk_exit0.returncode, 0)

            # 9. Test rejection of tampered/forged cancellation token
            forged_run = f"forged_{int(time.time() * 1000)}"
            forged_payload = json.dumps({
                "run_id": forged_run,
                "reason": "Forged malicious abort",
                "by": "attacker",
                "timestamp": "2026-09-19T00:00:00Z",
                "sig": "0000000000000000000000000000000000000000000000000000000000000000",
                "cancelled": True
            })
            subprocess.run(["redis-cli", "-u", REDIS_URL, "SET", f"{TEST_PREFIX}cancel:{forged_run}", forged_payload], check=True)
            res_chk_forged = self.run_locutus(["cancel", "check", forged_run, "--exit-code"])
            self.assertEqual(res_chk_forged.returncode, 1)
            self.assertIn("tampered", (res_chk_forged.stderr + res_chk_forged.stdout).lower())

            # 10. Clear the cancellation token and verify deletion in Redis
            res_clear = self.run_locutus(["cancel", "clear", run_id])
            self.assertEqual(res_clear.returncode, 0)

            chk_deleted = subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", cancel_key], capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(chk_deleted, "0", f"Cancellation key {cancel_key} was not deleted by clear")

            # 11. Check after clear: returns empty
            res_chk_after = self.run_locutus(["cancel", "check", run_id])
            self.assertEqual(res_chk_after.returncode, 0)
            self.assertEqual(res_chk_after.stdout.strip(), "")

        finally:
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", cancel_key, f"{TEST_PREFIX}cancel:{forged_run}"], capture_output=True)

    def test_50_blind_voting_ballot(self):
        """Test blind voting and ballot consensus ('locutus ballot') asserting vote secrecy and tamper resistance."""
        ballot_id = f"ballot_{int(time.time() * 1000)}"
        meta_key = f"{TEST_PREFIX}ballot:{{{ballot_id}}}"
        votes_key = f"{TEST_PREFIX}ballot:votes:{{{ballot_id}}}"
        sigs_key = f"{TEST_PREFIX}ballot:votes_sig:{{{ballot_id}}}"

        # 0. Argument validation negative controls
        res_no_args = self.run_locutus(["ballot"])
        self.assertEqual(res_no_args.returncode, 1)
        self.assertIn("Error: Missing ballot subcommand or ballot_id.", res_no_args.stderr)

        res_no_opts = self.run_locutus(["ballot", "open", ballot_id])
        self.assertEqual(res_no_opts.returncode, 1)
        self.assertIn("Error: Missing --options for ballot open.", res_no_opts.stderr)

        try:
            # 1. Open ballot with options and restricted voters
            res_open = self.run_locutus([
                "ballot", "open", ballot_id,
                "--options", "postgres,sqlite,redis",
                "--voters", "voter_a,voter_b,voter_c"
            ])
            self.assertEqual(res_open.returncode, 0)
            self.assertIn("OPEN", res_open.stdout)

            # Direct Redis inspection: meta hash initialized
            self.assertEqual(
                subprocess.run(["redis-cli", "-u", REDIS_URL, "HGET", meta_key, "status"], capture_output=True, text=True, check=True).stdout.strip(),
                "open"
            )

            # 2. Check status while open: voted_count is 0
            res_st = self.run_locutus(["ballot", "status", ballot_id])
            self.assertEqual(res_st.returncode, 0)
            st_data = json.loads(res_st.stdout.strip())
            self.assertEqual(st_data["status"], "open")
            self.assertEqual(st_data["voted_count"], 0)
            self.assertEqual(st_data["options"], ["postgres", "sqlite", "redis"])

            # 3. Cast legitimate votes
            res_v1 = self.run_locutus(["ballot", "cast", ballot_id, "--vote", "sqlite", "--voter", "voter_a"])
            self.assertEqual(res_v1.returncode, 0)
            self.assertIn("VOTED", res_v1.stdout)

            res_v2 = self.run_locutus(["ballot", "cast", ballot_id, "--vote", "postgres", "--voter", "voter_b"])
            self.assertEqual(res_v2.returncode, 0)

            res_v3 = self.run_locutus(["ballot", "cast", ballot_id, "--vote", "sqlite", "--voter", "voter_c"])
            self.assertEqual(res_v3.returncode, 0)

            # 4. Vote Secrecy verification: during voting, 'ballot status' MUST NOT leak individual votes or intermediate tallies
            res_st_mid = self.run_locutus(["ballot", "status", ballot_id])
            self.assertEqual(res_st_mid.returncode, 0)
            st_mid = json.loads(res_st_mid.stdout.strip())
            self.assertEqual(st_mid["voted_count"], 3)
            self.assertNotIn("votes", st_mid, "Blind ballot status must not reveal individual voter choices")
            self.assertNotIn("tally", st_mid, "Blind ballot status must not reveal intermediate tally")
            self.assertNotIn("winner", st_mid, "Blind ballot status must not reveal winner before tally")

            # 5. Negative control: Ineligible voter cannot vote
            res_unauth_voter = self.run_locutus(["ballot", "cast", ballot_id, "--vote", "sqlite", "--voter", "intruder_bot"])
            self.assertEqual(res_unauth_voter.returncode, 1)
            self.assertIn("not eligible", res_unauth_voter.stderr)

            # 6. Negative control: Invalid option rejected
            res_inv = self.run_locutus(["ballot", "cast", ballot_id, "--vote", "oracle", "--voter", "voter_a"])
            self.assertEqual(res_inv.returncode, 1)
            self.assertIn("Invalid vote choice 'oracle'", res_inv.stderr)

            # 7. Tally votes and close ballot
            res_tally = self.run_locutus(["ballot", "tally", ballot_id, "--close"])
            self.assertEqual(res_tally.returncode, 0)
            tally_data = json.loads(res_tally.stdout.strip())
            self.assertEqual(tally_data["total_votes"], 3)
            self.assertEqual(tally_data["winner"], "sqlite")
            self.assertEqual(tally_data["tally"]["sqlite"], 2)
            self.assertEqual(tally_data["tally"]["postgres"], 1)
            self.assertEqual(tally_data["tally"]["redis"], 0)
            self.assertEqual(tally_data["status"], "closed")
            # Tally output also purges individual voter choices to preserve privacy
            self.assertNotIn("votes", tally_data, "Tally report must not reveal voter choices")

            # 8. Tally with --raw prints winner
            res_tally_raw = self.run_locutus(["ballot", "tally", ballot_id, "--raw"])
            self.assertEqual(res_tally_raw.returncode, 0)
            self.assertEqual(res_tally_raw.stdout.strip(), "sqlite")

            # 9. Negative control: Casting vote after close is rejected
            res_post_close = self.run_locutus(["ballot", "cast", ballot_id, "--vote", "redis", "--voter", "voter_a"])
            self.assertEqual(res_post_close.returncode, 1)
            self.assertIn("Ballot is not open", res_post_close.stderr)

            # 10. Cluster slot affinity verification
            tag = LocutusPlugin.check_cluster_affinity([meta_key, votes_key, sigs_key])
            self.assertEqual(tag, ballot_id)

            # 11. Tamper verification on a second ballot
            ballot_id2 = f"ballot_sec_{int(time.time() * 1000)}"
            self.run_locutus(["ballot", "open", ballot_id2, "--options", "a,b"])
            # Cast legitimate vote for 'a'
            res_legit = self.run_locutus(["ballot", "cast", ballot_id2, "--voter", "agent1", "--vote", "a"])
            self.assertEqual(res_legit.returncode, 0)
            # Attacker forges an unauthenticated vote for 'b' in Redis
            subprocess.run(["redis-cli", "-u", REDIS_URL, "HSET", f"{TEST_PREFIX}ballot:votes:{{{ballot_id2}}}", "rogue_voter", "b"], check=True)
            # Tally should discard the forged vote from rogue_voter
            res_tally2 = self.run_locutus(["ballot", "tally", ballot_id2])
            self.assertEqual(res_tally2.returncode, 0)
            self.assertIn("tampered", (res_tally2.stderr + res_tally2.stdout).lower())
            tally2_data = json.loads(res_tally2.stdout.strip())
            self.assertEqual(tally2_data["total_votes"], 1)
            self.assertEqual(tally2_data["winner"], "a")
            self.assertEqual(tally2_data["tally"].get("b", 0), 0)

        finally:
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", meta_key, votes_key, sigs_key], capture_output=True)
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", f"{TEST_PREFIX}ballot:{{{ballot_id2}}}", f"{TEST_PREFIX}ballot:votes:{{{ballot_id2}}}"], capture_output=True)

    def test_51_leader_election(self):
        """Test leader election via lease preemption ('locutus leader') asserting automatic failover and HMAC security."""
        role = f"role_{int(time.time() * 1000)}"
        leader_key = f"{TEST_PREFIX}leader:{{{role}}}"
        leader_chan = f"{TEST_PREFIX}channel:leader:{{{role}}}"

        # 0. Argument validation negative controls
        res_no_args = self.run_locutus(["leader"])
        self.assertEqual(res_no_args.returncode, 1)
        self.assertIn("Error: Missing leader action or role name.", res_no_args.stderr)

        try:
            # 1. Initial status: role is vacant
            res_st0 = self.run_locutus(["leader", "status", role])
            self.assertEqual(res_st0.returncode, 0)
            st0 = json.loads(res_st0.stdout.strip())
            self.assertEqual(st0["role"], role)
            self.assertEqual(st0["status"], "vacant")
            self.assertEqual(st0["leader"], "")

            # 2. Lead 1 acquires leadership
            res_acq = self.run_locutus([
                "leader", "acquire", role,
                "--agent", "lead1",
                "--lease", "10"
            ])
            self.assertEqual(res_acq.returncode, 0)
            self.assertIn("ELECTED", res_acq.stdout)

            # Direct Redis inspection: check leader key, fields, and TTL
            raw_lead = subprocess.run(["redis-cli", "-u", REDIS_URL, "GET", leader_key], capture_output=True, text=True, check=True).stdout.strip()
            lead_obj = json.loads(raw_lead)
            self.assertEqual(lead_obj["leader"], "lead1")
            self.assertEqual(lead_obj["role"], role)
            self.assertIn("sig", lead_obj)
            self.assertEqual(len(lead_obj["sig"]), 64)

            lead_ttl = int(subprocess.run(["redis-cli", "-u", REDIS_URL, "TTL", leader_key], capture_output=True, text=True, check=True).stdout.strip())
            self.assertTrue(7 <= lead_ttl <= 10)

            # 3. Lead 2 attempts to acquire same role: exits with code 1, reports HELD:lead1
            res_acq_busy = self.run_locutus([
                "leader", "acquire", role,
                "--agent", "lead2",
                "--lease", "10"
            ])
            self.assertEqual(res_acq_busy.returncode, 1)
            self.assertIn("HELD:lead1", res_acq_busy.stdout + res_acq_busy.stderr)

            # 4. Status check
            res_st = self.run_locutus(["leader", "status", role])
            self.assertEqual(res_st.returncode, 0)
            st = json.loads(res_st.stdout.strip())
            self.assertEqual(st["role"], role)
            self.assertEqual(st["leader"], "lead1")
            self.assertEqual(st["status"], "active")
            self.assertEqual(st["sig"], lead_obj["sig"])

            # 5. Lead 1 renews lease
            res_ren = self.run_locutus([
                "leader", "renew", role,
                "--agent", "lead1",
                "--lease", "15"
            ])
            self.assertEqual(res_ren.returncode, 0)
            self.assertIn("RENEWED", res_ren.stdout)

            # Lead 2 fails to renew
            res_ren_bad = self.run_locutus([
                "leader", "renew", role,
                "--agent", "lead2",
                "--lease", "15"
            ])
            self.assertEqual(res_ren_bad.returncode, 1)

            # 6. Automatic failover after leader process dies and lease expires
            failover_role = f"fo_role_{int(time.time() * 1000)}"
            fo_key = f"{TEST_PREFIX}leader:{{{failover_role}}}"

            # Acquire leadership with short 2s lease
            res_fo_lead = self.run_locutus(["leader", "acquire", failover_role, "--agent", "primary_node", "--lease", "2"])
            self.assertEqual(res_fo_lead.returncode, 0)

            # Backup immediately tries to acquire -> rejected (HELD:primary_node)
            res_fo_backup_early = self.run_locutus(["leader", "acquire", failover_role, "--agent", "backup_node", "--lease", "5"])
            self.assertEqual(res_fo_backup_early.returncode, 1)
            self.assertIn("HELD:primary_node", res_fo_backup_early.stdout + res_fo_backup_early.stderr)

            # Primary node abruptly crashes; wait 2.2s for lease to expire in Redis
            time.sleep(2.2)
            chk_expired = subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", fo_key], capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(chk_expired, "0", "Leader lease key should have expired in Redis after primary crashed")

            # Backup node detects failure, acquires leadership upon lease expiry -> SUCCESS
            res_fo_backup_late = self.run_locutus(["leader", "acquire", failover_role, "--agent", "backup_node", "--lease", "5"])
            self.assertEqual(res_fo_backup_late.returncode, 0)
            self.assertIn("ELECTED", res_fo_backup_late.stdout)

            res_fo_st = self.run_locutus(["leader", "status", failover_role])
            self.assertEqual(res_fo_st.returncode, 0)
            fo_st = json.loads(res_fo_st.stdout.strip())
            self.assertEqual(fo_st["leader"], "backup_node")
            self.assertEqual(fo_st["status"], "active")

            # 7. Lead 1 resigns on original role
            res_res = self.run_locutus([
                "leader", "resign", role,
                "--agent", "lead1"
            ])
            self.assertEqual(res_res.returncode, 0)
            self.assertIn("RESIGNED", res_res.stdout)

            # 8. Now Lead 2 can acquire
            res_acq2 = self.run_locutus([
                "leader", "acquire", role,
                "--agent", "lead2",
                "--lease", "10"
            ])
            self.assertEqual(res_acq2.returncode, 0)
            self.assertIn("ELECTED", res_acq2.stdout)

            # 9. Test rejection of forged leader key
            forged_role = f"forged_role_{int(time.time() * 1000)}"
            forged_payload = json.dumps({
                "role": forged_role,
                "leader": "impostor",
                "acquired_at": "2026-09-19T00:00:00Z",
                "lease_sec": 60,
                "sig": "0000000000000000000000000000000000000000000000000000000000000000"
            })
            subprocess.run(["redis-cli", "-u", REDIS_URL, "SET", f"{TEST_PREFIX}leader:{{{forged_role}}}", forged_payload], check=True)
            res_st_forged = self.run_locutus(["leader", "status", forged_role])
            self.assertEqual(res_st_forged.returncode, 0)
            self.assertIn("tampered", (res_st_forged.stderr + res_st_forged.stdout).lower())
            st_forged = json.loads(res_st_forged.stdout.strip())
            self.assertEqual(st_forged["status"], "vacant")

            # Legitimate agent can preempt/evict forged leader
            res_preempt = self.run_locutus([
                "leader", "acquire", forged_role,
                "--agent", "legit_leader",
                "--lease", "10"
            ])
            self.assertEqual(res_preempt.returncode, 0)
            self.assertIn("ELECTED", res_preempt.stdout)

            # 10. Cluster slot affinity verification
            tag = LocutusPlugin.check_cluster_affinity([leader_key, leader_chan])
            self.assertEqual(tag, role)

        finally:
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", leader_key, f"{TEST_PREFIX}leader:{{{failover_role}}}", f"{TEST_PREFIX}leader:{{{forged_role}}}"], capture_output=True)

    def test_52_workflow_dag_engine(self):
        """Test DAG workflow engine ('locutus workflow') asserting parallel diamond DAG step unlocking and cycle prevention."""
        flow_id = f"flow_cli_{int(time.time() * 1000)}"
        flow_key = f"{TEST_PREFIX}workflow:{{{flow_id}}}"
        flow_chan = f"{TEST_PREFIX}channel:workflow:{{{flow_id}}}"

        # 0. Argument validation negative controls
        res_no_args = self.run_locutus(["workflow"])
        self.assertEqual(res_no_args.returncode, 1)
        self.assertIn("Error: Missing workflow action or flow ID.", res_no_args.stderr)

        res_no_steps = self.run_locutus(["workflow", "define", flow_id])
        self.assertEqual(res_no_steps.returncode, 1)
        self.assertIn("Missing steps", res_no_steps.stderr)

        # Negative control: cycle detection in dependency graph
        res_cycle = self.run_locutus([
            "workflow", "define", f"flow_cyc_{int(time.time()*1000)}",
            "--steps", "a,b",
            "--deps", "a:b;b:a"
        ])
        self.assertEqual(res_cycle.returncode, 1)
        self.assertIn("Cycle detected", res_cycle.stderr + res_cycle.stdout)

        # Negative control: dangling undeclared parent step
        res_dangle = self.run_locutus([
            "workflow", "define", f"flow_dan_{int(time.time()*1000)}",
            "--steps", "build",
            "--deps", "build:compile"
        ])
        self.assertEqual(res_dangle.returncode, 1)
        self.assertIn("Unknown dependency step 'compile'", res_dangle.stderr + res_dangle.stdout)

        try:
            # 1. Define diamond DAG: lint -> [test, build] -> deploy
            res_def = self.run_locutus([
                "workflow", "define", flow_id,
                "--steps", "lint,test,build,deploy",
                "--deps", "test:lint;build:lint;deploy:test,build"
            ])
            self.assertEqual(res_def.returncode, 0)
            self.assertIn("DEFINED", res_def.stdout)

            # Direct Redis inspection: workflow key exists and has status "running" (with root step ready)
            raw_flow = subprocess.run(["redis-cli", "-u", REDIS_URL, "GET", flow_key], capture_output=True, text=True, check=True).stdout.strip()
            flow_obj = json.loads(raw_flow)
            self.assertEqual(flow_obj["status"], "running")
            self.assertEqual(flow_obj["steps"]["lint"]["status"], "ready")
            self.assertEqual(flow_obj["steps"]["test"]["status"], "pending")
            self.assertEqual(flow_obj["steps"]["build"]["status"], "pending")
            self.assertEqual(flow_obj["steps"]["deploy"]["status"], "pending")

            # 2. Next: only root step 'lint' is ready
            res_next = self.run_locutus(["workflow", "next", flow_id])
            self.assertEqual(res_next.returncode, 0)
            next_data = json.loads(res_next.stdout.strip())
            self.assertEqual(next_data["ready"], ["lint"])

            # Next with --raw: prints bare step names
            res_next_raw = self.run_locutus(["workflow", "next", flow_id, "--raw"])
            self.assertEqual(res_next_raw.returncode, 0)
            self.assertEqual(res_next_raw.stdout.strip(), "lint")

            # 3. Resolve 'lint' -> diamond DAG parallel step unlocking:
            # Both 'test' and 'build' must be unlocked simultaneously
            res_res1 = self.run_locutus([
                "workflow", "resolve", flow_id, "lint",
                "--output", "all checks passed"
            ])
            self.assertEqual(res_res1.returncode, 0)
            r1_data = json.loads(res_res1.stdout.strip())
            self.assertEqual(r1_data["status"], "running")
            self.assertEqual(set(r1_data["unlocked"]), {"test", "build"})

            # workflow next must now return both 'test' and 'build' as ready
            res_next_mid = self.run_locutus(["workflow", "next", flow_id])
            self.assertEqual(res_next_mid.returncode, 0)
            next_mid = json.loads(res_next_mid.stdout.strip())
            self.assertEqual(set(next_mid["ready"]), {"test", "build"}, "Diamond DAG must unlock test and build simultaneously")

            # 4. Resolve 'test' -> 'deploy' still depends on 'build', so unlocked is empty
            res_res2 = self.run_locutus(["workflow", "resolve", flow_id, "test"])
            self.assertEqual(res_res2.returncode, 0)
            r2_data = json.loads(res_res2.stdout.strip())
            self.assertEqual(r2_data["unlocked"], [])

            # Only 'build' remains ready
            res_next_build = self.run_locutus(["workflow", "next", flow_id])
            self.assertEqual(res_next_build.returncode, 0)
            self.assertEqual(json.loads(res_next_build.stdout.strip())["ready"], ["build"])

            # 5. Resolve 'build' -> should unlock 'deploy'
            res_res3 = self.run_locutus(["workflow", "resolve", flow_id, "build", "--raw"])
            self.assertEqual(res_res3.returncode, 0)
            self.assertEqual(res_res3.stdout.strip(), "deploy")

            # Now 'deploy' is ready
            res_next_deploy = self.run_locutus(["workflow", "next", flow_id])
            self.assertEqual(res_next_deploy.returncode, 0)
            self.assertEqual(json.loads(res_next_deploy.stdout.strip())["ready"], ["deploy"])

            # 6. Resolve 'deploy' -> complete workflow
            res_res4 = self.run_locutus(["workflow", "resolve", flow_id, "deploy"])
            self.assertEqual(res_res4.returncode, 0)
            r4_data = json.loads(res_res4.stdout.strip())
            self.assertEqual(r4_data["status"], "completed")

            # 7. Status check with LocutusPlugin schema validation
            res_st = self.run_locutus(["workflow", "status", flow_id])
            self.assertEqual(res_st.returncode, 0)
            st_data = LocutusPlugin.validate_json_schema("workflow", res_st.stdout)
            self.assertEqual(st_data["status"], "completed")
            self.assertEqual(st_data["steps"]["lint"]["output"], "all checks passed")
            for step in ["lint", "test", "build", "deploy"]:
                self.assertEqual(st_data["steps"][step]["status"], "completed")

            # Status with --raw
            res_st_raw = self.run_locutus(["workflow", "status", flow_id, "--raw"])
            self.assertEqual(res_st_raw.returncode, 0)
            self.assertEqual(res_st_raw.stdout.strip(), "completed")

            # 8. Cluster slot affinity verification
            tag = LocutusPlugin.check_cluster_affinity([flow_key, flow_chan])
            self.assertEqual(tag, flow_id)

        finally:
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", flow_key], capture_output=True)

    def test_52b_workflow_encryption(self):
        """Test transparent encryption of workflow step outputs."""
        flow_id = f"flow_enc_{int(time.time() * 1000)}"
        enc_env = {"LOCUTUS_ENCRYPT": "1"}

        # Define DAG
        res_def = self.run_locutus([
            "workflow", "define", flow_id,
            "--steps", "step1,step2",
            "--deps", "step2:step1"
        ], env_overrides=enc_env)
        self.assertEqual(res_def.returncode, 0)

        # Resolve step1 with sensitive output
        secret_output = "confidential_api_token_9999"
        res_res = self.run_locutus([
            "workflow", "resolve", flow_id, "step1",
            "--output", secret_output
        ], env_overrides=enc_env)
        self.assertEqual(res_res.returncode, 0)

        # Verify raw data in Redis contains ciphertext and aes256 header, not plaintext
        raw_res = subprocess.run(["redis-cli", "-u", REDIS_URL, "GET", f"{TEST_PREFIX}workflow:{{{flow_id}}}"], capture_output=True, text=True, check=True)
        raw_in_redis = raw_res.stdout.strip()
        self.assertNotIn(secret_output, raw_in_redis)
        self.assertIn("aes256:", raw_in_redis)

        # Verify workflow status transparently decrypts the step output
        res_st = self.run_locutus(["workflow", "status", flow_id], env_overrides=enc_env)
        self.assertEqual(res_st.returncode, 0)
        st_data = json.loads(res_st.stdout.strip())
        self.assertEqual(st_data["steps"]["step1"]["output"], secret_output)

    def test_53_cluster_sweep(self):
        """Test cluster health watchdog and sweeper ('locutus sweep') asserting tag reverse index cleanup."""
        dead_bot = f"dead_sweep_{int(time.time() * 1000)}"
        stale_listener_bot = f"stale_listen_{int(time.time() * 1000)}"
        foreign_listener_bot = f"foreign_listen_{int(time.time() * 1000)}"

        import socket
        host = socket.gethostname()
        tag_worker_key = f"{TEST_PREFIX}tag:worker"
        tag_qa_key = f"{TEST_PREFIX}tag:qa"
        dead_agent_key = f"{TEST_PREFIX}agent:{dead_bot}"
        stale_lis_key = f"{TEST_PREFIX}listener:{stale_listener_bot}"
        foreign_lis_key = f"{TEST_PREFIX}listener:{foreign_listener_bot}"

        try:
            # 1. Inject dead agent with multiple tags (in active_agents and tag indexes without heartbeat)
            subprocess.run(["redis-cli", "-u", REDIS_URL, "SADD", f"{TEST_PREFIX}active_agents", dead_bot], check=True)
            subprocess.run(["redis-cli", "-u", REDIS_URL, "HSET", dead_agent_key, "tags", "worker,qa", "state", "idle"], check=True)
            subprocess.run(["redis-cli", "-u", REDIS_URL, "SADD", tag_worker_key, dead_bot], check=True)
            subprocess.run(["redis-cli", "-u", REDIS_URL, "SADD", tag_qa_key, dead_bot], check=True)

            # 2. Inject stale listener with dead PID 9999999 on this host
            stale_rec = json.dumps({"pid": 9999999, "host": host, "started": int(time.time())})
            subprocess.run(["redis-cli", "-u", REDIS_URL, "SET", stale_lis_key, stale_rec, "EX", "120"], check=True)

            # 3. Inject foreign host listener (must NOT be pruned by this node)
            foreign_rec = json.dumps({"pid": 9999999, "host": "foreign-cluster-node-99", "started": int(time.time())})
            subprocess.run(["redis-cli", "-u", REDIS_URL, "SET", foreign_lis_key, foreign_rec, "EX", "120"], check=True)

            # 4. Dry run
            res_dry = self.run_locutus(["sweep", "--dry-run"])
            self.assertEqual(res_dry.returncode, 0)
            dry_data = LocutusPlugin.validate_json_schema("sweep", res_dry.stdout)
            self.assertTrue(dry_data["dry_run"])
            self.assertIn(dead_bot, dry_data["pruned_agents"])
            self.assertIn(stale_listener_bot, dry_data["pruned_listeners"])
            dry_foreign_agents = [f["agent"] for f in dry_data.get("foreign_listeners", []) if isinstance(f, dict)]
            self.assertIn(foreign_listener_bot, dry_foreign_agents)

            # Confirm all records still exist after dry run
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "SISMEMBER", f"{TEST_PREFIX}active_agents", dead_bot], capture_output=True, text=True, check=True).stdout.strip(), "1")
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "SISMEMBER", tag_worker_key, dead_bot], capture_output=True, text=True, check=True).stdout.strip(), "1")
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "SISMEMBER", tag_qa_key, dead_bot], capture_output=True, text=True, check=True).stdout.strip(), "1")
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", stale_lis_key], capture_output=True, text=True, check=True).stdout.strip(), "1")
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", foreign_lis_key], capture_output=True, text=True, check=True).stdout.strip(), "1")

            # 5. Actual sweep
            res_sweep = self.run_locutus(["sweep"])
            self.assertEqual(res_sweep.returncode, 0)
            sweep_data = LocutusPlugin.validate_json_schema("sweep", res_sweep.stdout)
            self.assertFalse(sweep_data["dry_run"])
            self.assertIn(dead_bot, sweep_data["pruned_agents"])
            self.assertIn(stale_listener_bot, sweep_data["pruned_listeners"])
            sweep_foreign_agents = [f["agent"] for f in sweep_data.get("foreign_listeners", []) if isinstance(f, dict)]
            self.assertIn(foreign_listener_bot, sweep_foreign_agents)

            # Direct Redis assertions:
            # - Dead agent removed from active_agents
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "SISMEMBER", f"{TEST_PREFIX}active_agents", dead_bot], capture_output=True, text=True, check=True).stdout.strip(), "0")
            # - Dead agent metadata hash deleted
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", dead_agent_key], capture_output=True, text=True, check=True).stdout.strip(), "0")
            # - Green Mirage core assertion: Dead agent removed from all tag reverse index sets
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "SISMEMBER", tag_worker_key, dead_bot], capture_output=True, text=True, check=True).stdout.strip(), "0", f"Dead agent {dead_bot} was not pruned from tag:worker")
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "SISMEMBER", tag_qa_key, dead_bot], capture_output=True, text=True, check=True).stdout.strip(), "0", f"Dead agent {dead_bot} was not pruned from tag:qa")
            # - Stale local listener lock deleted
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", stale_lis_key], capture_output=True, text=True, check=True).stdout.strip(), "0")
            # - Foreign host listener safely preserved
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", foreign_lis_key], capture_output=True, text=True, check=True).stdout.strip(), "1", "Foreign listener was deleted by local node sweep")

            # 6. Sweep with --raw produces clean summary line
            res_raw = self.run_locutus(["sweep", "--raw"])
            self.assertEqual(res_raw.returncode, 0)
            self.assertIn("Pruned", res_raw.stdout)

        finally:
            subprocess.run(["redis-cli", "-u", REDIS_URL, "SREM", f"{TEST_PREFIX}active_agents", dead_bot], capture_output=True)
            subprocess.run(["redis-cli", "-u", REDIS_URL, "SREM", tag_worker_key, dead_bot], capture_output=True)
            subprocess.run(["redis-cli", "-u", REDIS_URL, "SREM", tag_qa_key, dead_bot], capture_output=True)
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", dead_agent_key, stale_lis_key, foreign_lis_key], capture_output=True)

    def test_54_lock_fencing_tokens(self):
        """Test distributed lock acquisition with monotonic fencing tokens ('locutus lock --fencing')."""
        lock_name = f"file:db_migration_{int(time.time() * 1000)}"
        a1 = "agent_fence_1"
        a2 = "agent_fence_2"
        a3 = "agent_fence_3"
        self.run_locutus(["open", a1, "dev"])
        self.run_locutus(["open", a2, "dev"])
        self.run_locutus(["open", a3, "dev"])

        lock_key = f"{TEST_PREFIX}lock:{{{lock_name}}}"
        fencing_key = f"{TEST_PREFIX}lock:fencing:{{{lock_name}}}"

        # 0. Argument validation negative controls
        res_no_name = self.run_locutus(["lock"])
        self.assertEqual(res_no_name.returncode, 1)
        self.assertIn("Error: Missing lock name.", res_no_name.stderr)

        res_no_unlock = self.run_locutus(["unlock"])
        self.assertEqual(res_no_unlock.returncode, 1)
        self.assertIn("Error: Missing lock name.", res_no_unlock.stderr)

        try:
            # 1. Iteration 1: Agent 1 acquires lock with --fencing
            res_acq1 = self.run_locutus(["lock", lock_name, "10", "--fencing"], env_overrides={"LOCUTUS_AGENT_NAME": a1})
            self.assertEqual(res_acq1.returncode, 0)
            self.assertIn(f"LOCKED {lock_name} by {a1}", res_acq1.stdout)
            self.assertIn("fencing: 1", res_acq1.stdout)

            # Direct Redis inspection: verify owner, fencing token, and TTL
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "GET", lock_key], capture_output=True, text=True, check=True).stdout.strip(), a1)
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "GET", fencing_key], capture_output=True, text=True, check=True).stdout.strip(), "1")
            ttl1 = int(subprocess.run(["redis-cli", "-u", REDIS_URL, "TTL", lock_key], capture_output=True, text=True, check=True).stdout.strip())
            self.assertTrue(7 <= ttl1 <= 10)

            # Negative control: Agent 2 tries to acquire held lock -> fails (code 1)
            res_acq_fail = self.run_locutus(["lock", lock_name, "10", "--fencing"], env_overrides={"LOCUTUS_AGENT_NAME": a2})
            self.assertEqual(res_acq_fail.returncode, 1)
            self.assertIn(f"Error: Lock '{lock_name}' is already held.", res_acq_fail.stderr)

            # Negative control: Agent 2 unauthorized unlock fails
            res_unauth = self.run_locutus(["unlock", lock_name], env_overrides={"LOCUTUS_AGENT_NAME": a2})
            self.assertEqual(res_unauth.returncode, 1)
            self.assertIn("not owner or lock not found", res_unauth.stderr)

            # Agent 1 unlocks
            res_un1 = self.run_locutus(["unlock", lock_name], env_overrides={"LOCUTUS_AGENT_NAME": a1})
            self.assertEqual(res_un1.returncode, 0)
            self.assertIn(f"UNLOCKED {lock_name}", res_un1.stdout)

            # Direct Redis inspection: lock key deleted, fencing counter preserved
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", lock_key], capture_output=True, text=True, check=True).stdout.strip(), "0")
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "GET", fencing_key], capture_output=True, text=True, check=True).stdout.strip(), "1")

            # 2. Iteration 2: Agent 2 acquires with --fencing and --raw -> prints bare monotonic token "2"
            res_acq2_raw = self.run_locutus(["lock", lock_name, "10", "--fencing", "--raw"], env_overrides={"LOCUTUS_AGENT_NAME": a2})
            self.assertEqual(res_acq2_raw.returncode, 0)
            token_2 = int(res_acq2_raw.stdout.strip())
            self.assertEqual(token_2, 2, f"Expected fencing token 2, got {token_2}")

            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "GET", lock_key], capture_output=True, text=True, check=True).stdout.strip(), a2)
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "GET", fencing_key], capture_output=True, text=True, check=True).stdout.strip(), "2")

            # Agent 2 unlocks
            res_un2 = self.run_locutus(["unlock", lock_name], env_overrides={"LOCUTUS_AGENT_NAME": a2})
            self.assertEqual(res_un2.returncode, 0)

            # 3. Iteration 3: Agent 3 acquires with --fencing and --raw -> prints bare monotonic token "3"
            res_acq3_raw = self.run_locutus(["lock", lock_name, "10", "--fencing", "--raw"], env_overrides={"LOCUTUS_AGENT_NAME": a3})
            self.assertEqual(res_acq3_raw.returncode, 0)
            token_3 = int(res_acq3_raw.stdout.strip())
            self.assertEqual(token_3, 3, f"Expected fencing token 3, got {token_3}")

            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "GET", lock_key], capture_output=True, text=True, check=True).stdout.strip(), a3)
            self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "GET", fencing_key], capture_output=True, text=True, check=True).stdout.strip(), "3")

            # Agent 3 unlocks
            res_un3 = self.run_locutus(["unlock", lock_name], env_overrides={"LOCUTUS_AGENT_NAME": a3})
            self.assertEqual(res_un3.returncode, 0)

            # 4. Strict monotonicity assertion across all 3 iterations
            self.assertEqual([1, token_2, token_3], [1, 2, 3], "Fencing tokens must be strictly monotonic consecutive integers")

            # 5. Re-unlock negative control: unlocking an already unlocked lock fails
            res_reunlock = self.run_locutus(["unlock", lock_name], env_overrides={"LOCUTUS_AGENT_NAME": a3})
            self.assertEqual(res_reunlock.returncode, 1)
            self.assertIn("not owner or lock not found", res_reunlock.stderr)

            # 6. Cluster slot affinity verification
            tag = LocutusPlugin.check_cluster_affinity([lock_key, fencing_key])
            self.assertEqual(tag, lock_name)

        finally:
            self.run_locutus(["close", a1])
            self.run_locutus(["close", a2])
            self.run_locutus(["close", a3])
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", lock_key, fencing_key], capture_output=True)

    def test_55_large_payload_blackboard_and_resp_guard(self):
        """Test large 100KB payload roundtrip through buffered RESP client and verify test_resp.nim suite."""
        room = f"large_buf_{int(time.time() * 1000)}"
        large_content = "X" * 100000
        large_json = json.dumps({"data": large_content})

        # 1. Blackboard set 100KB payload
        res_set = self.run_locutus(["blackboard", "set", room, "big_key", large_json])
        self.assertEqual(res_set.returncode, 0)

        # 2. Blackboard get 100KB payload
        res_get = self.run_locutus(["blackboard", "get", room, "big_key"])
        self.assertEqual(res_get.returncode, 0)
        parsed = json.loads(res_get.stdout.strip())
        self.assertEqual(len(parsed["data"]), 100000)
        self.assertEqual(parsed["data"], large_content)

        # 3. Verify large multi-chunk 500KB payload roundtrip through native nim-redis driver
        large_content_500k = "Y" * 500000
        large_json_500k = json.dumps({"data": large_content_500k})
        res_set_500k = self.run_locutus(["blackboard", "set", room, "big_key_500k", large_json_500k])
        self.assertEqual(res_set_500k.returncode, 0)

        res_get_500k = self.run_locutus(["blackboard", "get", room, "big_key_500k"])
        self.assertEqual(res_get_500k.returncode, 0)
        parsed_500k = json.loads(res_get_500k.stdout.strip())
        self.assertEqual(len(parsed_500k["data"]), 500000)
        self.assertEqual(parsed_500k["data"], large_content_500k)

    def test_56_graceful_signal_trapping(self):
        """Test graceful signal trapping (SIGTERM/SIGINT) cleans up active resources (TASK-18)."""
        agent = f"sig_bot_{int(time.time() * 1000)}"
        key = f"{TEST_PREFIX}listener:{agent}"

        # Start long-running listener process
        env = {**self.env, "LOCUTUS_AGENT_NAME": agent}
        p = subprocess.Popen(
            [BIN_PATH, "listen", agent, "60"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        try:
            # Wait for listener to register in Redis
            registered = False
            for _ in range(20):
                time.sleep(0.1)
                chk = subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", key], capture_output=True, text=True, check=True)
                if chk.stdout.strip() == "1":
                    registered = True
                    break
            self.assertTrue(registered, "Listener failed to register in Redis before signal test")

            # Send SIGTERM to process
            p.send_signal(signal.SIGTERM)
            p.wait(timeout=5)

            # Assert listener lock was cleanly deleted from Redis on signal exit
            chk_after = subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", key], capture_output=True, text=True, check=True)
            self.assertEqual(chk_after.stdout.strip(), "0", f"Listener lock {key} was orphaned in Redis after SIGTERM")
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", key], capture_output=True)

    def test_57_multi_host_sweeper_provenance(self):
        """Test multi-host/container sweeper provenance reporting (TASK-19)."""
        foreign_bot = f"foreign_agent_{int(time.time() * 1000)}"
        foreign_key = f"{TEST_PREFIX}listener:{foreign_bot}"
        foreign_rec = json.dumps({"pid": 12345, "host": "worker-node-99.internal", "started": int(time.time())})

        subprocess.run(["redis-cli", "-u", REDIS_URL, "SET", foreign_key, foreign_rec, "EX", "120"], check=True)

        try:
            res_sweep = self.run_locutus(["sweep", "--dry-run"])
            self.assertEqual(res_sweep.returncode, 0)
            data = json.loads(res_sweep.stdout.strip())

            self.assertIn("foreign_listeners", data, "Sweeper output missing 'foreign_listeners' provenance key")
            foreign_entries = [f for f in data["foreign_listeners"] if f.get("agent") == foreign_bot]
            self.assertEqual(len(foreign_entries), 1)
            entry = foreign_entries[0]
            self.assertEqual(entry["host"], "worker-node-99.internal")
            self.assertEqual(entry["pid"], 12345)
            self.assertEqual(entry["status"], "foreign_active")

            # Foreign listener must NOT be pruned
            chk = subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", foreign_key], capture_output=True, text=True, check=True)
            self.assertEqual(chk.stdout.strip(), "1")
        finally:
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", foreign_key], capture_output=True)

    def test_58_blackboard_snapshot_and_load(self):
        """Test blackboard snapshot export to file and load/restore from file with durability and schema validation."""
        room = f"bb_snap_room_{int(time.time() * 1000)}"
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_file = os.path.join(tmpdir, "snapshot.json")

            # 1. Populate blackboard
            self.run_locutus(["blackboard", "set", room, "version", "1.0.0"])
            self.run_locutus(["blackboard", "append", room, "todos", "write tests"])
            self.run_locutus(["blackboard", "append", room, "todos", "ship feature"])

            # 2. Export snapshot to file
            res_snap = self.run_locutus(["blackboard", "snapshot", room, snap_file])
            self.assertEqual(res_snap.returncode, 0)
            self.assertEqual(res_snap.stdout.strip(), "OK")
            self.assertTrue(os.path.isfile(snap_file))

            with open(snap_file, "r") as f:
                data = json.load(f)
            self.assertEqual(data["room"], room)
            self.assertEqual(data["kv"]["version"], "1.0.0")
            self.assertEqual(data["lists"]["todos"], ["write tests", "ship feature"])

            # 3. Clear room
            res_clr = self.run_locutus(["blackboard", "clear", room])
            self.assertEqual(res_clr.returncode, 0)
            res_get = self.run_locutus(["blackboard", "get", room, "version"])
            self.assertEqual(res_get.stdout.strip(), "")

            # 4. Restore from snapshot file
            res_load = self.run_locutus(["blackboard", "load", room, snap_file])
            self.assertEqual(res_load.returncode, 0)
            self.assertEqual(res_load.stdout.strip(), "OK")

            # 5. Verify restored state
            res_ver = self.run_locutus(["blackboard", "get", room, "version"])
            self.assertEqual(res_ver.returncode, 0)
            self.assertEqual(res_ver.stdout.strip(), "1.0.0")

            res_snap_again = self.run_locutus(["blackboard", "snapshot", room])
            self.assertEqual(res_snap_again.returncode, 0)
            data2 = json.loads(res_snap_again.stdout.strip())
            self.assertEqual(data2["lists"]["todos"], ["write tests", "ship feature"])

            # Negative control: invalid JSON snapshot file
            bad_file = os.path.join(tmpdir, "bad.json")
            with open(bad_file, "w") as f:
                f.write("not-valid-json")
            res_bad = self.run_locutus(["blackboard", "load", room, bad_file])
            self.assertEqual(res_bad.returncode, 1)
            self.assertIn("ERR: Invalid JSON snapshot", res_bad.stderr)

            # Cleanup
            self.run_locutus(["blackboard", "clear", room])

    def test_59_workflow_export_and_import(self):
        """Test workflow export to file and import from file (DAG state persistence)."""
        flow_id = f"flow_io_{int(time.time() * 1000)}"
        restored_flow = f"{flow_id}_restored"
        with tempfile.TemporaryDirectory() as tmpdir:
            export_file = os.path.join(tmpdir, "flow_export.json")

            # 1. Define and partially execute workflow
            res_def = self.run_locutus(["workflow", "define", flow_id, "--steps", "build,test,deploy", "--deps", "test:build;deploy:test"])
            self.assertEqual(res_def.returncode, 0)

            res_resolve = self.run_locutus(["workflow", "resolve", flow_id, "build", "--output", "binary compiled"])
            self.assertEqual(res_resolve.returncode, 0)

            # 2. Export workflow to file
            res_exp = self.run_locutus(["workflow", "export", flow_id, export_file])
            self.assertEqual(res_exp.returncode, 0)
            self.assertEqual(res_exp.stdout.strip(), "OK")
            self.assertTrue(os.path.isfile(export_file))

            with open(export_file, "r") as f:
                flow_data = json.load(f)
            self.assertEqual(flow_data["flow_id"], flow_id)
            self.assertEqual(flow_data["status"], "running")
            self.assertEqual(flow_data["steps"]["build"]["status"], "completed")
            self.assertEqual(flow_data["steps"]["build"]["output"], "binary compiled")
            self.assertEqual(flow_data["steps"]["test"]["status"], "ready")

            # 3. Import into a new workflow instance
            res_imp = self.run_locutus(["workflow", "import", restored_flow, export_file])
            self.assertEqual(res_imp.returncode, 0)
            self.assertEqual(res_imp.stdout.strip(), "OK")

            # 4. Verify unblocked progression on the imported flow
            res_next = self.run_locutus(["workflow", "next", restored_flow, "--raw"])
            self.assertEqual(res_next.returncode, 0)
            self.assertEqual(res_next.stdout.strip(), "test")

            # Negative control: missing import file or invalid payload
            res_missing = self.run_locutus(["workflow", "import", "bogus_flow"])
            self.assertEqual(res_missing.returncode, 1)

            bad_flow_file = os.path.join(tmpdir, "bad_flow.json")
            with open(bad_flow_file, "w") as f:
                f.write('{"random": "payload"}')
            res_bad = self.run_locutus(["workflow", "import", "bogus_flow", bad_flow_file])
            self.assertEqual(res_bad.returncode, 1)
            self.assertIn("ERR: Invalid JSON workflow payload", res_bad.stderr)

    def test_60_parse_required_int_validation(self):
        """Test strict integer validation across CLI commands with immediate failure (no silent swallows)."""
        # lock ttl
        res = self.run_locutus(["lock", "test_lock", "not_a_number"])
        self.assertEqual(res.returncode, 1)
        self.assertIn("Error: Invalid integer for lock ttl: 'not_a_number'", res.stderr)

        # claim --lease
        res = self.run_locutus(["claim", "test_q", "--lease", "bad_lease"])
        self.assertEqual(res.returncode, 1)
        self.assertIn("Error: Invalid integer for --lease: 'bad_lease'", res.stderr)

        # scatter --quorum
        res = self.run_locutus(["scatter", "--targets", "a", "--subject", "s", "--body", "b", "--quorum", "bad_quorum"])
        self.assertEqual(res.returncode, 1)
        self.assertIn("Error: Invalid integer for --quorum: 'bad_quorum'", res.stderr)

        # sub timeout
        res = self.run_locutus(["sub", "test_chan", "bad_timeout"])
        self.assertEqual(res.returncode, 1)
        self.assertIn("Error: Invalid integer for sub timeout: 'bad_timeout'", res.stderr)

        # anonymous agent rejection on leader acquire without open or identity
        with tempfile.TemporaryDirectory() as empty_dir:
            res_lead = self.run_locutus(["leader", "acquire", "test_role"], cwd=empty_dir, env_overrides={"LOCUTUS_AGENT_NAME": ""})
            self.assertEqual(res_lead.returncode, 1)
            self.assertIn("Error: No agent name specified.", res_lead.stderr)

            # anonymous agent rejection on floor request without open or identity
            res_floor = self.run_locutus(["floor", "request", "test_room"], cwd=empty_dir, env_overrides={"LOCUTUS_AGENT_NAME": ""})
            self.assertEqual(res_floor.returncode, 1)
            self.assertIn("Error: No agent name specified.", res_floor.stderr)

            # anonymous agent rejection on ballot cast without open or identity
            res_ballot = self.run_locutus(["ballot", "cast", "test_ballot", "--vote", "opt1"], cwd=empty_dir, env_overrides={"LOCUTUS_AGENT_NAME": ""})
            self.assertEqual(res_ballot.returncode, 1)
            self.assertIn("Error: No voter name specified.", res_ballot.stderr)

    def test_61_valkey_url_and_env_support(self):
        """Test valkey:// URL schemes, --valkey-url flag, and VALKEY_URL / LOCUTUS_VALKEY_URL env vars."""
        parsed = re.match(r"(?:redis|valkey)://([^:/]+)(?::(\d+))?", REDIS_URL)
        host = parsed.group(1) if parsed else "127.0.0.1"
        port = parsed.group(2) if parsed and parsed.group(2) else "6379"
        valkey_url = f"valkey://{host}:{port}"
        agent = f"valkey_agent_{int(time.time() * 1000)}"

        try:
            # 1. Register agent over valkey:// via --valkey-url
            res_open = self.run_locutus(["open", agent, "worker", "--valkey-url", valkey_url])
            self.assertEqual(res_open.returncode, 0)

            # 2. Test who with --valkey-url flag
            res_flag = self.run_locutus(["who", "--valkey-url", valkey_url])
            self.assertEqual(res_flag.returncode, 0)
            self.assertIn("AGENT", res_flag.stdout)
            self.assertIn(agent, res_flag.stdout)

            # 3. Test who with -u alias with valkey://
            res_alias = self.run_locutus(["who", "-u", valkey_url])
            self.assertEqual(res_alias.returncode, 0)
            self.assertIn(agent, res_alias.stdout)

            # 4. Test VALKEY_URL env override
            res_env = self.run_locutus(
                ["who"],
                env_overrides={
                    "LOCUTUS_REDIS_URL": "",
                    "REDIS_URL": "",
                    "VALKEY_URL": valkey_url,
                    "LOCUTUS_VALKEY_URL": ""
                }
            )
            self.assertEqual(res_env.returncode, 0)
            self.assertIn(agent, res_env.stdout)

            # 5. Test LOCUTUS_VALKEY_URL precedence over VALKEY_URL and REDIS_URL
            res_prec = self.run_locutus(
                ["who"],
                env_overrides={
                    "LOCUTUS_REDIS_URL": "",
                    "REDIS_URL": "redis://invalid-host-should-fail:6379",
                    "VALKEY_URL": "valkey://invalid-host-should-fail:6379",
                    "LOCUTUS_VALKEY_URL": valkey_url
                }
            )
            self.assertEqual(res_prec.returncode, 0)
            self.assertIn(agent, res_prec.stdout)

            # 6. Test blackboard operations over valkey://
            room = f"valkey_bb_{int(time.time() * 1000)}"
            res_bb_set = self.run_locutus(["blackboard", "set", room, "state", "valkey_ok", "--valkey-url", valkey_url])
            self.assertEqual(res_bb_set.returncode, 0)
            res_bb_get = self.run_locutus(["blackboard", "get", room, "state", "--valkey-url", valkey_url])
            self.assertEqual(res_bb_get.returncode, 0)
            self.assertEqual(res_bb_get.stdout.strip(), "valkey_ok")
        finally:
            self.run_locutus(["close", agent], env_overrides={"LOCUTUS_REDIS_URL": valkey_url})

        # 7. Negative controls: invalid scheme and bad port formatting fail fast with code 1
        res_bad_scheme = self.run_locutus(["who", "--valkey-url", "http://127.0.0.1:6379"])
        self.assertEqual(res_bad_scheme.returncode, 1)
        self.assertIn("Invalid Redis/Valkey URL scheme 'http'", res_bad_scheme.stderr)

        res_bad_port = self.run_locutus(["who", "--valkey-url", "valkey://127.0.0.1:not_a_port"])
        self.assertEqual(res_bad_port.returncode, 1)
        self.assertIn("Invalid port in Redis/Valkey URL: 'not_a_port'", res_bad_port.stderr)

    def test_62_unlink_nonblocking_cleanup(self):
        """Test non-blocking UNLINK semantics in blackboard delete/clear and sweep dead-agent pruning."""
        room = f"unlink_test_{int(time.time() * 1000)}"
        key1 = "k1"
        key2 = "k2"
        list_key = "l1"

        # 1. Blackboard set keys and append to list
        self.run_locutus(["blackboard", "set", room, key1, '{"v": 1}'])
        self.run_locutus(["blackboard", "set", room, key2, '{"v": 2}'])
        self.run_locutus(["blackboard", "append", room, list_key, "item1"])

        # Direct Redis check: keys exist
        kv_key = f"{TEST_PREFIX}blackboard:{{{room}}}:kv"
        rev_key = f"{TEST_PREFIX}blackboard:{{{room}}}:rev"
        lists_idx = f"{TEST_PREFIX}blackboard:{{{room}}}:lists"
        list_redis_key = f"{TEST_PREFIX}blackboard:{{{room}}}:list:{list_key}"

        self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "HEXISTS", kv_key, key1], capture_output=True, text=True, check=True).stdout.strip(), "1")
        self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "HEXISTS", kv_key, key2], capture_output=True, text=True, check=True).stdout.strip(), "1")
        self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", list_redis_key], capture_output=True, text=True, check=True).stdout.strip(), "1")
        self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "SISMEMBER", lists_idx, list_key], capture_output=True, text=True, check=True).stdout.strip(), "1")

        # 2. Blackboard delete list key (uses UNLINK)
        res_del = self.run_locutus(["blackboard", "delete", room, list_key])
        self.assertEqual(res_del.returncode, 0)
        self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", list_redis_key], capture_output=True, text=True, check=True).stdout.strip(), "0")
        self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "SISMEMBER", lists_idx, list_key], capture_output=True, text=True, check=True).stdout.strip(), "0")

        # 3. Blackboard clear room (uses UNLINK across multi-key expansion)
        res_clear = self.run_locutus(["blackboard", "clear", room])
        self.assertEqual(res_clear.returncode, 0)
        self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", kv_key], capture_output=True, text=True, check=True).stdout.strip(), "0")
        self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", rev_key], capture_output=True, text=True, check=True).stdout.strip(), "0")
        self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", lists_idx], capture_output=True, text=True, check=True).stdout.strip(), "0")

        # 4. Sweep dead-agent pruning via UNLINK
        dead_agent = f"dead_unlink_{int(time.time() * 1000)}"
        self.run_locutus(["open", dead_agent, "worker"])

        agent_key = f"{TEST_PREFIX}agent:{dead_agent}"
        hb_key = f"{TEST_PREFIX}heartbeat:{dead_agent}"
        self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", agent_key], capture_output=True, text=True, check=True).stdout.strip(), "1")

        # Delete heartbeat key so the agent is considered dead by sweep
        subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", hb_key], check=True, capture_output=True)

        # Run sweep
        res_sweep = self.run_locutus(["sweep"])
        self.assertEqual(res_sweep.returncode, 0)
        sweep_data = LocutusPlugin.validate_json_schema("sweep", res_sweep.stdout)
        self.assertIn(dead_agent, sweep_data["pruned_agents"])

        # Verify direct Redis state: agent hash was unlinked and removed from active_agents
        self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", agent_key], capture_output=True, text=True, check=True).stdout.strip(), "0")
        self.assertEqual(subprocess.run(["redis-cli", "-u", REDIS_URL, "SISMEMBER", f"{TEST_PREFIX}active_agents", dead_agent], capture_output=True, text=True, check=True).stdout.strip(), "0")

    def test_63_listener_reconnect_resilience(self):
        """Verify that long-running listener automatically reconnects with exponential backoff when connection is severed."""
        agent = f"reconn_ear_{int(time.time() * 1000)}"
        listener_key = f"{TEST_PREFIX}listener:{agent}"

        # 1. Spawn background listener
        p = subprocess.Popen(
            [BIN_PATH, "listen", agent, "10"],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        try:
            # 2. Wait for listener to register ownership in Redis
            registered = False
            for _ in range(20):
                time.sleep(0.1)
                chk = subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", listener_key], capture_output=True, text=True, check=True)
                if chk.stdout.strip() == "1":
                    registered = True
                    break
            self.assertTrue(registered, "Listener failed to register in Redis before connection sever test")

            # 3. Sever all normal client connections via Redis CLIENT KILL
            subprocess.run(["redis-cli", "-u", REDIS_URL, "CLIENT", "KILL", "TYPE", "normal", "SKIPME", "yes"], check=True, capture_output=True)

            # 4. Allow listener to detect disconnection and re-establish connection
            time.sleep(0.6)

            # 5. Send message to the agent
            res_send = self.run_locutus([
                "send",
                "--to", agent,
                "--from", "reconn_tester",
                "--subject", "Reconnection Test",
                "--body", "Payload delivered after disconnect"
            ])
            self.assertEqual(res_send.returncode, 0)

            # 6. Wait for listener to process message and exit cleanly
            stdout, stderr = p.communicate(timeout=8)
            self.assertEqual(p.returncode, 0, f"Listener failed with code {p.returncode}:\nSTDERR: {stderr}\nSTDOUT: {stdout}")

            # 7. Assert message received and diagnostic log emitted
            envelope = LocutusPlugin.validate_wire_envelope(stdout.strip())
            self.assertEqual(envelope["body"], "Payload delivered after disconnect")
            self.assertIn("Connection severed. Reconnecting", stderr)
            self.assertIn("Connection re-established successfully.", stderr)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", listener_key], capture_output=True)

    def test_64_claim_reconnect_on_severed_socket(self):
        """Verify that claim polling loop reconnects and continues claiming tasks after connection is severed."""
        q = f"claim_reconn_{int(time.time() * 1000)}"
        worker = f"worker_reconn_{int(time.time() * 1000)}"
        hb_key = f"{TEST_PREFIX}heartbeat:{worker}"

        # 1. Spawn background claim worker on empty queue
        p = subprocess.Popen(
            [BIN_PATH, "claim", q, "10"],
            env={**self.env, "LOCUTUS_AGENT_NAME": worker},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        try:
            # 2. Wait for worker heartbeat to register
            registered = False
            for _ in range(20):
                time.sleep(0.1)
                chk = subprocess.run(["redis-cli", "-u", REDIS_URL, "EXISTS", hb_key], capture_output=True, text=True, check=True)
                if chk.stdout.strip() == "1":
                    registered = True
                    break
            self.assertTrue(registered, "Claim worker failed to register heartbeat in Redis")

            # 3. Sever all normal client connections via Redis CLIENT KILL
            subprocess.run(["redis-cli", "-u", REDIS_URL, "CLIENT", "KILL", "TYPE", "normal", "SKIPME", "yes"], check=True, capture_output=True)

            # 4. Allow worker to reconnect
            time.sleep(0.6)

            # 5. Enqueue a task to the queue
            res_enq = self.run_locutus([
                "enqueue", q, "Sever Resilience Task", "Claimed post-sever task",
                "--from", "dispatcher"
            ])
            self.assertEqual(res_enq.returncode, 0)

            # 6. Wait for worker to claim task and exit cleanly
            stdout, stderr = p.communicate(timeout=8)
            self.assertEqual(p.returncode, 0, f"Claim worker failed with code {p.returncode}:\nSTDERR: {stderr}\nSTDOUT: {stdout}")

            # 7. Assert task claimed and diagnostic log emitted
            envelope = LocutusPlugin.validate_wire_envelope(stdout.strip())
            self.assertEqual(envelope["body"], "Claimed post-sever task")
            self.assertIn("Connection severed. Reconnecting", stderr)
            self.assertIn("Connection re-established successfully.", stderr)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            subprocess.run(["redis-cli", "-u", REDIS_URL, "DEL", hb_key], capture_output=True)


if __name__ == "__main__":
    unittest.main()



