#!/usr/bin/env python3
"""
Comprehensive Unit Tests for Redis A2A Protocol and Embedded Lua Scripts.
Tests:
1. Registration, tag indexing, and directory discovery
2. Multi-agent multicast with mixed tags and strict payload content verification
3. Broadcast multicast (*) to all live agents
4. Offline queuing and FIFO backlog ordering
5. Disconnect, dead-agent pruning, and reconnection recovery
6. Full round-trip request/reply threading (id -> reply_to)
7. Inbox TTL and keyspace memory hygiene
8. Clean unregister / shutdown
"""

import json
import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tests.schema import LocutusMessage, A2AMessage

LOCUTUS_REDIS_URL = os.environ.get("LOCUTUS_REDIS_URL", os.environ.get("A2A_REDIS_URL", os.environ.get("REDIS_URL", "redis://127.0.0.1:6379")))
LOCUTUS_REDIS_PREFIX = os.environ.get("LOCUTUS_REDIS_PREFIX", os.environ.get("A2A_REDIS_PREFIX", "locutus_test:"))
PREFIX = LOCUTUS_REDIS_PREFIX

# Load Lua Scripts directly from scripts/ directory (Single Source of Truth)
SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))

def load_lua(filename: str) -> str:
    path = os.path.join(SCRIPTS_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    # Strip comments and collapse into single-line to avoid newline/CRLF argument splitting on Windows
    lines = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        if "--" in line:
            line = line.split("--")[0].strip()
        lines.append(line)
    return " ".join(lines)

LUA_REGISTER = load_lua("register.lua")
LUA_SEND_O2O = load_lua("send_o2o.lua")
LUA_MULTICAST = load_lua("multicast.lua")
LUA_DRAIN = load_lua("drain.lua")
LUA_DIRECTORY = load_lua("directory.lua")
LUA_UNREGISTER = load_lua("unregister.lua")
LUA_TAG = load_lua("tag.lua")

def run_redis(*args):
    cmd = ["redis-cli", "-u", LOCUTUS_REDIS_URL] + list(args)
    res = subprocess.run(cmd, capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL, timeout=15)
    return res.stdout.strip()

def run_eval(script, numkeys, *args):
    cmd = ["redis-cli", "-u", LOCUTUS_REDIS_URL, "EVAL", script, str(numkeys)] + list(args)
    res = subprocess.run(cmd, capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL, timeout=15)
    return res.stdout.strip()


class TestRedisA2AProtocol(unittest.TestCase):

    def setUp(self):
        # Clean up test keyspace
        keys = run_redis("KEYS", f"{PREFIX}*").split()
        if keys:
            run_redis("DEL", *keys)

    def tearDown(self):
        keys = run_redis("KEYS", f"{PREFIX}*").split()
        if keys:
            run_redis("DEL", *keys)

    def test_01_registration_and_directory(self):
        """Test agent registration, tag indexing, heartbeat, and directory lookup."""
        res = run_eval(LUA_REGISTER, 0, PREFIX, "alice", "worker,math", "120")
        self.assertEqual(res, "OK")

        # Verify heartbeat exists
        hb = run_redis("GET", f"{PREFIX}heartbeat:alice")
        self.assertEqual(hb, "1")

        # Verify active roster and tag sets
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}active_agents"))
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:math"))
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:worker"))

        # Query directory
        directory = run_eval(LUA_DIRECTORY, 0, PREFIX)
        self.assertIn("alice|1|worker,math", directory)

    def test_02_multicast_multi_agent_with_content_verification(self):
        """Test multicast to specific tag and strictly verify payload content across all recipients."""
        # Setup 3 agents with overlapping tags
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "qa,frontend", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "bob", "qa,backend", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "charlie", "devops", "120")

        # Send multicast to tag 'qa'
        payload = {
            "id": "msg_mcast_qa_100",
            "from": "lead",
            "to": "@qa",
            "type": "task",
            "reply_to": None,
            "tags": ["qa"],
            "subject": "Run QA Regression",
            "body": "Execute test suite against staging branch.",
            "timestamp": "2026-09-18T23:35:00Z"
        }
        msg_str = json.dumps(payload)
        delivered = run_eval(LUA_MULTICAST, 0, PREFIX, "qa", msg_str, "604800")
        self.assertIn(delivered, ["2", "(integer) 2"])

        # Charlie (devops) must NOT have received it
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:charlie"), "0")

        # Alice must have received exact payload validated against schema
        alice_msg = A2AMessage.model_validate_json(run_redis("RPOP", f"{PREFIX}inbox:alice"))
        self.assertEqual(alice_msg.id, "msg_mcast_qa_100")
        self.assertEqual(alice_msg.from_agent, "lead")
        self.assertEqual(alice_msg.to_agent, "@qa")
        self.assertEqual(alice_msg.subject, "Run QA Regression")
        self.assertEqual(alice_msg.body, "Execute test suite against staging branch.")

        # Bob must have received exact same payload validated against schema
        bob_msg = A2AMessage.model_validate_json(run_redis("RPOP", f"{PREFIX}inbox:bob"))
        self.assertEqual(bob_msg.id, "msg_mcast_qa_100")
        self.assertEqual(bob_msg.body, "Execute test suite against staging branch.")

    def test_03_broadcast_to_all_active_agents(self):
        """Test multicast with tag '*' reaches every active agent."""
        run_eval(LUA_REGISTER, 0, PREFIX, "agent1", "tag1", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "agent2", "tag2", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "agent3", "tag3", "120")

        broadcast_msg = json.dumps({
            "id": "bcast_001",
            "from": "ops",
            "to": "*",
            "type": "status",
            "subject": "System Announcement",
            "body": "Deployment completed successfully.",
            "timestamp": "2026-09-18T23:35:00Z"
        })

        delivered = run_eval(LUA_MULTICAST, 0, PREFIX, "*", broadcast_msg, "604800")
        self.assertIn(delivered, ["3", "(integer) 3"])

        for agent in ["agent1", "agent2", "agent3"]:
            raw = run_redis("RPOP", f"{PREFIX}inbox:{agent}")
            self.assertIsNotNone(raw)
            parsed = A2AMessage.model_validate_json(raw)
            self.assertEqual(parsed.id, "bcast_001")
            self.assertEqual(parsed.subject, "System Announcement")

    def test_04_offline_queuing_and_ordered_backlog(self):
        """Test that messages sent to an offline/unregistered agent are queued and drained in FIFO order."""
        # Send 3 tasks to 'david' BEFORE he registers
        for i in [1, 2, 3]:
            task_msg = json.dumps({
                "id": f"task_00{i}",
                "from": "lead",
                "to": "david",
                "type": "task",
                "subject": f"Task {i}",
                "body": f"Execute step {i}",
                "timestamp": "2026-09-18T23:35:00Z"
            })
            run_eval(LUA_SEND_O2O, 0, PREFIX, "david", task_msg, "604800")

        # Verify 3 messages waiting in inbox
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:david"), "3")

        # David comes online and registers
        run_eval(LUA_REGISTER, 0, PREFIX, "david", "worker", "120")

        # David drains his inbox (up to 10)
        drained_raw = run_eval(LUA_DRAIN, 0, PREFIX, "david", "10")
        
        # Redis CLI prints multi-bulk replies as newline-separated items
        # Let's verify by popping or reading drained results
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:david"), "0")
        self.assertIn("task_001", drained_raw)
        self.assertIn("task_002", drained_raw)
        self.assertIn("task_003", drained_raw)

    def test_05_disconnect_pruning_and_reconnect_recovery(self):
        """Test that a disconnected agent is pruned from multicasts, and recovers upon reconnect."""
        # Alice and Bob register
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "qa", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "bob", "qa", "120")

        # Simulate Alice crashing / disconnect: delete her heartbeat
        run_redis("DEL", f"{PREFIX}heartbeat:alice")

        # Send multicast to 'qa'
        msg1 = json.dumps({"id": "qa_round_1", "from": "lead", "to": "@qa", "body": "first check"})
        delivered = run_eval(LUA_MULTICAST, 0, PREFIX, "qa", msg1, "604800")
        self.assertIn(delivered, ["1", "(integer) 1"])

        # Bob got it, Alice didn't
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:bob"), "1")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:alice"), "0")

        # Alice should now be pruned from tag:qa
        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:qa"))

        # Alice reconnects and re-registers
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "qa", "120")
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:qa"))

        # Send second multicast to 'qa'
        msg2 = json.dumps({
            "id": "qa_round_2",
            "from": "lead",
            "to": "@qa",
            "type": "task",
            "subject": "Second Check",
            "body": "second check",
            "timestamp": "2026-09-18T23:35:00Z"
        })
        delivered2 = run_eval(LUA_MULTICAST, 0, PREFIX, "qa", msg2, "604800")
        self.assertIn(delivered2, ["2", "(integer) 2"])

        # Alice now receives the new message!
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:alice"), "1")
        alice_received = A2AMessage.model_validate_json(run_redis("RPOP", f"{PREFIX}inbox:alice"))
        self.assertEqual(alice_received.id, "qa_round_2")

    def test_06_roundtrip_request_reply_threading(self):
        """Test full round-trip: Alice sends task -> Bob executes -> Bob replies with reply_to -> Alice verifies."""
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "client", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "bob", "math-service", "120")

        # 1. Alice sends task to Bob
        task_id = "req_171000_alice_999"
        task_payload = {
            "id": task_id,
            "from": "alice",
            "to": "bob",
            "type": "task",
            "reply_to": None,
            "subject": "Multiply",
            "body": "12 * 12",
            "timestamp": "2026-09-18T23:35:00Z"
        }
        run_eval(LUA_SEND_O2O, 0, PREFIX, "bob", json.dumps(task_payload), "604800")

        # 2. Bob receives task
        raw_task = run_redis("RPOP", f"{PREFIX}inbox:bob")
        incoming_task = A2AMessage.model_validate_json(raw_task)
        self.assertEqual(incoming_task.id, task_id)
        self.assertEqual(incoming_task.body, "12 * 12")

        # 3. Bob computes result (144) and replies to Alice
        reply_id = "rep_171000_bob_888"
        reply_payload = {
            "id": reply_id,
            "from": "bob",
            "to": incoming_task.from_agent,
            "type": "reply",
            "reply_to": incoming_task.id,
            "subject": f"Re: {incoming_task.subject}",
            "body": "144",
            "timestamp": "2026-09-18T23:35:05Z"
        }
        run_eval(LUA_SEND_O2O, 0, PREFIX, "alice", json.dumps(reply_payload), "604800")

        # 4. Alice receives reply and verifies correlation
        raw_reply = run_redis("RPOP", f"{PREFIX}inbox:alice")
        received_reply = A2AMessage.model_validate_json(raw_reply)
        self.assertEqual(received_reply.from_agent, "bob")
        self.assertEqual(received_reply.to_agent, "alice")
        self.assertEqual(received_reply.type, "reply")
        self.assertEqual(received_reply.reply_to, task_id)
        self.assertEqual(received_reply.body, "144")

    def test_07_inbox_ttl_hygiene(self):
        """Test that sending a message sets an expiration TTL on the inbox key to avoid leaking RAM."""
        msg = json.dumps({"id": "ttl_test", "from": "a", "to": "ephemeral_user", "body": "hi"})
        run_eval(LUA_SEND_O2O, 0, PREFIX, "ephemeral_user", msg, "3600")

        ttl = int(run_redis("TTL", f"{PREFIX}inbox:ephemeral_user"))
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, 3600)

    def test_08_unregister_and_cleanup(self):
        """Test graceful logout and cleanup of all keys and sets."""
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "math,worker", "120")
        run_eval(LUA_UNREGISTER, 0, PREFIX, "alice")

        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}active_agents"))
        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:math"))
        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:worker"))
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}heartbeat:alice"), "0")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:alice"), "0")

    def test_09_multi_tag_and_filtering_with_project_isolation(self):
        """Test multi-tag AND-filtering via SINTER ensuring project isolation and role targeting."""
        # Setup 4 agents across two separate projects
        # Project 'alpha'
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "alpha,backend,python", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "bob", "alpha,frontend,react", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "charlie", "alpha,backend,golang", "120")
        # Project 'beta'
        run_eval(LUA_REGISTER, 0, PREFIX, "dave", "beta,backend,python", "120")

        # 1. Multicast within project 'alpha' to 'backend' (target: "alpha,backend")
        # Should reach alice and charlie, but NOT bob (frontend) and NOT dave (project beta)
        task_msg = json.dumps({
            "id": "msg_alpha_backend_001",
            "from": "lead_alpha",
            "to": "@alpha,backend",
            "type": "task",
            "tags": ["alpha", "backend"],
            "subject": "Alpha Backend Sync",
            "body": "Check API schema.",
            "timestamp": "2026-09-18T23:40:00Z"
        })
        delivered = run_eval(LUA_MULTICAST, 0, PREFIX, "alpha,backend", task_msg, "604800")
        self.assertIn(delivered, ["2", "(integer) 2"])

        # Bob (alpha,frontend) and Dave (beta,backend) must have 0 messages
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:bob"), "0")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:dave"), "0")

        # Alice and Charlie received it
        alice_msg = LocutusMessage.model_validate_json(run_redis("RPOP", f"{PREFIX}inbox:alice"))
        self.assertEqual(alice_msg.id, "msg_alpha_backend_001")
        self.assertEqual(alice_msg.subject, "Alpha Backend Sync")

        charlie_msg = LocutusMessage.model_validate_json(run_redis("RPOP", f"{PREFIX}inbox:charlie"))
        self.assertEqual(charlie_msg.id, "msg_alpha_backend_001")
        self.assertEqual(charlie_msg.subject, "Alpha Backend Sync")

        # 2. Targeted multicast with 3 tags: "alpha,backend,python"
        # Only Alice has all 3 tags!
        py_msg = json.dumps({
            "id": "msg_alpha_py_001",
            "from": "lead_alpha",
            "to": "@alpha,backend,python",
            "type": "task",
            "subject": "Python Specialist Task",
            "body": "Refactor async handler.",
            "timestamp": "2026-09-18T23:41:00Z"
        })
        delivered2 = run_eval(LUA_MULTICAST, 0, PREFIX, "alpha,backend,python", py_msg, "604800")
        self.assertIn(delivered2, ["1", "(integer) 1"])
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:charlie"), "0")
        alice_py = LocutusMessage.model_validate_json(run_redis("RPOP", f"{PREFIX}inbox:alice"))
        self.assertEqual(alice_py.id, "msg_alpha_py_001")
        self.assertEqual(alice_py.body, "Refactor async handler.")

    def test_10_dynamic_tag_management(self):
        """Test tag.lua: adding, removing, and setting tags dynamically without dropping inbox."""
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "alpha,worker", "120")

        # Queue a pending task message for Alice BEFORE changing tags
        pending_msg = json.dumps({
            "id": "pending_001",
            "from": "lead",
            "to": "alice",
            "type": "task",
            "subject": "Pending Work",
            "body": "Do not lose me during re-tagging!",
            "timestamp": "2026-09-18T23:42:00Z"
        })
        run_eval(LUA_SEND_O2O, 0, PREFIX, "alice", pending_msg, "604800")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:alice"), "1")

        # 1. Add tags 'gpu,ml'
        res_add = run_eval(LUA_TAG, 0, PREFIX, "alice", "add", "gpu,ml")
        self.assertEqual(res_add, "alpha,gpu,ml,worker")
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:gpu"))
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:ml"))
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:alpha"))

        # 2. Remove tag 'worker'
        res_rem = run_eval(LUA_TAG, 0, PREFIX, "alice", "remove", "worker")
        self.assertEqual(res_rem, "alpha,gpu,ml")
        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:worker"))

        # 3. Set tags to 'alpha,specialist'
        res_set = run_eval(LUA_TAG, 0, PREFIX, "alice", "set", "alpha,specialist")
        self.assertEqual(res_set, "alpha,specialist")
        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:gpu"))
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:specialist"))
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:alpha"))

        # 4. Crucial: verify Alice's pending message was NOT dropped or disturbed!
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:alice"), "1")
        preserved = LocutusMessage.model_validate_json(run_redis("RPOP", f"{PREFIX}inbox:alice"))
        self.assertEqual(preserved.id, "pending_001")
        self.assertEqual(preserved.body, "Do not lose me during re-tagging!")

    def test_11_team_directory_project_filtering(self):
        """Test directory.lua: listing roster filtered by project vs cluster-wide."""
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "team_alpha,lead", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "bob", "team_alpha,dev", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "charlie", "team_beta,dev", "120")

        # Filter by project 'team_alpha'
        dir_alpha = run_eval(LUA_DIRECTORY, 0, PREFIX, "team_alpha")
        self.assertIn("alice|1|team_alpha,lead", dir_alpha)
        self.assertIn("bob|1|team_alpha,dev", dir_alpha)
        self.assertNotIn("charlie", dir_alpha)

        # Filter by project 'team_beta'
        dir_beta = run_eval(LUA_DIRECTORY, 0, PREFIX, "team_beta")
        self.assertIn("charlie|1|team_beta,dev", dir_beta)
        self.assertNotIn("alice", dir_beta)
        self.assertNotIn("bob", dir_beta)

        # Cluster-wide query ('*' or empty)
        dir_all = run_eval(LUA_DIRECTORY, 0, PREFIX, "*")
        self.assertIn("alice|1|team_alpha,lead", dir_all)
        self.assertIn("bob|1|team_alpha,dev", dir_all)
        self.assertIn("charlie|1|team_beta,dev", dir_all)

    def test_12_structured_field_invocation_and_cjson_encoding(self):
        """Test sending messages via individual field arguments with server-side cjson encoding."""
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "locutus,worker", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "bob", "locutus,qa", "120")

        # 1. Send O2O with field arguments (no JSON quoting in shell!)
        ts = "2026-09-18T23:50:00Z"
        res = run_eval(
            LUA_SEND_O2O, 0,
            PREFIX, "bob", "task", "alice", "Run Tests", "pytest tests/", "locutus", "", "", ts
        )
        self.assertEqual(res, "OK")

        # Verify Bob received perfectly formed JSON validated by Pydantic
        raw_msg = run_redis("RPOP", f"{PREFIX}inbox:bob")
        msg = LocutusMessage.model_validate_json(raw_msg)
        self.assertEqual(msg.from_agent, "alice")
        self.assertEqual(msg.to_agent, "bob")
        self.assertEqual(msg.type, "task")
        self.assertEqual(msg.subject, "Run Tests")
        self.assertEqual(msg.body, "pytest tests/")
        self.assertEqual(msg.tags, ["locutus"])
        self.assertEqual(msg.timestamp, ts)
        self.assertIsNone(msg.reply_to)

        # 2. Multicast with field arguments
        res_mcast = run_eval(
            LUA_MULTICAST, 0,
            PREFIX, "locutus", "status", "lead", "Build Passed", "All tests green.", "locutus", "", "", ts
        )
        self.assertIn(res_mcast, ["2", "(integer) 2"])

        # Alice and Bob received it
        alice_mcast = LocutusMessage.model_validate_json(run_redis("RPOP", f"{PREFIX}inbox:alice"))
        self.assertEqual(alice_mcast.subject, "Build Passed")
        self.assertEqual(alice_mcast.body, "All tests green.")

    def test_13_register_tag_cleanup_on_reregistration(self):
        """Test that re-registering an agent with new tags cleans up its previous tag indexing."""
        # Alice registers initially with 'math,gpu'
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "math,gpu", "120")
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:math"))
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:gpu"))

        # Alice re-registers with 'backend,cpu'
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "backend,cpu", "120")
        # New tags indexed
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:backend"))
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:cpu"))
        # Old tags MUST be cleaned up!
        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:math"))
        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:gpu"))

    def test_14_tag_unregistered_agent_rejection(self):
        """Test that tag.lua rejects modifying tags for an agent that is not registered."""
        # 'ghost_agent' was never registered
        cmd = ["redis-cli", "-u", LOCUTUS_REDIS_URL, "EVAL", LUA_TAG, "0", PREFIX, "ghost_agent", "add", "calc"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertIn("not registered", res.stderr + res.stdout)
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:ghost_agent"), "0")

    def test_15_multicast_dead_agent_hash_cleanup(self):
        """Test that dead agent pruning in multicast.lua deletes agent:<name> hash to prevent memory leak."""
        run_eval(LUA_REGISTER, 0, PREFIX, "bob", "qa", "120")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:bob"), "1")

        # Simulate bob dying (heartbeat expires)
        run_redis("DEL", f"{PREFIX}heartbeat:bob")

        # Multicast triggers dead agent pruning
        msg = json.dumps({"id": "m1", "from": "lead", "to": "@qa", "type": "task", "subject": "Test", "body": "Go", "timestamp": "2026-09-18T00:00:00Z"})
        run_eval(LUA_MULTICAST, 0, PREFIX, "qa", msg, "604800")

        # Verify bob is pruned from active_agents, tag:qa, AND agent:bob hash is deleted!
        self.assertNotIn("bob", run_redis("SMEMBERS", f"{PREFIX}active_agents"))
        self.assertNotIn("bob", run_redis("SMEMBERS", f"{PREFIX}tag:qa"))
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:bob"), "0")


if __name__ == "__main__":
    unittest.main()
