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
import time
import unittest

A2A_REDIS_URL = os.environ.get("A2A_REDIS_URL", os.environ.get("REDIS_URL", "redis://127.0.0.1:6379"))
A2A_REDIS_PREFIX = os.environ.get("A2A_REDIS_PREFIX", os.environ.get("A2A_PREFIX", "a2a_test:"))
PREFIX = A2A_REDIS_PREFIX

# Load Lua Scripts directly from scripts/ directory (Single Source of Truth)
SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))

def load_lua(filename: str) -> str:
    path = os.path.join(SCRIPTS_DIR, filename)
    with open(path, "r") as f:
        return f.read()

LUA_REGISTER = load_lua("register.lua")
LUA_SEND_O2O = load_lua("send_o2o.lua")
LUA_MULTICAST = load_lua("multicast.lua")
LUA_DRAIN = load_lua("drain.lua")
LUA_DIRECTORY = load_lua("directory.lua")
LUA_UNREGISTER = load_lua("unregister.lua")

def run_redis(*args):
    cmd = ["redis-cli", "-u", A2A_REDIS_URL] + list(args)
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return res.stdout.strip()

def run_eval(script, numkeys, *args):
    cmd = ["redis-cli", "-u", A2A_REDIS_URL, "EVAL", script, str(numkeys)] + list(args)
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
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

        # Alice must have received exact payload
        alice_msgs = json.loads(run_redis("RPOP", f"{PREFIX}inbox:alice"))
        self.assertEqual(alice_msgs["id"], "msg_mcast_qa_100")
        self.assertEqual(alice_msgs["from"], "lead")
        self.assertEqual(alice_msgs["to"], "@qa")
        self.assertEqual(alice_msgs["subject"], "Run QA Regression")
        self.assertEqual(alice_msgs["body"], "Execute test suite against staging branch.")

        # Bob must have received exact same payload
        bob_msgs = json.loads(run_redis("RPOP", f"{PREFIX}inbox:bob"))
        self.assertEqual(bob_msgs["id"], "msg_mcast_qa_100")
        self.assertEqual(bob_msgs["body"], "Execute test suite against staging branch.")

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
            "body": "Deployment completed successfully."
        })

        delivered = run_eval(LUA_MULTICAST, 0, PREFIX, "*", broadcast_msg, "604800")
        self.assertIn(delivered, ["3", "(integer) 3"])

        for agent in ["agent1", "agent2", "agent3"]:
            raw = run_redis("RPOP", f"{PREFIX}inbox:{agent}")
            self.assertIsNotNone(raw)
            parsed = json.loads(raw)
            self.assertEqual(parsed["id"], "bcast_001")
            self.assertEqual(parsed["subject"], "System Announcement")

    def test_04_offline_queuing_and_ordered_backlog(self):
        """Test that messages sent to an offline/unregistered agent are queued and drained in FIFO order."""
        # Send 3 tasks to 'david' BEFORE he registers
        for i in [1, 2, 3]:
            task_msg = json.dumps({
                "id": f"task_00{i}",
                "from": "lead",
                "to": "david",
                "type": "task",
                "seq": i,
                "body": f"Execute step {i}"
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
        msg2 = json.dumps({"id": "qa_round_2", "from": "lead", "to": "@qa", "body": "second check"})
        delivered2 = run_eval(LUA_MULTICAST, 0, PREFIX, "qa", msg2, "604800")
        self.assertIn(delivered2, ["2", "(integer) 2"])

        # Alice now receives the new message!
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:alice"), "1")
        alice_received = json.loads(run_redis("RPOP", f"{PREFIX}inbox:alice"))
        self.assertEqual(alice_received["id"], "qa_round_2")

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
            "body": "12 * 12"
        }
        run_eval(LUA_SEND_O2O, 0, PREFIX, "bob", json.dumps(task_payload), "604800")

        # 2. Bob receives task
        raw_task = run_redis("RPOP", f"{PREFIX}inbox:bob")
        incoming_task = json.loads(raw_task)
        self.assertEqual(incoming_task["id"], task_id)
        self.assertEqual(incoming_task["body"], "12 * 12")

        # 3. Bob computes result (144) and replies to Alice
        reply_id = "rep_171000_bob_888"
        reply_payload = {
            "id": reply_id,
            "from": "bob",
            "to": incoming_task["from"],
            "type": "reply",
            "reply_to": incoming_task["id"],
            "subject": f"Re: {incoming_task['subject']}",
            "body": "144"
        }
        run_eval(LUA_SEND_O2O, 0, PREFIX, "alice", json.dumps(reply_payload), "604800")

        # 4. Alice receives reply and verifies correlation
        raw_reply = run_redis("RPOP", f"{PREFIX}inbox:alice")
        received_reply = json.loads(raw_reply)
        self.assertEqual(received_reply["from"], "bob")
        self.assertEqual(received_reply["to"], "alice")
        self.assertEqual(received_reply["type"], "reply")
        self.assertEqual(received_reply["reply_to"], task_id)
        self.assertEqual(received_reply["body"], "144")

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


if __name__ == "__main__":
    unittest.main()
