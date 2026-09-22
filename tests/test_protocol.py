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
from tests.tripwire_locutus import LocutusPlugin

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
LUA_STATUS = load_lua("status.lua")
LUA_LOCK = load_lua("lock.lua")
LUA_UNLOCK = load_lua("unlock.lua")
LUA_ENQUEUE = load_lua("enqueue.lua")
LUA_SCATTER = load_lua("scatter.lua")
LUA_CLAIM = load_lua("claim.lua")
LUA_ACK = load_lua("ack.lua")
LUA_BLACKBOARD = load_lua("blackboard.lua")
LUA_FLOOR = load_lua("floor.lua")
LUA_CANCEL = load_lua("cancel.lua")
LUA_BALLOT = load_lua("ballot.lua")
LUA_LEADER = load_lua("leader.lua")
LUA_WORKFLOW = load_lua("workflow.lua")
LUA_SWEEP = load_lua("sweep.lua")

import redis
_protocol_redis_client = None

def get_protocol_redis():
    global _protocol_redis_client
    if _protocol_redis_client is None:
        _protocol_redis_client = redis.Redis.from_url(LOCUTUS_REDIS_URL, decode_responses=True, protocol=2)
    return _protocol_redis_client

def run_redis(*args):
    if os.name == "nt":
        r = get_protocol_redis()
        try:
            res = r.execute_command(*args)
        except redis.exceptions.ResponseError as e:
            msg = str(e.args[0])
            if not msg.startswith("ERR"):
                return f"(error) ERR {msg}"
            return f"(error) {msg}"
        if res is None:
            return ""
        if isinstance(res, bool):
            return "1" if res else "0"
        if isinstance(res, (list, tuple, set)):
            return "\n".join(str(item) for item in res)
        return str(res).strip()
    cmd = ["redis-cli", "-u", LOCUTUS_REDIS_URL] + list(args)
    res = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", check=True, stdin=subprocess.DEVNULL, timeout=15)
    return res.stdout.strip()

def run_eval(script, numkeys, *args):
    if os.name == "nt":
        r = get_protocol_redis()
        try:
            res = r.eval(script, numkeys, *args)
        except redis.exceptions.ResponseError as e:
            msg = str(e.args[0])
            if not msg.startswith("ERR"):
                return f"(error) ERR {msg}"
            return f"(error) {msg}"
        if res is None:
            return ""
        if isinstance(res, bool):
            return "1" if res else "0"
        if isinstance(res, (list, tuple)):
            return "\n".join(str(item) for item in res)
        return str(res).strip()
    cmd = ["redis-cli", "-u", LOCUTUS_REDIS_URL, "EVAL", script, str(numkeys)] + list(args)
    res = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", check=True, stdin=subprocess.DEVNULL, timeout=15)
    return res.stdout.strip()


import pytest

@pytest.mark.unit
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
        """Test agent registration, tag indexing, heartbeat, and directory lookup with delimiter escaping."""
        # 1. Argument validation negative controls
        res_no_prefix = run_eval(LUA_REGISTER, 0)
        self.assertIn("ERR: Missing prefix or agent name", res_no_prefix)

        res_no_agent = run_eval(LUA_REGISTER, 0, PREFIX)
        self.assertIn("ERR: Missing prefix or agent name", res_no_agent)

        res_dir_no_prefix = run_eval(LUA_DIRECTORY, 0)
        self.assertIn("ERR: Missing prefix", res_dir_no_prefix)

        # 2. Standard agent registration: 'alice'
        res = run_eval(LUA_REGISTER, 0, PREFIX, "alice", "worker,math", "120")
        self.assertEqual(res, "OK")

        # Verify heartbeat exists with correct TTL
        hb = run_redis("GET", f"{PREFIX}heartbeat:alice")
        self.assertEqual(hb, "1")
        ttl = int(run_redis("TTL", f"{PREFIX}heartbeat:alice"))
        self.assertTrue(110 <= ttl <= 120, f"Expected TTL ~120, got {ttl}")

        # Verify active roster and tag sets
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}active_agents").split())
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:math").split())
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:worker").split())

        # Verify agent metadata hash
        meta_tags = run_redis("HGET", f"{PREFIX}agent:alice", "tags")
        self.assertEqual(meta_tags, "worker,math")
        last_seen = int(run_redis("HGET", f"{PREFIX}agent:alice", "last_seen"))
        self.assertGreater(last_seen, 0)

        # 3. Special characters & delimiter escaping in agent name and activity
        special_agent = "agent:complex-name.v1#42"
        res_spec = run_eval(LUA_REGISTER, 0, PREFIX, special_agent, "tag-1, complex:tag, spaced tag", "120")
        self.assertEqual(res_spec, "OK")

        # Set complex activity containing pipe delimiters via status.lua
        res_stat = run_eval(LUA_STATUS, 0, PREFIX, special_agent, "busy", "compiling | testing | deploying", "120")
        self.assertEqual(res_stat, "OK")

        # Verify status state in Redis
        stat_state = run_redis("HGET", f"{PREFIX}agent:{special_agent}", "state")
        self.assertEqual(stat_state, "busy")
        stat_act = run_redis("HGET", f"{PREFIX}agent:{special_agent}", "activity")
        self.assertEqual(stat_act, "compiling | testing | deploying")

        # 4. Query full directory
        directory = run_eval(LUA_DIRECTORY, 0, PREFIX)
        self.assertIn("alice|1|worker,math", directory)
        self.assertIn(f"{special_agent}|1|", directory)
        self.assertIn("compiling | testing | deploying", directory)

        # 5. Tag-filtered directory queries
        dir_math = run_eval(LUA_DIRECTORY, 0, PREFIX, "math")
        self.assertIn("alice|1|worker,math", dir_math)
        self.assertNotIn(special_agent, dir_math)

        dir_spec = run_eval(LUA_DIRECTORY, 0, PREFIX, "complex:tag")
        self.assertIn(special_agent, dir_spec)
        self.assertNotIn("alice", dir_spec)

        dir_empty = run_eval(LUA_DIRECTORY, 0, PREFIX, "non_existent_tag_xyz")
        self.assertEqual(dir_empty, "")

        # 6. Negative control: unregistered agent never appears
        self.assertNotIn("ghost_agent", run_redis("SMEMBERS", f"{PREFIX}active_agents").split())
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}heartbeat:ghost_agent"), "0")
        self.assertNotIn("ghost_agent", directory)

    def test_02_multicast_multi_agent_with_content_verification(self):
        """Test multicast to specific tag and strictly verify payload content across all recipients."""
        # 1. Argument validation negative controls
        res_no_prefix = run_eval(LUA_MULTICAST, 0)
        self.assertIn("ERR: Missing prefix", res_no_prefix)

        res_no_payload = run_eval(LUA_MULTICAST, 0, PREFIX, "qa")
        self.assertIn("ERR: Missing message payload", res_no_payload)

        # 2. Setup 3 agents with overlapping tags
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "qa,frontend", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "bob", "qa,backend", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "charlie", "devops", "120")

        # 3. Construct canonical payload
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

        # Negative control: Multicast to non-existent tag delivers to 0 agents
        delivered_none = run_eval(LUA_MULTICAST, 0, PREFIX, "nonexistent_tag", msg_str, "604800")
        self.assertIn(delivered_none, ["0", "(integer) 0"])

        # 4. Send multicast to tag 'qa'
        delivered = run_eval(LUA_MULTICAST, 0, PREFIX, "qa", msg_str, "604800")
        self.assertIn(delivered, ["2", "(integer) 2"])

        # Verify inbox TTLs
        alice_ttl = int(run_redis("TTL", f"{PREFIX}inbox:alice"))
        self.assertTrue(604700 <= alice_ttl <= 604800, f"Expected TTL ~604800, got {alice_ttl}")
        bob_ttl = int(run_redis("TTL", f"{PREFIX}inbox:bob"))
        self.assertTrue(604700 <= bob_ttl <= 604800, f"Expected TTL ~604800, got {bob_ttl}")

        # 5. Negative controls on non-recipients
        # Charlie (devops) must NOT have received it
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:charlie"), "0")
        # Ghost agent inbox does not exist
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}inbox:ghost_agent"), "0")

        # 6. Retrieve raw message strings from inboxes
        alice_raw = run_redis("RPOP", f"{PREFIX}inbox:alice")
        bob_raw = run_redis("RPOP", f"{PREFIX}inbox:bob")

        # Assert byte-for-byte identity
        self.assertEqual(alice_raw, msg_str, "Alice inbox payload must match original JSON byte-for-byte")
        self.assertEqual(bob_raw, msg_str, "Bob inbox payload must match original JSON byte-for-byte")
        self.assertEqual(alice_raw, bob_raw, "Alice and Bob must receive identical raw payloads")

        # Wire envelope schema validation
        alice_wire = LocutusPlugin.validate_wire_envelope(alice_raw)
        bob_wire = LocutusPlugin.validate_wire_envelope(bob_raw)
        self.assertEqual(alice_wire["id"], "msg_mcast_qa_100")
        self.assertEqual(bob_wire["id"], "msg_mcast_qa_100")

        # Pydantic schema validation (LocutusMessage and A2AMessage)
        alice_loc = LocutusMessage.model_validate_json(alice_raw)
        bob_loc = LocutusMessage.model_validate_json(bob_raw)
        self.assertEqual(alice_loc.id, "msg_mcast_qa_100")
        self.assertEqual(bob_loc.id, "msg_mcast_qa_100")
        self.assertEqual(alice_loc.from_agent, "lead")
        self.assertEqual(alice_loc.to_agent, "@qa")
        self.assertEqual(alice_loc.subject, "Run QA Regression")
        self.assertEqual(alice_loc.body, "Execute test suite against staging branch.")
        self.assertEqual(alice_loc.tags, ["qa"])

        alice_a2a = A2AMessage.model_validate_json(alice_raw)
        bob_a2a = A2AMessage.model_validate_json(bob_raw)
        self.assertEqual(alice_a2a.id, alice_loc.id)
        self.assertEqual(bob_a2a.id, bob_loc.id)

        # Inboxes must now be cleanly drained
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:alice"), "0")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:bob"), "0")

    def test_03_broadcast_to_all_active_agents(self):
        """Test multicast with tag '*' reaches every active agent and prunes expired/dead agents with zero leaks."""
        # 1. Register 3 live agents
        run_eval(LUA_REGISTER, 0, PREFIX, "agent1", "tag1", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "agent2", "tag2", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "agent3", "tag3", "120")

        # 2. Register a dead agent and expire its heartbeat immediately
        run_eval(LUA_REGISTER, 0, PREFIX, "dead_agent", "tag_dead", "120")
        run_redis("DEL", f"{PREFIX}heartbeat:dead_agent")

        # 3. Register a 5th agent and unregister it cleanly
        run_eval(LUA_REGISTER, 0, PREFIX, "unregistered_agent", "tag_temp", "120")
        run_eval(LUA_UNREGISTER, 0, PREFIX, "unregistered_agent")

        # 4. Construct canonical broadcast payload
        broadcast_msg = json.dumps({
            "id": "bcast_001",
            "from": "ops",
            "to": "*",
            "type": "status",
            "subject": "System Announcement",
            "body": "Deployment completed successfully.",
            "timestamp": "2026-09-18T23:35:00Z"
        })

        # 5. Broadcast to '*' -> delivered count must be strictly 3 (live agents only)
        delivered = run_eval(LUA_MULTICAST, 0, PREFIX, "*", broadcast_msg, "604800")
        self.assertIn(delivered, ["3", "(integer) 3"])

        # 6. Verify each live agent received the exact broadcast payload
        for agent in ["agent1", "agent2", "agent3"]:
            self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:{agent}"), "1")
            raw = run_redis("RPOP", f"{PREFIX}inbox:{agent}")
            self.assertEqual(raw, broadcast_msg, f"Inbox payload for {agent} must match broadcast JSON byte-for-byte")

            # Schema and wire envelope validations
            wire = LocutusPlugin.validate_wire_envelope(raw)
            self.assertEqual(wire["id"], "bcast_001")
            self.assertEqual(wire["to"], "*")

            parsed_loc = LocutusMessage.model_validate_json(raw)
            self.assertEqual(parsed_loc.id, "bcast_001")
            self.assertEqual(parsed_loc.subject, "System Announcement")

            parsed_a2a = A2AMessage.model_validate_json(raw)
            self.assertEqual(parsed_a2a.id, "bcast_001")

            self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:{agent}"), "0")

        # 7. Negative control: Pruned/dead agent must receive strictly 0 messages
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:dead_agent"), "0")
        self.assertNotIn("dead_agent", run_redis("SMEMBERS", f"{PREFIX}active_agents").split())
        self.assertNotIn("dead_agent", run_redis("SMEMBERS", f"{PREFIX}tag:tag_dead").split())
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:dead_agent"), "0")

        # 8. Negative control: Unregistered agent must receive strictly 0 messages
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:unregistered_agent"), "0")

        # 9. Test '@all' broadcast alias
        broadcast_all = json.dumps({
            "id": "bcast_all_002",
            "from": "ops",
            "to": "@all",
            "type": "status",
            "subject": "Cluster Synchronized",
            "body": "All nodes in sync.",
            "timestamp": "2026-09-18T23:36:00Z"
        })
        delivered_all = run_eval(LUA_MULTICAST, 0, PREFIX, "@all", broadcast_all, "604800")
        self.assertIn(delivered_all, ["3", "(integer) 3"])
        for agent in ["agent1", "agent2", "agent3"]:
            self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:{agent}"), "1")
            raw = run_redis("RPOP", f"{PREFIX}inbox:{agent}")
            self.assertEqual(raw, broadcast_all)
            self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:{agent}"), "0")

    def test_04_offline_queuing_and_ordered_backlog(self):
        """Test that 20 messages sent to an offline/unregistered agent are queued and drained in strict FIFO order."""
        # 1. Argument validation negative controls
        res_no_prefix = run_eval(LUA_SEND_O2O, 0)
        self.assertIn("ERR: Missing prefix", res_no_prefix)

        res_no_recip = run_eval(LUA_SEND_O2O, 0, PREFIX)
        self.assertIn("ERR: Missing recipient", res_no_recip)

        res_no_payload = run_eval(LUA_SEND_O2O, 0, PREFIX, "david")
        self.assertIn("ERR: Missing message payload", res_no_payload)

        res_drain_no_prefix = run_eval(LUA_DRAIN, 0)
        self.assertIn("ERR: Missing prefix", res_drain_no_prefix)

        res_drain_no_agent = run_eval(LUA_DRAIN, 0, PREFIX)
        self.assertIn("ERR: Missing agent name", res_drain_no_agent)

        # 2. Send 20 distinct sequential messages to offline agent 'david'
        expected_msgs = []
        for i in range(1, 21):
            payload = {
                "id": f"task_seq_{i:03d}",
                "from": "lead",
                "to": "david",
                "type": "task",
                "reply_to": None,
                "subject": f"Task {i}",
                "body": f"Execute step {i:03d}",
                "timestamp": "2026-09-18T23:35:00Z"
            }
            raw_json = json.dumps(payload)
            res_send = run_eval(LUA_SEND_O2O, 0, PREFIX, "david", raw_json, "604800")
            self.assertEqual(res_send, "OK")
            expected_msgs.append(payload)

        # Verify exactly 20 messages waiting in inbox
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:david"), "20")

        # Negative control: Innocent agent 'alice' inbox remains empty
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:alice"), "0")

        # 3. David comes online and registers
        res_reg = run_eval(LUA_REGISTER, 0, PREFIX, "david", "worker", "120")
        self.assertEqual(res_reg, "OK")
        self.assertIn("david", run_redis("SMEMBERS", f"{PREFIX}active_agents").split())

        # 4. Drain batch 1: drain first 10 messages (FIFO)
        drained_raw_1 = run_eval(LUA_DRAIN, 0, PREFIX, "david", "10")
        lines_1 = [l.strip() for l in drained_raw_1.splitlines() if l.strip()]
        self.assertEqual(len(lines_1), 10, f"Expected 10 drained messages, got {len(lines_1)}")

        # Validate wire envelope, Pydantic schema, and exact FIFO ordering for batch 1
        for idx, line in enumerate(lines_1):
            wire = LocutusPlugin.validate_wire_envelope(line)
            expected = expected_msgs[idx]
            self.assertEqual(wire["id"], expected["id"])
            self.assertEqual(wire["subject"], expected["subject"])
            self.assertEqual(wire["body"], expected["body"])

            loc_msg = LocutusMessage.model_validate_json(line)
            self.assertEqual(loc_msg.id, expected["id"])
            a2a_msg = A2AMessage.model_validate_json(line)
            self.assertEqual(a2a_msg.id, expected["id"])

        # Backlog must now have exactly 10 remaining messages
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:david"), "10")

        # 5. Drain batch 2: request 15 messages (more than remaining 10)
        drained_raw_2 = run_eval(LUA_DRAIN, 0, PREFIX, "david", "15")
        lines_2 = [l.strip() for l in drained_raw_2.splitlines() if l.strip()]
        self.assertEqual(len(lines_2), 10, f"Expected remaining 10 messages, got {len(lines_2)}")

        for idx, line in enumerate(lines_2):
            wire = LocutusPlugin.validate_wire_envelope(line)
            expected = expected_msgs[10 + idx]
            self.assertEqual(wire["id"], expected["id"])
            self.assertEqual(wire["subject"], expected["subject"])
            self.assertEqual(wire["body"], expected["body"])

        # Inbox must now be completely drained
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:david"), "0")

        # 6. Drain on empty inbox returns 0 messages
        drained_empty = run_eval(LUA_DRAIN, 0, PREFIX, "david", "10")
        self.assertEqual(drained_empty, "")

    def test_05_disconnect_pruning_and_reconnect_recovery(self):
        """Test that a disconnected agent is pruned from multicasts and directory, and fully recovers state upon reconnect."""
        # 1. Initial registration: Alice and Bob
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "qa,automation", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "bob", "qa,manual", "120")
        run_eval(LUA_STATUS, 0, PREFIX, "alice", "busy", "running benchmark", "120")

        # Verify initial directory contains both active agents with state and activity
        dir_init = run_eval(LUA_DIRECTORY, 0, PREFIX)
        self.assertIn("alice|1|qa,automation|busy|running benchmark", dir_init)
        self.assertIn("bob|1|qa,manual|idle|", dir_init)

        # 2. Simulate Alice crashing / disconnect: delete her heartbeat
        run_redis("DEL", f"{PREFIX}heartbeat:alice")

        # Query directory -> directory.lua auto-prunes dead Alice
        dir_after_crash = run_eval(LUA_DIRECTORY, 0, PREFIX)
        self.assertNotIn("alice", dir_after_crash)
        self.assertIn("bob|1|qa,manual|idle|", dir_after_crash)

        # Direct Redis inspection: Alice pruned from roster and tag sets
        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}active_agents").split())
        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:qa").split())
        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:automation").split())
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:alice"), "0")

        # 3. Send multicast to 'qa' while Alice is offline
        msg1 = json.dumps({
            "id": "qa_round_1",
            "from": "lead",
            "to": "@qa",
            "type": "task",
            "subject": "Initial Check",
            "body": "first check",
            "timestamp": "2026-09-18T23:35:00Z"
        })
        delivered = run_eval(LUA_MULTICAST, 0, PREFIX, "qa", msg1, "604800")
        self.assertIn(delivered, ["1", "(integer) 1"])

        # Bob got it, Alice received 0 messages
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:bob"), "1")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:alice"), "0")
        raw_bob1 = run_redis("RPOP", f"{PREFIX}inbox:bob")
        self.assertEqual(raw_bob1, msg1)
        LocutusPlugin.validate_wire_envelope(raw_bob1)

        # 4. Alice reconnects and recovers state
        res_reconnect = run_eval(LUA_REGISTER, 0, PREFIX, "alice", "qa,automation", "120")
        self.assertEqual(res_reconnect, "OK")
        run_eval(LUA_STATUS, 0, PREFIX, "alice", "idle", "ready for tasks", "120")

        # Verify Alice is fully restored in active roster, tag sets, and directory
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}active_agents").split())
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:qa").split())
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:automation").split())

        dir_recovered = run_eval(LUA_DIRECTORY, 0, PREFIX)
        self.assertIn("alice|1|qa,automation|idle|ready for tasks", dir_recovered)
        self.assertIn("bob|1|qa,manual|idle|", dir_recovered)

        # 5. Send second multicast to 'qa' -> both Alice and Bob receive it
        msg2 = json.dumps({
            "id": "qa_round_2",
            "from": "lead",
            "to": "@qa",
            "type": "task",
            "subject": "Second Check",
            "body": "second check",
            "timestamp": "2026-09-18T23:36:00Z"
        })
        delivered2 = run_eval(LUA_MULTICAST, 0, PREFIX, "qa", msg2, "604800")
        self.assertIn(delivered2, ["2", "(integer) 2"])

        # Both receive exact message
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:alice"), "1")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:bob"), "1")

        alice_raw2 = run_redis("RPOP", f"{PREFIX}inbox:alice")
        bob_raw2 = run_redis("RPOP", f"{PREFIX}inbox:bob")
        self.assertEqual(alice_raw2, msg2)
        self.assertEqual(bob_raw2, msg2)

        # Wire and schema validations
        LocutusPlugin.validate_wire_envelope(alice_raw2)
        LocutusPlugin.validate_wire_envelope(bob_raw2)
        alice_msg2 = LocutusMessage.model_validate_json(alice_raw2)
        bob_msg2 = LocutusMessage.model_validate_json(bob_raw2)
        self.assertEqual(alice_msg2.id, "qa_round_2")
        self.assertEqual(bob_msg2.id, "qa_round_2")

    def test_06_roundtrip_request_reply_threading(self):
        """Test full round-trip request/reply conversation with multi-hop reply_to thread correlation and schema validation."""
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "client", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "bob", "math-service", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "charlie", "observer", "120")

        # 1. Hop 1: Alice sends initial task to Bob
        task_1_id = "req_171000_alice_001"
        task_1_payload = {
            "id": task_1_id,
            "from": "alice",
            "to": "bob",
            "type": "task",
            "reply_to": None,
            "subject": "Compute Power",
            "body": "2^8",
            "timestamp": "2026-09-18T23:35:00Z"
        }
        res_send1 = run_eval(LUA_SEND_O2O, 0, PREFIX, "bob", json.dumps(task_1_payload), "604800")
        self.assertEqual(res_send1, "OK")

        # Verify inbox routing
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:bob"), "1")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:alice"), "0")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:charlie"), "0")

        # Bob receives task 1
        raw_task_1 = run_redis("RPOP", f"{PREFIX}inbox:bob")
        self.assertEqual(raw_task_1, json.dumps(task_1_payload))
        LocutusPlugin.validate_wire_envelope(raw_task_1)
        inc_task_1 = LocutusMessage.model_validate_json(raw_task_1)
        self.assertEqual(inc_task_1.id, task_1_id)
        self.assertIsNone(inc_task_1.reply_to)
        self.assertEqual(inc_task_1.body, "2^8")

        # 2. Hop 2: Bob computes (256) and replies to Alice with reply_to referencing task_1_id
        reply_1_id = "rep_171000_bob_001"
        reply_1_payload = {
            "id": reply_1_id,
            "from": "bob",
            "to": inc_task_1.from_agent,
            "type": "reply",
            "reply_to": inc_task_1.id,
            "subject": f"Re: {inc_task_1.subject}",
            "body": "256",
            "timestamp": "2026-09-18T23:35:05Z"
        }
        res_rep1 = run_eval(LUA_SEND_O2O, 0, PREFIX, "alice", json.dumps(reply_1_payload), "604800")
        self.assertEqual(res_rep1, "OK")

        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:alice"), "1")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:bob"), "0")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:charlie"), "0")

        # Alice receives reply 1 and verifies thread correlation
        raw_rep_1 = run_redis("RPOP", f"{PREFIX}inbox:alice")
        LocutusPlugin.validate_wire_envelope(raw_rep_1)
        inc_rep_1 = LocutusMessage.model_validate_json(raw_rep_1)
        self.assertEqual(inc_rep_1.id, reply_1_id)
        self.assertEqual(inc_rep_1.reply_to, task_1_id, "Hop 2 reply_to must correlate with Hop 1 task ID")
        self.assertEqual(inc_rep_1.body, "256")

        # 3. Hop 3: Alice sends follow-up task referencing reply_1_id
        task_2_id = "req_171000_alice_002"
        task_2_payload = {
            "id": task_2_id,
            "from": "alice",
            "to": "bob",
            "type": "task",
            "reply_to": inc_rep_1.id,
            "subject": "Compute Sqrt",
            "body": "sqrt(256)",
            "timestamp": "2026-09-18T23:35:10Z"
        }
        res_send2 = run_eval(LUA_SEND_O2O, 0, PREFIX, "bob", json.dumps(task_2_payload), "604800")
        self.assertEqual(res_send2, "OK")

        raw_task_2 = run_redis("RPOP", f"{PREFIX}inbox:bob")
        LocutusPlugin.validate_wire_envelope(raw_task_2)
        inc_task_2 = LocutusMessage.model_validate_json(raw_task_2)
        self.assertEqual(inc_task_2.id, task_2_id)
        self.assertEqual(inc_task_2.reply_to, reply_1_id, "Hop 3 task reply_to must correlate with Hop 2 reply ID")
        self.assertEqual(inc_task_2.body, "sqrt(256)")

        # 4. Hop 4: Bob computes (16) and sends final reply referencing task_2_id
        reply_2_id = "rep_171000_bob_002"
        reply_2_payload = {
            "id": reply_2_id,
            "from": "bob",
            "to": inc_task_2.from_agent,
            "type": "reply",
            "reply_to": inc_task_2.id,
            "subject": f"Re: {inc_task_2.subject}",
            "body": "16",
            "timestamp": "2026-09-18T23:35:15Z"
        }
        res_rep2 = run_eval(LUA_SEND_O2O, 0, PREFIX, "alice", json.dumps(reply_2_payload), "604800")
        self.assertEqual(res_rep2, "OK")

        raw_rep_2 = run_redis("RPOP", f"{PREFIX}inbox:alice")
        LocutusPlugin.validate_wire_envelope(raw_rep_2)
        inc_rep_2 = LocutusMessage.model_validate_json(raw_rep_2)
        self.assertEqual(inc_rep_2.id, reply_2_id)
        self.assertEqual(inc_rep_2.reply_to, task_2_id, "Hop 4 reply_to must correlate with Hop 3 task ID")
        self.assertEqual(inc_rep_2.body, "16")

        # 5. Verify all inboxes are cleanly empty
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:alice"), "0")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:bob"), "0")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:charlie"), "0")

    def test_07_inbox_ttl_hygiene(self):
        """Test that sending a message sets an expiration TTL on the inbox key and keys vanish upon expiry."""
        inbox_key = f"{PREFIX}inbox:ephemeral_user"
        msg = json.dumps({"id": "ttl_test", "from": "a", "to": "ephemeral_user", "body": "hi", "type": "task", "timestamp": "2026-09-18T23:35:00Z"})

        # 1. Send with 1 second TTL
        run_eval(LUA_SEND_O2O, 0, PREFIX, "ephemeral_user", msg, "1")

        # Immediately assert key exists and TTL is <= 1
        self.assertEqual(run_redis("EXISTS", inbox_key), "1")
        self.assertEqual(run_redis("LLEN", inbox_key), "1")
        ttl = int(run_redis("TTL", inbox_key))
        self.assertTrue(0 <= ttl <= 1, f"Expected initial TTL <= 1, got {ttl}")

        # 2. Wait for TTL to naturally expire
        time.sleep(1.15)

        # 3. Assert key has vanished from Redis keyspace (TTL auto-cleanup)
        self.assertEqual(run_redis("EXISTS", inbox_key), "0", "Inbox key must vanish from Redis after TTL expiration")
        self.assertEqual(run_redis("TTL", inbox_key), "-2", "TTL on expired non-existent key must return -2")
        self.assertEqual(run_redis("LLEN", inbox_key), "0")

        # 4. Test sliding window TTL refresh
        refresh_inbox = f"{PREFIX}inbox:refresh_user"
        run_eval(LUA_SEND_O2O, 0, PREFIX, "refresh_user", msg, "2")
        self.assertEqual(run_redis("EXISTS", refresh_inbox), "1")

        time.sleep(1.0)
        # Refresh with 5-second TTL
        run_eval(LUA_SEND_O2O, 0, PREFIX, "refresh_user", msg, "5")
        refreshed_ttl = int(run_redis("TTL", refresh_inbox))
        self.assertTrue(3 <= refreshed_ttl <= 5, f"Expected refreshed TTL ~5s, got {refreshed_ttl}")

        # Wait 1.5s (past initial 2s mark) -> key still alive due to refresh
        time.sleep(1.5)
        self.assertEqual(run_redis("EXISTS", refresh_inbox), "1")
        self.assertEqual(run_redis("LLEN", refresh_inbox), "2")
        run_redis("DEL", refresh_inbox)

    def test_08_unregister_and_cleanup(self):
        """Test graceful logout and complete cleanup of all keys, inboxes, tags, and metadata hashes."""
        # 1. Argument validation negative controls
        res_no_prefix = run_eval(LUA_UNREGISTER, 0)
        self.assertIn("ERR: Missing prefix", res_no_prefix)

        res_no_name = run_eval(LUA_UNREGISTER, 0, PREFIX)
        self.assertIn("ERR: Missing agent name", res_no_name)

        # 2. Register Alice with multiple tags and populate inbox and status
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "math,worker,qa", "120")
        run_eval(LUA_STATUS, 0, PREFIX, "alice", "busy", "processing data", "120")

        # Send 2 messages into Alice's inbox
        msg1 = json.dumps({"id": "m1", "from": "lead", "to": "alice", "body": "msg 1"})
        msg2 = json.dumps({"id": "m2", "from": "lead", "to": "alice", "body": "msg 2"})
        run_eval(LUA_SEND_O2O, 0, PREFIX, "alice", msg1, "604800")
        run_eval(LUA_SEND_O2O, 0, PREFIX, "alice", msg2, "604800")

        # Verify active presence in Redis
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}active_agents").split())
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:math").split())
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:worker").split())
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:qa").split())
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}heartbeat:alice"), "1")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:alice"), "1")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}inbox:alice"), "1")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:alice"), "2")

        # 3. Graceful unregister
        res_unreg = run_eval(LUA_UNREGISTER, 0, PREFIX, "alice")
        self.assertEqual(res_unreg, "OK")

        # 4. Strict assertions: Complete keyspace cleanup
        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}active_agents").split())
        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:math").split())
        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:worker").split())
        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:qa").split())
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}heartbeat:alice"), "0")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:alice"), "0")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}inbox:alice"), "0", "Inbox key must be purged upon unregister")

        # 5. Directory verification
        directory = run_eval(LUA_DIRECTORY, 0, PREFIX)
        self.assertNotIn("alice", directory)

        # 6. Idempotency test: Re-unregistering a non-existent agent returns OK cleanly
        res_idempotent = run_eval(LUA_UNREGISTER, 0, PREFIX, "alice")
        self.assertEqual(res_idempotent, "OK")

    def test_09_multi_tag_and_filtering_with_project_isolation(self):
        """Test multi-tag AND-filtering via SINTER ensuring cross-project collision isolation and role targeting."""
        # Setup 6 agents across two separate projects with identical role tags
        # Project 'alpha'
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "alpha,backend,python", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "bob", "alpha,frontend,react", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "charlie", "alpha,backend,golang", "120")
        # Project 'beta' (identical role sub-tags)
        run_eval(LUA_REGISTER, 0, PREFIX, "dave", "beta,backend,python", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "eve", "beta,frontend,react", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "frank", "beta,backend,golang", "120")

        # 1. Multicast within project 'alpha' to 'backend' (target: "alpha,backend")
        # Must reach alice and charlie, but NOT bob (frontend) and NOT dave/frank (project beta backend)
        task_alpha_msg = json.dumps({
            "id": "msg_alpha_backend_001",
            "from": "lead_alpha",
            "to": "@alpha,backend",
            "type": "task",
            "tags": ["alpha", "backend"],
            "subject": "Alpha Backend Sync",
            "body": "Check API schema.",
            "timestamp": "2026-09-18T23:40:00Z"
        })
        delivered_alpha = run_eval(LUA_MULTICAST, 0, PREFIX, "alpha,backend", task_alpha_msg, "604800")
        self.assertIn(delivered_alpha, ["2", "(integer) 2"])

        # Negative controls: Project beta backend agents and all frontend agents must receive strictly 0
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:bob"), "0")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:dave"), "0")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:eve"), "0")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:frank"), "0")

        # Alice and Charlie received exact message
        raw_alice = run_redis("RPOP", f"{PREFIX}inbox:alice")
        raw_charlie = run_redis("RPOP", f"{PREFIX}inbox:charlie")
        self.assertEqual(raw_alice, task_alpha_msg)
        self.assertEqual(raw_charlie, task_alpha_msg)
        LocutusPlugin.validate_wire_envelope(raw_alice)
        LocutusPlugin.validate_wire_envelope(raw_charlie)

        # 2. Multicast within project 'beta' to 'backend' (target: "beta,backend")
        # Must reach dave and frank, but NOT alice/charlie (project alpha backend)
        task_beta_msg = json.dumps({
            "id": "msg_beta_backend_001",
            "from": "lead_beta",
            "to": "@beta,backend",
            "type": "task",
            "tags": ["beta", "backend"],
            "subject": "Beta Backend Sync",
            "body": "Run migrations.",
            "timestamp": "2026-09-18T23:40:30Z"
        })
        delivered_beta = run_eval(LUA_MULTICAST, 0, PREFIX, "beta,backend", task_beta_msg, "604800")
        self.assertIn(delivered_beta, ["2", "(integer) 2"])

        # Negative controls: Alpha backend agents must receive 0 messages
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:alice"), "0")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:charlie"), "0")

        raw_dave = run_redis("RPOP", f"{PREFIX}inbox:dave")
        raw_frank = run_redis("RPOP", f"{PREFIX}inbox:frank")
        self.assertEqual(raw_dave, task_beta_msg)
        self.assertEqual(raw_frank, task_beta_msg)

        # 3. Targeted multicast with 3 tags: "alpha,backend,python"
        # Only Alice has all 3 tags (Dave has beta,backend,python)
        py_msg = json.dumps({
            "id": "msg_alpha_py_001",
            "from": "lead_alpha",
            "to": "@alpha,backend,python",
            "type": "task",
            "subject": "Python Specialist Task",
            "body": "Refactor async handler.",
            "timestamp": "2026-09-18T23:41:00Z"
        })
        delivered3 = run_eval(LUA_MULTICAST, 0, PREFIX, "alpha,backend,python", py_msg, "604800")
        self.assertIn(delivered3, ["1", "(integer) 1"])
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:charlie"), "0")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:dave"), "0")
        alice_py = LocutusMessage.model_validate_json(run_redis("RPOP", f"{PREFIX}inbox:alice"))
        self.assertEqual(alice_py.id, "msg_alpha_py_001")
        self.assertEqual(alice_py.body, "Refactor async handler.")

        # 4. Cross-project intersection: "backend,python" (no project filter)
        # Should reach both Alice (alpha) and Dave (beta)
        cross_msg = json.dumps({
            "id": "msg_cross_py_001",
            "from": "architect",
            "to": "@backend,python",
            "type": "task",
            "subject": "Global Python Standard",
            "body": "Upgrade to Python 3.14.",
            "timestamp": "2026-09-18T23:42:00Z"
        })
        delivered_cross = run_eval(LUA_MULTICAST, 0, PREFIX, "backend,python", cross_msg, "604800")
        self.assertIn(delivered_cross, ["2", "(integer) 2"])
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:bob"), "0")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:charlie"), "0")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:eve"), "0")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:frank"), "0")
        self.assertEqual(run_redis("RPOP", f"{PREFIX}inbox:alice"), cross_msg)
        self.assertEqual(run_redis("RPOP", f"{PREFIX}inbox:dave"), cross_msg)

    def test_10_dynamic_tag_management(self):
        """Test tag.lua: adding, removing, and setting tags dynamically, asserting reverse index set shrinking and inbox preservation."""
        # 1. Negative controls: argument validation and unregistered agent rejection
        res_no_prefix = run_eval(LUA_TAG, 0)
        self.assertIn("ERR: Missing prefix", res_no_prefix)

        res_no_name = run_eval(LUA_TAG, 0, PREFIX)
        self.assertIn("ERR: Missing agent name", res_no_name)

        res_bad_action = run_eval(LUA_TAG, 0, PREFIX, "alice", "bogus_action", "tag1")
        self.assertIn("ERR: Unknown action 'bogus_action'", res_bad_action)

        res_unregistered = run_eval(LUA_TAG, 0, PREFIX, "ghost_agent", "add", "tag1")
        self.assertIn("ERR agent 'ghost_agent' is not registered", res_unregistered)

        # 2. Register Alice with 'alpha,worker'
        res_reg = run_eval(LUA_REGISTER, 0, PREFIX, "alice", "alpha,worker", "120")
        self.assertEqual(res_reg, "OK")
        self.assertEqual(run_redis("SCARD", f"{PREFIX}tag:alpha"), "1")
        self.assertEqual(run_redis("SCARD", f"{PREFIX}tag:worker"), "1")

        # Queue a pending task message for Alice BEFORE changing tags
        pending_msg = json.dumps({
            "id": "pending_001",
            "from": "lead",
            "to": "alice",
            "type": "task",
            "reply_to": None,
            "subject": "Pending Work",
            "body": "Do not lose me during re-tagging!",
            "timestamp": "2026-09-18T23:42:00Z"
        })
        run_eval(LUA_SEND_O2O, 0, PREFIX, "alice", pending_msg, "604800")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:alice"), "1")

        # 3. Action 'add': Add tags 'gpu,ml'
        res_add = run_eval(LUA_TAG, 0, PREFIX, "alice", "add", "gpu,ml")
        self.assertEqual(res_add, "alpha,gpu,ml,worker")
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:gpu"))
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:ml"))
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:alpha"))
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:worker"))
        self.assertEqual(run_redis("SCARD", f"{PREFIX}tag:gpu"), "1")
        self.assertEqual(run_redis("SCARD", f"{PREFIX}tag:ml"), "1")

        # 4. Action 'remove': Remove tag 'worker' -> Assert tag:worker shrinks to 0
        res_rem = run_eval(LUA_TAG, 0, PREFIX, "alice", "remove", "worker")
        self.assertEqual(res_rem, "alpha,gpu,ml")
        self.assertEqual(run_redis("SCARD", f"{PREFIX}tag:worker"), "0", "tag:worker reverse index set must shrink to 0")
        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:worker"))
        self.assertEqual(run_redis("HGET", f"{PREFIX}agent:alice", "tags"), "alpha,gpu,ml")

        # 5. Action 'set': Overwrite entire tag set with 'alpha,specialist'
        res_set = run_eval(LUA_TAG, 0, PREFIX, "alice", "set", "alpha,specialist")
        self.assertEqual(res_set, "alpha,specialist")
        # Old tags gpu and ml must shrink to 0
        self.assertEqual(run_redis("SCARD", f"{PREFIX}tag:gpu"), "0", "tag:gpu must shrink to 0 upon set")
        self.assertEqual(run_redis("SCARD", f"{PREFIX}tag:ml"), "0", "tag:ml must shrink to 0 upon set")
        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:gpu"))
        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:ml"))

        # New tags must have Alice
        self.assertEqual(run_redis("SCARD", f"{PREFIX}tag:specialist"), "1")
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:specialist"))
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:alpha"))
        self.assertEqual(run_redis("HGET", f"{PREFIX}agent:alice", "tags"), "alpha,specialist")

        # 6. Crucial: verify Alice's pending message was NOT dropped or disturbed!
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:alice"), "1")
        raw_preserved = run_redis("RPOP", f"{PREFIX}inbox:alice")
        self.assertEqual(raw_preserved, pending_msg)
        LocutusPlugin.validate_wire_envelope(raw_preserved)
        preserved = LocutusMessage.model_validate_json(raw_preserved)
        self.assertEqual(preserved.id, "pending_001")
        self.assertEqual(preserved.body, "Do not lose me during re-tagging!")

    def test_11_team_directory_project_filtering(self):
        """Test directory.lua: listing roster filtered by project vs cluster-wide discovery via '*', '@all', and empty filter."""
        # 1. Setup agents across 3 distinct projects
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "team_alpha,lead", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "bob", "team_alpha,dev", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "charlie", "team_beta,dev", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "dave", "team_beta,qa", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "eve", "team_gamma,admin", "120")

        # 2. Filter by project 'team_alpha' -> strictly alice and bob
        dir_alpha = run_eval(LUA_DIRECTORY, 0, PREFIX, "team_alpha")
        alpha_lines = [l.strip() for l in dir_alpha.splitlines() if l.strip()]
        self.assertEqual(len(alpha_lines), 2, f"Expected 2 agents in team_alpha, got {len(alpha_lines)}")
        self.assertIn("alice|1|team_alpha,lead", dir_alpha)
        self.assertIn("bob|1|team_alpha,dev", dir_alpha)
        self.assertNotIn("charlie", dir_alpha)
        self.assertNotIn("dave", dir_alpha)
        self.assertNotIn("eve", dir_alpha)

        # 3. Filter by project 'team_beta' -> strictly charlie and dave
        dir_beta = run_eval(LUA_DIRECTORY, 0, PREFIX, "team_beta")
        beta_lines = [l.strip() for l in dir_beta.splitlines() if l.strip()]
        self.assertEqual(len(beta_lines), 2, f"Expected 2 agents in team_beta, got {len(beta_lines)}")
        self.assertIn("charlie|1|team_beta,dev", dir_beta)
        self.assertIn("dave|1|team_beta,qa", dir_beta)
        self.assertNotIn("alice", dir_beta)
        self.assertNotIn("bob", dir_beta)
        self.assertNotIn("eve", dir_beta)

        # 4. Filter by project 'team_gamma' -> strictly eve
        dir_gamma = run_eval(LUA_DIRECTORY, 0, PREFIX, "team_gamma")
        gamma_lines = [l.strip() for l in dir_gamma.splitlines() if l.strip()]
        self.assertEqual(len(gamma_lines), 1)
        self.assertIn("eve|1|team_gamma,admin", dir_gamma)

        # 5. Cluster-wide discovery via '*' -> discovers all 5 agents across all projects
        dir_star = run_eval(LUA_DIRECTORY, 0, PREFIX, "*")
        star_lines = [l.strip() for l in dir_star.splitlines() if l.strip()]
        self.assertEqual(len(star_lines), 5, f"Expected 5 agents across all teams, got {len(star_lines)}")
        for ag in ["alice", "bob", "charlie", "dave", "eve"]:
            self.assertIn(ag, dir_star)

        # 6. Cluster-wide discovery via '@all' alias
        dir_all = run_eval(LUA_DIRECTORY, 0, PREFIX, "@all")
        all_lines = [l.strip() for l in dir_all.splitlines() if l.strip()]
        self.assertEqual(len(all_lines), 5)
        for ag in ["alice", "bob", "charlie", "dave", "eve"]:
            self.assertIn(ag, dir_all)

        # 7. Cluster-wide discovery via empty filter string
        dir_empty = run_eval(LUA_DIRECTORY, 0, PREFIX, "")
        empty_lines = [l.strip() for l in dir_empty.splitlines() if l.strip()]
        self.assertEqual(len(empty_lines), 5)

        # 8. Negative control: non-existent project tag returns empty result
        dir_nonexistent = run_eval(LUA_DIRECTORY, 0, PREFIX, "team_nonexistent")
        self.assertEqual(dir_nonexistent, "")

    def test_12_structured_field_invocation_and_cjson_encoding(self):
        """Test sending messages via individual field arguments with server-side cjson encoding across complex Unicode and escaped strings."""
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "locutus,worker", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "bob", "locutus,qa", "120")

        # 1. Send O2O with field arguments (no JSON quoting in shell!)
        ts = "2026-09-18T23:50:00Z"
        res = run_eval(
            LUA_SEND_O2O, 0,
            PREFIX, "bob", "task", "alice", "Run Tests", "pytest tests/", "locutus", "", "", ts
        )
        self.assertEqual(res, "OK")

        # Verify Bob received perfectly formed JSON validated by wire envelope & Pydantic
        raw_msg = run_redis("RPOP", f"{PREFIX}inbox:bob")
        LocutusPlugin.validate_wire_envelope(raw_msg)
        msg = LocutusMessage.model_validate_json(raw_msg)
        self.assertEqual(msg.from_agent, "alice")
        self.assertEqual(msg.to_agent, "bob")
        self.assertEqual(msg.type, "task")
        self.assertEqual(msg.subject, "Run Tests")
        self.assertEqual(msg.body, "pytest tests/")
        self.assertEqual(msg.tags, ["locutus"])
        self.assertEqual(msg.timestamp, ts)
        self.assertIsNone(msg.reply_to)

        # 2. Extreme Unicode, quotes, escaped characters, tabs, and newlines in structured fields
        complex_subject = "Complex: 🚀 日本語 'single' \"double\" \\ backslash / slash"
        complex_body = "Line 1: Hello World!\nLine 2: Tab\there.\nLine 3: <html><script>alert('xss')</script></html>\nLine 4: Emojis 🔥✨🎯"
        custom_id = "custom_id_unicode_999"

        res_complex = run_eval(
            LUA_SEND_O2O, 0,
            PREFIX, "bob", "query", "alice", complex_subject, complex_body, "utf8,unicode", "orig_req_123", custom_id, ts
        )
        self.assertEqual(res_complex, "OK")

        raw_complex = run_redis("RPOP", f"{PREFIX}inbox:bob")
        LocutusPlugin.validate_wire_envelope(raw_complex)
        parsed_complex = LocutusMessage.model_validate_json(raw_complex)
        self.assertEqual(parsed_complex.id, custom_id)
        self.assertEqual(parsed_complex.from_agent, "alice")
        self.assertEqual(parsed_complex.to_agent, "bob")
        self.assertEqual(parsed_complex.type, "query")
        self.assertEqual(parsed_complex.reply_to, "orig_req_123")
        self.assertEqual(parsed_complex.subject, complex_subject)
        self.assertEqual(parsed_complex.body, complex_body)
        self.assertEqual(parsed_complex.tags, ["utf8", "unicode"])

        # 3. Multicast with complex field arguments
        mcast_subject = "Fanout: 🌐 Global Sync"
        mcast_body = "All systems operational with \"quoted parameters\" and 'single'."
        res_mcast = run_eval(
            LUA_MULTICAST, 0,
            PREFIX, "locutus", "status", "lead", mcast_subject, mcast_body, "locutus", "", "", ts
        )
        self.assertIn(res_mcast, ["2", "(integer) 2"])

        # Both Alice and Bob received it with exact content
        raw_alice_m = run_redis("RPOP", f"{PREFIX}inbox:alice")
        raw_bob_m = run_redis("RPOP", f"{PREFIX}inbox:bob")
        LocutusPlugin.validate_wire_envelope(raw_alice_m)
        LocutusPlugin.validate_wire_envelope(raw_bob_m)

        alice_mcast = LocutusMessage.model_validate_json(raw_alice_m)
        bob_mcast = LocutusMessage.model_validate_json(raw_bob_m)
        self.assertEqual(alice_mcast.subject, mcast_subject)
        self.assertEqual(alice_mcast.body, mcast_body)
        self.assertEqual(bob_mcast.subject, mcast_subject)
        self.assertEqual(bob_mcast.body, mcast_body)

    def test_13_register_tag_cleanup_on_reregistration(self):
        """Test that re-registering an agent with new tags cleans up previous tag indexing, shrinks shared sets, and updates multicast routing."""
        # 1. Step 1: Alice registers with 'math,gpu,python', Bob registers with 'gpu,docker'
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "math,gpu,python", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "bob", "gpu,docker", "120")

        self.assertEqual(run_redis("SCARD", f"{PREFIX}tag:math"), "1")
        self.assertEqual(run_redis("SCARD", f"{PREFIX}tag:gpu"), "2")
        self.assertEqual(run_redis("SCARD", f"{PREFIX}tag:python"), "1")
        self.assertEqual(run_redis("SCARD", f"{PREFIX}tag:docker"), "1")
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:math").split())
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:gpu").split())
        self.assertIn("bob", run_redis("SMEMBERS", f"{PREFIX}tag:gpu").split())

        # 2. Step 2: Alice re-registers with 'backend,cpu'
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "backend,cpu", "120")

        # New tags indexed
        self.assertEqual(run_redis("SCARD", f"{PREFIX}tag:backend"), "1")
        self.assertEqual(run_redis("SCARD", f"{PREFIX}tag:cpu"), "1")
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:backend").split())
        self.assertIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:cpu").split())

        # Old solo tags cleared to 0
        self.assertEqual(run_redis("SCARD", f"{PREFIX}tag:math"), "0", "tag:math must shrink to 0")
        self.assertEqual(run_redis("SCARD", f"{PREFIX}tag:python"), "0", "tag:python must shrink to 0")
        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:math").split())
        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:python").split())

        # Shared tag 'gpu' must shrink from 2 to 1 (Bob retained, Alice removed)
        self.assertEqual(run_redis("SCARD", f"{PREFIX}tag:gpu"), "1", "tag:gpu must shrink to 1")
        self.assertNotIn("alice", run_redis("SMEMBERS", f"{PREFIX}tag:gpu").split())
        self.assertIn("bob", run_redis("SMEMBERS", f"{PREFIX}tag:gpu").split())

        # Metadata hash updated
        self.assertEqual(run_redis("HGET", f"{PREFIX}agent:alice", "tags"), "backend,cpu")

        # 3. Multicast verification: routing reflects new tag set
        ts = "2026-09-18T23:55:00Z"
        msg_math = json.dumps({"id": "m_math", "from": "lead", "to": "@math", "type": "task", "body": "math task", "timestamp": ts})
        del_math = run_eval(LUA_MULTICAST, 0, PREFIX, "math", msg_math, "604800")
        self.assertIn(del_math, ["0", "(integer) 0"])

        msg_gpu = json.dumps({"id": "m_gpu", "from": "lead", "to": "@gpu", "type": "task", "body": "gpu task", "timestamp": ts})
        del_gpu = run_eval(LUA_MULTICAST, 0, PREFIX, "gpu", msg_gpu, "604800")
        self.assertIn(del_gpu, ["1", "(integer) 1"])
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:alice"), "0")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:bob"), "1")
        run_redis("DEL", f"{PREFIX}inbox:bob")

        msg_backend = json.dumps({"id": "m_backend", "from": "lead", "to": "@backend", "type": "task", "body": "backend task", "timestamp": ts})
        del_backend = run_eval(LUA_MULTICAST, 0, PREFIX, "backend", msg_backend, "604800")
        self.assertIn(del_backend, ["1", "(integer) 1"])
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:alice"), "1")
        raw_alice = run_redis("RPOP", f"{PREFIX}inbox:alice")
        LocutusPlugin.validate_wire_envelope(raw_alice)

        # 4. Directory reflects updated tags
        directory = run_eval(LUA_DIRECTORY, 0, PREFIX)
        self.assertIn("alice|1|backend,cpu", directory)
        self.assertNotIn("math", directory)

    def test_14_tag_unregistered_agent_rejection(self):
        """Test that tag.lua rejects all tag mutations for unregistered agents with zero keyspace side-effects."""
        # 1. Take keyspace snapshot before attempting mutations
        initial_keys = set(run_redis("KEYS", f"{PREFIX}*").split())

        # 2. Test 'add' action on unregistered agent
        res_add = run_eval(LUA_TAG, 0, PREFIX, "ghost_agent", "add", "calc,ml")
        self.assertIn("ERR agent 'ghost_agent' is not registered", res_add)

        # 3. Test 'remove' action on unregistered agent
        res_rem = run_eval(LUA_TAG, 0, PREFIX, "ghost_agent", "remove", "calc")
        self.assertIn("ERR agent 'ghost_agent' is not registered", res_rem)

        # 4. Test 'set' action on unregistered agent
        res_set = run_eval(LUA_TAG, 0, PREFIX, "ghost_agent", "set", "calc,solo")
        self.assertIn("ERR agent 'ghost_agent' is not registered", res_set)

        # 5. Strict negative controls: verify NO keys or set entries were created
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:ghost_agent"), "0")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}heartbeat:ghost_agent"), "0")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}inbox:ghost_agent"), "0")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}tag:calc"), "0")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}tag:ml"), "0")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}tag:solo"), "0")
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}active_agents", "ghost_agent"), "0")

        # 6. Absolute immutability: Keyspace after failed operations matches snapshot exactly
        post_keys = set(run_redis("KEYS", f"{PREFIX}*").split())
        self.assertEqual(post_keys, initial_keys, "Attempted tag operations on unregistered agent must create zero keys")

    def test_15_multicast_dead_agent_hash_cleanup(self):
        """Test that dead agent pruning in multicast.lua deletes agent:<name> hash while living agents receive message."""
        # Setup: Register bob (qa,tester), carol (qa,lead), and dan (dev)
        run_eval(LUA_REGISTER, 0, PREFIX, "bob", "qa,tester", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "carol", "qa,lead", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "dan", "dev", "120")

        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:bob"), "1")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:carol"), "1")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:dan"), "1")
        self.assertIn("bob", run_redis("SMEMBERS", f"{PREFIX}tag:qa"))
        self.assertIn("carol", run_redis("SMEMBERS", f"{PREFIX}tag:qa"))
        self.assertIn("bob", run_redis("SMEMBERS", f"{PREFIX}tag:tester"))

        # Simulate bob dying (heartbeat expires)
        run_redis("DEL", f"{PREFIX}heartbeat:bob")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}heartbeat:bob"), "0")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}heartbeat:carol"), "1")

        # Multicast targeting @qa: should prune dead bob AND deliver to living carol in single operation
        msg_dict = {
            "id": "msg_qa_01",
            "from": "dispatch",
            "to": "@qa",
            "type": "task",
            "subject": "Regression Run",
            "body": "Execute test matrix",
            "timestamp": "2026-09-18T12:00:00Z"
        }
        raw_payload = json.dumps(msg_dict)
        delivered_count = run_eval(LUA_MULTICAST, 0, PREFIX, "qa", raw_payload, "604800")
        # Exactly 1 agent (carol) was alive to receive it
        self.assertEqual(int(delivered_count), 1)

        # Verify bob is pruned from active_agents, all tag sets, AND agent:bob hash is deleted
        self.assertNotIn("bob", run_redis("SMEMBERS", f"{PREFIX}active_agents"))
        self.assertNotIn("bob", run_redis("SMEMBERS", f"{PREFIX}tag:qa"))
        self.assertNotIn("bob", run_redis("SMEMBERS", f"{PREFIX}tag:tester"))
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:bob"), "0")
        # Dead bob must NOT have received any message
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:bob"), "0")

        # Verify living carol is retained and received the multicast message
        self.assertIn("carol", run_redis("SMEMBERS", f"{PREFIX}active_agents"))
        self.assertIn("carol", run_redis("SMEMBERS", f"{PREFIX}tag:qa"))
        self.assertIn("carol", run_redis("SMEMBERS", f"{PREFIX}tag:lead"))
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:carol"), "1")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:carol"), "1")

        # Drain carol's inbox and validate wire envelope
        received_carol = run_redis("RPOP", f"{PREFIX}inbox:carol")
        self.assertEqual(received_carol, raw_payload)
        LocutusPlugin.validate_wire_envelope(received_carol)
        model_msg = LocutusMessage.model_validate_json(received_carol)
        self.assertEqual(model_msg.id, "msg_qa_01")
        self.assertEqual(model_msg.from_agent, "dispatch")
        self.assertEqual(model_msg.body, "Execute test matrix")
        a2a_msg = A2AMessage.model_validate_json(received_carol)
        self.assertEqual(a2a_msg.from_agent, "dispatch")
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:carol"), "0")

        # Negative control: bystander dan (under dev tag) must NOT receive the message
        self.assertIn("dan", run_redis("SMEMBERS", f"{PREFIX}active_agents"))
        self.assertNotIn("dan", run_redis("SMEMBERS", f"{PREFIX}tag:qa"))
        self.assertEqual(run_redis("LLEN", f"{PREFIX}inbox:dan"), "0")

        # Negative control: Multicast to non-existent tag returns 0 delivered
        res_nonexistent = run_eval(LUA_MULTICAST, 0, PREFIX, "nonexistent_tag", raw_payload, "604800")
        self.assertEqual(int(res_nonexistent), 0)

    def test_16_status_lua(self):
        """Test status.lua updates state, activity, refreshes heartbeat TTL, and restores active_agents membership."""
        # Negative control: missing prefix
        res_p = run_eval(LUA_STATUS, 0, "", "alice", "busy")
        self.assertIn("ERR: Missing prefix", res_p)

        # Negative control: missing agent name
        res_n = run_eval(LUA_STATUS, 0, PREFIX, "", "busy")
        self.assertIn("ERR: Missing agent name", res_n)

        # Setup: Register alice (worker,ml) and bystander bob (qa)
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "worker,ml", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, "bob", "qa", "120")

        # Initial alice state before status call (empty state in hash, defaulted to idle in directory)
        self.assertEqual(run_redis("HGET", f"{PREFIX}agent:alice", "state"), "")
        self.assertEqual(run_redis("HGET", f"{PREFIX}agent:alice", "activity"), "")
        dir_init = run_eval(LUA_DIRECTORY, 0, PREFIX)
        self.assertIn("alice|1|worker,ml|idle|", dir_init)

        # Call status with state='busy', activity='Processing dataset #42', and custom TTL 45
        res = run_eval(LUA_STATUS, 0, PREFIX, "alice", "busy", "Processing dataset #42", "45")
        self.assertEqual(res, "OK")

        # Verify hash fields and heartbeat in Redis
        self.assertEqual(run_redis("HGET", f"{PREFIX}agent:alice", "state"), "busy")
        self.assertEqual(run_redis("HGET", f"{PREFIX}agent:alice", "activity"), "Processing dataset #42")
        last_seen = run_redis("HGET", f"{PREFIX}agent:alice", "last_seen")
        self.assertTrue(last_seen.isdigit(), f"Expected numeric last_seen, got: {last_seen}")
        self.assertEqual(run_redis("GET", f"{PREFIX}heartbeat:alice"), "1")
        ttl_alice = int(run_redis("TTL", f"{PREFIX}heartbeat:alice"))
        self.assertTrue(35 <= ttl_alice <= 45, f"Heartbeat TTL out of expected range: {ttl_alice}")

        # Directory reflects updated state and activity
        directory = run_eval(LUA_DIRECTORY, 0, PREFIX)
        self.assertIn("alice|1|worker,ml|busy|Processing dataset #42", directory)
        # Bystander bob unchanged with default idle state
        self.assertIn("bob|1|qa|idle|", directory)

        # Simulate alice being pruned from active_agents and heartbeat expiring
        run_redis("SREM", f"{PREFIX}active_agents", "alice")
        run_redis("DEL", f"{PREFIX}heartbeat:alice")
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}active_agents", "alice"), "0")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}heartbeat:alice"), "0")

        # Call status again: must refresh heartbeat, update fields, AND restore active_agents membership
        res_restore = run_eval(LUA_STATUS, 0, PREFIX, "alice", "idle", "Awaiting tasks", "60")
        self.assertEqual(res_restore, "OK")
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}active_agents", "alice"), "1")
        self.assertEqual(run_redis("GET", f"{PREFIX}heartbeat:alice"), "1")
        self.assertEqual(run_redis("HGET", f"{PREFIX}agent:alice", "state"), "idle")
        self.assertEqual(run_redis("HGET", f"{PREFIX}agent:alice", "activity"), "Awaiting tasks")

        # Directory confirms alice is back online in active roster
        dir_after = run_eval(LUA_DIRECTORY, 0, PREFIX)
        self.assertIn("alice|1|worker,ml|idle|Awaiting tasks", dir_after)

    def test_17_distributed_lock_and_unlock_lua(self):
        """Test lock.lua and unlock.lua atomic lease acquisition, mutual exclusion, and release semantics."""
        lock_name = "git_checkout"

        # 1. Argument validation negative controls for lock.lua
        res_lock_no_prefix = run_eval(LUA_LOCK, 0, "", lock_name, "alice")
        self.assertIn("ERR: Missing prefix", res_lock_no_prefix)

        res_lock_no_name = run_eval(LUA_LOCK, 0, PREFIX, "", "alice")
        self.assertIn("ERR: Missing lock name", res_lock_no_name)

        res_lock_no_owner = run_eval(LUA_LOCK, 0, PREFIX, lock_name, "")
        self.assertIn("ERR: Missing lock owner", res_lock_no_owner)

        # 2. Argument validation negative controls for unlock.lua
        res_un_no_prefix = run_eval(LUA_UNLOCK, 0, "", lock_name, "alice")
        self.assertIn("ERR: Missing prefix", res_un_no_prefix)

        res_un_no_name = run_eval(LUA_UNLOCK, 0, PREFIX, "", "alice")
        self.assertIn("ERR: Missing lock name", res_un_no_name)

        res_un_no_owner = run_eval(LUA_UNLOCK, 0, PREFIX, lock_name, "")
        self.assertIn("ERR: Missing lock owner", res_un_no_owner)

        # 3. Alice acquires lock with 10s TTL
        res_a = run_eval(LUA_LOCK, 0, PREFIX, lock_name, "alice", "10")
        self.assertEqual(res_a, "1")
        lock_key = f"{PREFIX}lock:{{{lock_name}}}"
        self.assertEqual(run_redis("GET", lock_key), "alice")
        ttl = int(run_redis("TTL", lock_key))
        self.assertTrue(5 <= ttl <= 10, f"Lock lease TTL out of expected range: {ttl}")

        # 4. Bob attempts to acquire same lock -> must fail (return 0)
        res_b = run_eval(LUA_LOCK, 0, PREFIX, lock_name, "bob", "10")
        self.assertEqual(res_b, "0")
        # Ensure lock ownership unchanged
        self.assertEqual(run_redis("GET", lock_key), "alice")

        # 5. Non-owner (Bob) attempts to release Alice's lock -> must fail (return 0)
        res_un_b = run_eval(LUA_UNLOCK, 0, PREFIX, lock_name, "bob")
        self.assertEqual(res_un_b, "0")
        # Lock must remain held by Alice
        self.assertEqual(run_redis("EXISTS", lock_key), "1")
        self.assertEqual(run_redis("GET", lock_key), "alice")

        # 6. Another non-owner (Charlie) attempts to release Alice's lock -> must fail (0)
        res_un_c = run_eval(LUA_UNLOCK, 0, PREFIX, lock_name, "charlie")
        self.assertEqual(res_un_c, "0")
        self.assertEqual(run_redis("GET", lock_key), "alice")

        # 7. Alice releases her lock -> must succeed (return 1)
        res_un_a = run_eval(LUA_UNLOCK, 0, PREFIX, lock_name, "alice")
        self.assertEqual(res_un_a, "1")
        self.assertEqual(run_redis("EXISTS", lock_key), "0")

        # 8. Idempotent unlock on already-released lock -> returns 0
        res_un_a2 = run_eval(LUA_UNLOCK, 0, PREFIX, lock_name, "alice")
        self.assertEqual(res_un_a2, "0")

        # 9. Bob can now acquire the previously released lock immediately
        res_b2 = run_eval(LUA_LOCK, 0, PREFIX, lock_name, "bob", "10")
        self.assertEqual(res_b2, "1")
        self.assertEqual(run_redis("GET", lock_key), "bob")

        # Cleanup: Bob releases lock
        self.assertEqual(run_eval(LUA_UNLOCK, 0, PREFIX, lock_name, "bob"), "1")
        self.assertEqual(run_redis("EXISTS", lock_key), "0")

    def test_18_enqueue_work_queue_lua(self):
        """Test enqueue.lua pushes tasks to work queue with LPUSH, verifies TTL, DLQ key tags, and FIFO ordering."""
        qname = "render_jobs"
        key = f"{PREFIX}queue:{{{qname}}}"

        # 1. Argument validation negative controls
        res_no_prefix = run_eval(LUA_ENQUEUE, 0, "", qname, "{}")
        self.assertIn("ERR: Missing prefix", res_no_prefix)

        res_no_qname = run_eval(LUA_ENQUEUE, 0, PREFIX, "", "{}")
        self.assertIn("ERR: Missing queue name", res_no_qname)

        res_no_payload = run_eval(LUA_ENQUEUE, 0, PREFIX, qname, "")
        self.assertIn("ERR: Missing message payload", res_no_payload)

        # 2. Structured message payloads
        msg1_dict = {
            "id": "job_01",
            "from": "orchestrator",
            "to": "@render",
            "type": "task",
            "subject": "Scene 1",
            "body": "Render frames 0-100",
            "timestamp": "2026-09-18T10:00:00Z"
        }
        msg2_dict = {
            "id": "job_02",
            "from": "orchestrator",
            "to": "@render",
            "type": "task",
            "subject": "Scene 2",
            "body": "Render frames 101-200",
            "timestamp": "2026-09-18T10:01:00Z"
        }
        raw_msg1 = json.dumps(msg1_dict)
        raw_msg2 = json.dumps(msg2_dict)

        # 3. Enqueue msg1 with custom 300s TTL
        len1 = run_eval(LUA_ENQUEUE, 0, PREFIX, qname, raw_msg1, "300")
        self.assertEqual(int(len1), 1)
        self.assertEqual(run_redis("EXISTS", key), "1")
        self.assertEqual(run_redis("TYPE", key), "list")
        ttl = int(run_redis("TTL", key))
        self.assertTrue(250 <= ttl <= 300, f"Queue TTL out of expected range: {ttl}")

        # 4. Enqueue msg2 with custom 300s TTL
        len2 = run_eval(LUA_ENQUEUE, 0, PREFIX, qname, raw_msg2, "300")
        self.assertEqual(int(len2), 2)
        self.assertEqual(int(run_redis("LLEN", key)), 2)

        # 5. DLQ routing test: queue name prefixed with 'dlq:'
        dlq_qname = f"dlq:{qname}"
        dlq_key = f"{PREFIX}queue:dlq:{{{qname}}}"
        dlq_msg = json.dumps({"id": "err_01", "from": "worker", "to": "@dlq", "type": "task", "subject": "Failed", "body": "Fatal error", "timestamp": "2026-09-18T10:02:00Z"})
        dlq_len = run_eval(LUA_ENQUEUE, 0, PREFIX, dlq_qname, dlq_msg, "600")
        self.assertEqual(int(dlq_len), 1)
        self.assertEqual(run_redis("EXISTS", dlq_key), "1")
        dlq_ttl = int(run_redis("TTL", dlq_key))
        self.assertTrue(550 <= dlq_ttl <= 600, f"DLQ TTL out of range: {dlq_ttl}")
        self.assertEqual(run_redis("RPOP", dlq_key), dlq_msg)

        # 6. Strict FIFO consumer consumption (RPOP pops tail = oldest message first)
        pop1 = run_redis("RPOP", key)
        self.assertEqual(pop1, raw_msg1)
        LocutusPlugin.validate_wire_envelope(pop1)
        m1 = LocutusMessage.model_validate_json(pop1)
        self.assertEqual(m1.id, "job_01")
        self.assertEqual(m1.body, "Render frames 0-100")
        a1 = A2AMessage.model_validate_json(pop1)
        self.assertEqual(a1.from_agent, "orchestrator")
        self.assertEqual(int(run_redis("LLEN", key)), 1)

        pop2 = run_redis("RPOP", key)
        self.assertEqual(pop2, raw_msg2)
        LocutusPlugin.validate_wire_envelope(pop2)
        m2 = LocutusMessage.model_validate_json(pop2)
        self.assertEqual(m2.id, "job_02")
        self.assertEqual(m2.body, "Render frames 101-200")
        a2 = A2AMessage.model_validate_json(pop2)
        self.assertEqual(a2.from_agent, "orchestrator")
        self.assertEqual(int(run_redis("LLEN", key)), 0)

        # Negative control: bystander queue does not exist
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}queue:{{other_queue}}"), "0")

    def test_19_directory_auto_pruning(self):
        """Test directory.lua automatically prunes dead agents upon natural heartbeat TTL expiry."""
        agent_alive = "alive_agent_1"
        agent_expiring = "expiring_agent_2"

        # 1. Register alive agent with long TTL (120s) and expiring agent with short TTL (1s)
        run_eval(LUA_REGISTER, 0, PREFIX, agent_alive, "team,projectX", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, agent_expiring, "team,projectX", "1")

        # 2. Immediately verify both are registered and alive
        init_dir = run_eval(LUA_DIRECTORY, 0, PREFIX, "projectX")
        init_lines = init_dir.splitlines() if isinstance(init_dir, str) else list(init_dir)
        self.assertTrue(any(line.startswith(f"{agent_alive}|1|") for line in init_lines))
        self.assertTrue(any(line.startswith(f"{agent_expiring}|1|") for line in init_lines))
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}heartbeat:{agent_expiring}"), "1")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:{agent_expiring}"), "1")

        # 3. Wait for natural heartbeat expiration across TTL boundary (1.2s)
        time.sleep(1.2)

        # 4. Verify heartbeat key expired naturally without manual deletion
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}heartbeat:{agent_expiring}"), "0")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}heartbeat:{agent_alive}"), "1")

        # Prior to directory query, agent_expiring is still in active_agents set (lazy pruning)
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}active_agents", agent_expiring), "1")

        # 5. Query directory for projectX: triggers auto-pruning of expired agent
        directory_res = run_eval(LUA_DIRECTORY, 0, PREFIX, "projectX")
        directory_lines = directory_res.splitlines() if isinstance(directory_res, str) else list(directory_res)

        # 6. Assert alive_agent_1 is retained and marked alive (1)
        alive_entries = [line for line in directory_lines if line.startswith(f"{agent_alive}|")]
        self.assertEqual(len(alive_entries), 1)
        self.assertIn("|1|team,projectX|idle|", alive_entries[0])

        # 7. Assert agent_expiring is completely absent from directory output
        expiring_entries = [line for line in directory_lines if line.startswith(f"{agent_expiring}|")]
        self.assertEqual(len(expiring_entries), 0)

        # 8. Verify agent_expiring was pruned from active_agents, all tag sets, and metadata hash
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}active_agents", agent_expiring), "0")
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}tag:projectX", agent_expiring), "0")
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}tag:team", agent_expiring), "0")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:{agent_expiring}"), "0")

        # 9. Verify alive agent's metadata and set memberships remain fully intact
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}active_agents", agent_alive), "1")
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}tag:projectX", agent_alive), "1")
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}tag:team", agent_alive), "1")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:{agent_alive}"), "1")

        # 10. Negative control: Querying non-existent tag returns empty and alters zero keys
        res_nonexistent = run_eval(LUA_DIRECTORY, 0, PREFIX, "nonexistent_project")
        self.assertEqual(res_nonexistent, "")

    def test_20_scatter_lua_protocol(self):
        """Test scatter.lua fan-out directly via Redis EVAL with exact payload delivery and schema verification."""
        # 1. Argument validation negative controls
        res_no_p = run_eval(LUA_SCATTER, 0, "", "@workers", "{}")
        self.assertIn("ERR: Missing prefix", res_no_p)

        res_no_body = run_eval(LUA_SCATTER, 0, PREFIX, "@workers", "")
        self.assertIn("ERR: Missing message payload", res_no_body)

        # 2. Register worker agents and bystander
        a1 = "scatter_worker_1"
        a2 = "scatter_worker_2"
        bystander = "scatter_bystander"
        run_eval(LUA_REGISTER, 0, PREFIX, a1, "workers,compute", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, a2, "workers,gpu", "120")
        run_eval(LUA_REGISTER, 0, PREFIX, bystander, "qa", "120")

        # 3. Formulate structured message
        msg_dict = {
            "id": "scatter_task_99",
            "from": "planner",
            "to": "@workers",
            "type": "task",
            "subject": "Distributed Map",
            "body": "Map slice partition",
            "timestamp": "2026-09-18T15:00:00Z"
        }
        raw_msg = json.dumps(msg_dict)

        # 4. Scatter to tag @workers: delivers to both a1 and a2
        delivered_tag = run_eval(LUA_SCATTER, 0, PREFIX, "@workers", raw_msg, "300")
        self.assertEqual(int(delivered_tag), 2)

        # Verify inboxes received exactly 1 message each
        self.assertEqual(int(run_redis("LLEN", f"{PREFIX}inbox:{a1}")), 1)
        self.assertEqual(int(run_redis("LLEN", f"{PREFIX}inbox:{a2}")), 1)
        self.assertEqual(int(run_redis("LLEN", f"{PREFIX}inbox:{bystander}")), 0)

        # Drain inboxes, assert exact payload match and validate wire envelope
        for agent_name in [a1, a2]:
            inbox_item = run_redis("RPOP", f"{PREFIX}inbox:{agent_name}")
            self.assertEqual(inbox_item, raw_msg)
            LocutusPlugin.validate_wire_envelope(inbox_item)
            m = LocutusMessage.model_validate_json(inbox_item)
            self.assertEqual(m.id, "scatter_task_99")
            self.assertEqual(m.from_agent, "planner")
            self.assertEqual(m.body, "Map slice partition")
            a2a = A2AMessage.model_validate_json(inbox_item)
            self.assertEqual(a2a.from_agent, "planner")
            self.assertEqual(int(run_redis("LLEN", f"{PREFIX}inbox:{agent_name}")), 0)

        # 5. Scatter by explicit comma-separated names
        delivered_names = run_eval(LUA_SCATTER, 0, PREFIX, f"{a1},{a2}", raw_msg, "300")
        self.assertEqual(int(delivered_names), 2)
        self.assertEqual(int(run_redis("LLEN", f"{PREFIX}inbox:{a1}")), 1)
        self.assertEqual(int(run_redis("LLEN", f"{PREFIX}inbox:{a2}")), 1)
        # Clear inboxes
        run_redis("DEL", f"{PREFIX}inbox:{a1}", f"{PREFIX}inbox:{a2}")

        # 6. Scatter to non-existent tag -> 0 delivered
        res_empty = run_eval(LUA_SCATTER, 0, PREFIX, "@nonexistent_tag", raw_msg, "300")
        self.assertEqual(int(res_empty), 0)

        # 7. Dead agent pruning: simulate a2 dying
        run_redis("DEL", f"{PREFIX}heartbeat:{a2}")
        delivered_pruned = run_eval(LUA_SCATTER, 0, PREFIX, "@workers", raw_msg, "300")
        # Only living a1 received message
        self.assertEqual(int(delivered_pruned), 1)
        self.assertEqual(int(run_redis("LLEN", f"{PREFIX}inbox:{a1}")), 1)
        self.assertEqual(int(run_redis("LLEN", f"{PREFIX}inbox:{a2}")), 0)

        # Confirm a2 was pruned from active roster and tag sets
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}active_agents", a2), "0")
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}tag:workers", a2), "0")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:{a2}"), "0")

        # Drain a1
        item_a1 = run_redis("RPOP", f"{PREFIX}inbox:{a1}")
        self.assertEqual(item_a1, raw_msg)
        LocutusPlugin.validate_wire_envelope(item_a1)

    def test_21_reliable_queue_lua_protocol(self):
        """Test claim.lua and ack.lua via Redis EVAL with lease timestamp score verification and schema validation."""
        q = f"proto_q_{int(time.time() * 1000)}"

        # 1. Argument validation negative controls
        res_claim_no_p = run_eval(LUA_CLAIM, 0, "", q, "worker1", "10", "3")
        self.assertIn("ERR: Missing prefix", res_claim_no_p)

        res_claim_no_q = run_eval(LUA_CLAIM, 0, PREFIX, "", "worker1", "10", "3")
        self.assertIn("ERR: Missing queue name", res_claim_no_q)

        res_ack_no_p = run_eval(LUA_ACK, 0, "", q, "task_01")
        self.assertIn("ERR: Missing prefix", res_ack_no_p)

        res_ack_no_q = run_eval(LUA_ACK, 0, PREFIX, "", "task_01")
        self.assertIn("ERR: Missing queue name", res_ack_no_q)

        res_ack_no_id = run_eval(LUA_ACK, 0, PREFIX, q, "")
        self.assertIn("ERR: Missing task ID", res_ack_no_id)

        # 2. Enqueue structured wire envelope task
        msg_dict = {
            "id": "task_proto_1",
            "from": "planner",
            "to": "@worker",
            "type": "task",
            "subject": "Reliable Processing",
            "body": "Payload 123",
            "timestamp": "2026-09-18T16:00:00Z"
        }
        raw_msg = json.dumps(msg_dict)
        run_eval(LUA_ENQUEUE, 0, PREFIX, q, raw_msg, "300")

        # 3. Capture Redis server epoch time before claiming
        now_ts = int(run_redis("TIME").split()[0])

        # 4. Claim task with 10-second lease
        claimed = run_eval(LUA_CLAIM, 0, PREFIX, q, "worker1", "10", "3")
        self.assertEqual(claimed, raw_msg)
        LocutusPlugin.validate_wire_envelope(claimed)
        m = LocutusMessage.model_validate_json(claimed)
        self.assertEqual(m.id, "task_proto_1")
        self.assertEqual(m.body, "Payload 123")
        a2a = A2AMessage.model_validate_json(claimed)
        self.assertEqual(a2a.from_agent, "planner")

        # 5. Cluster slot keyspace checks
        active_key = f"{PREFIX}active:{{{q}}}:task_proto_1"
        leases_key = f"{PREFIX}leases:{{{q}}}"
        attempts_key = f"{PREFIX}attempts:{{{q}}}"

        self.assertEqual(run_redis("EXISTS", active_key), "1")
        self.assertEqual(run_redis("GET", active_key), raw_msg)
        self.assertEqual(run_redis("HGET", attempts_key, "task_proto_1"), "1")

        # 6. Verify lease sorted set score equals now + lease_sec (10s)
        score_str = run_redis("ZSCORE", leases_key, "task_proto_1")
        self.assertTrue(bool(score_str), f"Expected non-empty ZSCORE for task_proto_1, got: '{score_str}'")
        score = float(score_str)
        expected_expiry = now_ts + 10
        self.assertTrue(
            abs(score - expected_expiry) <= 2,
            f"Lease expiration score {score} differs from expected {expected_expiry}"
        )

        # 7. Acknowledge task completion
        ack_res = run_eval(LUA_ACK, 0, PREFIX, q, "task_proto_1")
        self.assertEqual(int(ack_res), 1)

        # 8. Assert full cleanup of lease, active data, and attempts
        self.assertEqual(run_redis("ZSCORE", leases_key, "task_proto_1"), "")
        self.assertEqual(run_redis("EXISTS", active_key), "0")
        self.assertEqual(run_redis("HEXISTS", attempts_key, "task_proto_1"), "0")

        # 9. Idempotent ack on already acknowledged task returns 0
        ack_again = run_eval(LUA_ACK, 0, PREFIX, q, "task_proto_1")
        self.assertEqual(int(ack_again), 0)

        # 10. Claim on empty queue returns nil / empty string
        empty_claim = run_eval(LUA_CLAIM, 0, PREFIX, q, "worker1", "10", "3")
        self.assertEqual(empty_claim, "")

    def test_22_blackboard_lua_protocol(self):
        """Test blackboard.lua via Redis EVAL with OCC revision token enforcement and schema validation."""
        room = f"proto_room_{int(time.time() * 1000)}"

        # 1. Argument validation negative controls
        res_no_p = run_eval(LUA_BLACKBOARD, 0, "", "set", room, "k", "v")
        self.assertIn("ERR: Missing prefix", res_no_p)

        res_no_act = run_eval(LUA_BLACKBOARD, 0, PREFIX, "", room, "k", "v")
        self.assertIn("ERR: Missing action", res_no_act)

        res_no_key = run_eval(LUA_BLACKBOARD, 0, PREFIX, "set", room, "", "v")
        self.assertIn("ERR: Missing key", res_no_key)

        # 2. Initial set of key-value
        set_res = run_eval(LUA_BLACKBOARD, 0, PREFIX, "set", room, "author", "alice", "300")
        self.assertEqual(set_res, "OK")
        val = run_eval(LUA_BLACKBOARD, 0, PREFIX, "get", room, "author", "", "300")
        self.assertEqual(val, "alice")

        # 3. Assert initial revision is 1
        rev1 = run_eval(LUA_BLACKBOARD, 0, PREFIX, "rev", room, "author")
        self.assertEqual(rev1, "1")

        # 4. OCC negative control: updating with outdated revision token (e.g. expected 0) must be rejected
        res_stale = run_eval(LUA_BLACKBOARD, 0, PREFIX, "set", room, "author", "eve", "300", "0")
        self.assertIn("ERR: OCC revision mismatch", res_stale)
        # Value in Redis must remain unchanged and revision must not increment
        self.assertEqual(run_eval(LUA_BLACKBOARD, 0, PREFIX, "get", room, "author"), "alice")
        self.assertEqual(run_eval(LUA_BLACKBOARD, 0, PREFIX, "rev", room, "author"), "1")

        # 5. OCC success: updating with matching revision token (expected 1) succeeds and increments revision to 2
        res_valid = run_eval(LUA_BLACKBOARD, 0, PREFIX, "set", room, "author", "bob", "300", "1")
        self.assertEqual(res_valid, "OK")
        self.assertEqual(run_eval(LUA_BLACKBOARD, 0, PREFIX, "get", room, "author"), "bob")
        rev2 = run_eval(LUA_BLACKBOARD, 0, PREFIX, "rev", room, "author")
        self.assertEqual(rev2, "2")

        # 6. Append to list memory
        app1 = run_eval(LUA_BLACKBOARD, 0, PREFIX, "append", room, "notes", "Note 1", "300")
        self.assertEqual(int(app1), 1)
        app2 = run_eval(LUA_BLACKBOARD, 0, PREFIX, "append", room, "notes", "Note 2", "300")
        self.assertEqual(int(app2), 2)
        notes_json = run_eval(LUA_BLACKBOARD, 0, PREFIX, "get", room, "notes", "", "300")
        self.assertEqual(json.loads(notes_json), ["Note 1", "Note 2"])

        # 7. Snapshot validation with schema
        snap_json = run_eval(LUA_BLACKBOARD, 0, PREFIX, "snapshot", room, "", "", "300")
        LocutusPlugin.validate_json_schema("blackboard", snap_json)
        snap = json.loads(snap_json)
        self.assertEqual(snap["room"], room)
        self.assertEqual(snap["kv"]["author"], "bob")
        self.assertEqual(snap["lists"]["notes"], ["Note 1", "Note 2"])

        # 8. Single key deletion
        del_res = run_eval(LUA_BLACKBOARD, 0, PREFIX, "delete", room, "author")
        self.assertEqual(del_res, "OK")
        self.assertEqual(run_eval(LUA_BLACKBOARD, 0, PREFIX, "get", room, "author"), "")

        # 9. Clean up entire room and verify full keyspace teardown
        run_eval(LUA_BLACKBOARD, 0, PREFIX, "clear", room, "", "", "300")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}blackboard:{{{room}}}:kv"), "0")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}blackboard:{{{room}}}:rev"), "0")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}blackboard:{{{room}}}:lists"), "0")

    def test_23_floor_lua_protocol(self):
        """Test floor.lua via Redis EVAL with waiters serialization, mutual exclusion, and handoff semantics."""
        room = f"proto_floor_{int(time.time() * 1000)}"
        holder_key = f"{PREFIX}floor:{{{room}}}:holder"
        waiters_key = f"{PREFIX}floor:{{{room}}}:waiters"

        # 1. Argument validation negative controls
        res_no_p = run_eval(LUA_FLOOR, 0, "", "status", room)
        self.assertIn("ERR: Missing prefix", res_no_p)

        res_no_act = run_eval(LUA_FLOOR, 0, PREFIX, "", room)
        self.assertIn("ERR: Missing action", res_no_act)

        # 2. Initial status: floor is free, waiters serialized strictly as empty JSON array []
        init_st_raw = run_eval(LUA_FLOOR, 0, PREFIX, "status", room)
        self.assertIn('"waiters":[]', init_st_raw, "Empty waiters table must strictly serialize as JSON array '[]', not '{}'")
        init_st = json.loads(init_st_raw)
        self.assertEqual(init_st["holder"], "")
        self.assertEqual(init_st["ttl"], 0)
        self.assertIsInstance(init_st["waiters"], list)
        self.assertEqual(init_st["waiters"], [])

        # 3. Alice acquires floor with 15s lease
        res_acq = run_eval(LUA_FLOOR, 0, PREFIX, "request", room, "alice", "15")
        self.assertEqual(res_acq, "ACQUIRED")
        self.assertEqual(run_redis("GET", holder_key), "alice")
        ttl = int(run_redis("TTL", holder_key))
        self.assertTrue(10 <= ttl <= 15, f"Floor holder TTL out of expected range: {ttl}")

        # 4. Mutual exclusion: Bob's request is rejected with BUSY:alice
        busy = run_eval(LUA_FLOOR, 0, PREFIX, "request", room, "bob", "15")
        self.assertEqual(busy, "BUSY:alice")

        # 5. Enqueue waiters: Bob and Charlie queue for floor
        q_bob = run_eval(LUA_FLOOR, 0, PREFIX, "enqueue_waiter", room, "bob")
        self.assertEqual(q_bob, "QUEUED")
        q_charlie = run_eval(LUA_FLOOR, 0, PREFIX, "enqueue_waiter", room, "charlie")
        self.assertEqual(q_charlie, "QUEUED")

        # Verify status reflects active holder and populated waiters array
        st_queued_raw = run_eval(LUA_FLOOR, 0, PREFIX, "status", room)
        st_queued = json.loads(st_queued_raw)
        self.assertEqual(st_queued["holder"], "alice")
        self.assertIsInstance(st_queued["waiters"], list)
        self.assertEqual(st_queued["waiters"], ["bob", "charlie"])

        # 6. Unauthorized pass/yield negative controls: non-holder cannot pass or yield floor
        err_pass = run_eval(LUA_FLOOR, 0, PREFIX, "pass", room, "eve", "dan", "15")
        self.assertIn("ERR: Floor is held by alice", err_pass)
        err_yield = run_eval(LUA_FLOOR, 0, PREFIX, "yield", room, "eve")
        self.assertIn("ERR: Floor is held by alice", err_yield)
        self.assertEqual(run_redis("GET", holder_key), "alice")

        # 7. Alice yields floor: auto-handoff to first queued waiter (bob)
        yield_alice = run_eval(LUA_FLOOR, 0, PREFIX, "yield", room, "alice", "", "15")
        self.assertEqual(yield_alice, "PASSED:bob")
        self.assertEqual(run_redis("GET", holder_key), "bob")

        # Remaining waiter should now be only charlie
        st_bob = json.loads(run_eval(LUA_FLOOR, 0, PREFIX, "status", room))
        self.assertEqual(st_bob["holder"], "bob")
        self.assertEqual(st_bob["waiters"], ["charlie"])

        # 8. Bob passes floor directly to Dan
        passed_dan = run_eval(LUA_FLOOR, 0, PREFIX, "pass", room, "bob", "dan", "15")
        self.assertEqual(passed_dan, "PASSED:dan")
        self.assertEqual(run_redis("GET", holder_key), "dan")

        # 9. Dan yields: auto-handoff to remaining waiter Charlie
        yield_dan = run_eval(LUA_FLOOR, 0, PREFIX, "yield", room, "dan", "", "15")
        self.assertEqual(yield_dan, "PASSED:charlie")
        self.assertEqual(run_redis("GET", holder_key), "charlie")

        # 10. Charlie yields with no waiters left: floor becomes FREE
        yield_final = run_eval(LUA_FLOOR, 0, PREFIX, "yield", room, "charlie")
        self.assertEqual(yield_final, "YIELDED")
        self.assertEqual(run_redis("EXISTS", holder_key), "0")

        # Final status check: empty waiters array strictly formatted as []
        fin_st_raw = run_eval(LUA_FLOOR, 0, PREFIX, "status", room)
        self.assertIn('"waiters":[]', fin_st_raw)
        fin_st = json.loads(fin_st_raw)
        self.assertEqual(fin_st["holder"], "")
        self.assertIsInstance(fin_st["waiters"], list)
        self.assertEqual(fin_st["waiters"], [])

    def test_24_cancel_lua_protocol(self):
        """Test cancel.lua via Redis EVAL with cancellation token payload, timestamp, and TTL assertions."""
        run_id = f"run_{int(time.time() * 1000)}"
        cancel_key = f"{PREFIX}cancel:{run_id}"

        # 1. Argument validation negative controls
        res_no_p = run_eval(LUA_CANCEL, 0, "", "check", run_id)
        self.assertIn("ERR: Missing prefix", res_no_p)

        res_no_act = run_eval(LUA_CANCEL, 0, PREFIX, "", run_id)
        self.assertIn("ERR: Missing action", res_no_act)

        res_no_id = run_eval(LUA_CANCEL, 0, PREFIX, "check", "")
        self.assertIn("ERR: Missing run_id", res_no_id)

        res_bad_act = run_eval(LUA_CANCEL, 0, PREFIX, "bogus_action", run_id)
        self.assertIn("ERR: Unknown cancel action 'bogus_action'", res_bad_act)

        # 2. Check initial state: should be empty
        init_chk = run_eval(LUA_CANCEL, 0, PREFIX, "check", run_id)
        self.assertEqual(init_chk, "")
        self.assertEqual(run_redis("EXISTS", cancel_key), "0")

        # 3. Emit cancellation token with custom reason, agent, TTL (1800s), timestamp, and signature
        res = run_eval(
            LUA_CANCEL, 0, PREFIX, "cancel", run_id,
            "User aborted operation", "lead_agent", "1800", "2026-09-18T16:30:00Z", "hmac_sig_test_123"
        )
        self.assertEqual(res, "CANCELLED")

        # 4. Direct Redis verification of key existence and TTL
        self.assertEqual(run_redis("EXISTS", cancel_key), "1")
        ttl = int(run_redis("TTL", cancel_key))
        self.assertTrue(1700 <= ttl <= 1800, f"Cancellation token TTL out of range: {ttl}")

        # 5. Check action: returns JSON payload containing all exact fields
        chk_json = run_eval(LUA_CANCEL, 0, PREFIX, "check", run_id)
        self.assertTrue(len(chk_json) > 0)
        data = json.loads(chk_json)
        self.assertTrue(data.get("cancelled"))
        self.assertEqual(data.get("run_id"), run_id)
        self.assertEqual(data.get("reason"), "User aborted operation")
        self.assertEqual(data.get("by"), "lead_agent")
        self.assertEqual(data.get("timestamp"), "2026-09-18T16:30:00Z")
        self.assertEqual(data.get("sig"), "hmac_sig_test_123")

        # 6. Alias 'status' returns identical payload
        st_json = run_eval(LUA_CANCEL, 0, PREFIX, "status", run_id)
        self.assertEqual(st_json, chk_json)

        # 7. Fallback auto-timestamping when timestamp omitted
        run_id_2 = f"run2_{int(time.time() * 1000)}"
        run_eval(LUA_CANCEL, 0, PREFIX, "cancel", run_id_2, "Cluster panic", "watchdog", "600")
        data2 = json.loads(run_eval(LUA_CANCEL, 0, PREFIX, "check", run_id_2))
        self.assertEqual(data2.get("reason"), "Cluster panic")
        self.assertEqual(data2.get("by"), "watchdog")
        ts2 = data2.get("timestamp", "")
        self.assertTrue(ts2.isdigit(), f"Expected numeric server timestamp, got: '{ts2}'")
        run_redis("DEL", f"{PREFIX}cancel:{run_id_2}")

        # 8. Clear action: cleans up token
        clr = run_eval(LUA_CANCEL, 0, PREFIX, "clear", run_id)
        self.assertEqual(clr, "CLEARED")
        self.assertEqual(run_redis("EXISTS", cancel_key), "0")
        after_clr = run_eval(LUA_CANCEL, 0, PREFIX, "check", run_id)
        self.assertEqual(after_clr, "")

        # 9. Negative control: bystander run_id check returns empty
        self.assertEqual(run_eval(LUA_CANCEL, 0, PREFIX, "check", "nonexistent_run"), "")

    def test_25_ballot_lua_protocol(self):
        """Test ballot.lua via Redis EVAL with restricted voter eligibility, option validation, and tally semantics."""
        ballot_id = f"ballot_{int(time.time() * 1000)}"
        meta_key = f"{PREFIX}ballot:{{{ballot_id}}}"
        votes_key = f"{PREFIX}ballot:votes:{{{ballot_id}}}"

        # 1. Argument validation negative controls
        res_no_p = run_eval(LUA_BALLOT, 0, "", "open", ballot_id)
        self.assertIn("ERR: Missing prefix", res_no_p)

        res_no_act = run_eval(LUA_BALLOT, 0, PREFIX, "", ballot_id)
        self.assertIn("ERR: Missing action", res_no_act)

        res_no_id = run_eval(LUA_BALLOT, 0, PREFIX, "open", "")
        self.assertIn("ERR: Missing ballot_id", res_no_id)

        res_no_opts = run_eval(LUA_BALLOT, 0, PREFIX, "open", ballot_id, "", "alice")
        self.assertIn("ERR: Missing options for ballot", res_no_opts)

        # 2. Open ballot with restricted voter roster: only alice, bob, carol
        open_res = run_eval(LUA_BALLOT, 0, PREFIX, "open", ballot_id, "optA,optB,optC", "alice,bob,carol", "1800")
        self.assertEqual(open_res, "OPEN")

        self.assertEqual(run_redis("HGET", meta_key, "status"), "open")
        self.assertEqual(run_redis("HGET", meta_key, "options"), "optA,optB,optC")
        self.assertEqual(run_redis("HGET", meta_key, "voters"), "alice,bob,carol")
        ttl = int(run_redis("TTL", meta_key))
        self.assertTrue(1700 <= ttl <= 1800, f"Ballot meta TTL out of range: {ttl}")

        # 3. Unlisted voter negative control: eve is not on the voter roster
        inv_voter = run_eval(LUA_BALLOT, 0, PREFIX, "cast", ballot_id, "eve", "optA")
        self.assertIn("ERR: Voter 'eve' is not eligible for this ballot", inv_voter)
        self.assertEqual(run_redis("HEXISTS", votes_key, "eve"), "0")

        # 4. Invalid option negative control: carol attempts to vote for optInvalid
        inv_opt = run_eval(LUA_BALLOT, 0, PREFIX, "cast", ballot_id, "carol", "optInvalid")
        self.assertIn("ERR: Invalid vote choice 'optInvalid'", inv_opt)
        self.assertEqual(run_redis("HEXISTS", votes_key, "carol"), "0")

        # 5. Missing arguments on cast
        res_no_voter = run_eval(LUA_BALLOT, 0, PREFIX, "cast", ballot_id, "", "optA")
        self.assertIn("ERR: Missing voter", res_no_voter)

        res_no_choice = run_eval(LUA_BALLOT, 0, PREFIX, "cast", ballot_id, "alice", "")
        self.assertIn("ERR: Missing vote choice", res_no_choice)

        # 6. Cast legitimate votes
        v1 = run_eval(LUA_BALLOT, 0, PREFIX, "cast", ballot_id, "alice", "optA", "sig_alice_123")
        self.assertEqual(v1, "VOTED")
        v2 = run_eval(LUA_BALLOT, 0, PREFIX, "cast", ballot_id, "bob", "optB")
        self.assertEqual(v2, "VOTED")
        v3 = run_eval(LUA_BALLOT, 0, PREFIX, "cast", ballot_id, "carol", "optA")
        self.assertEqual(v3, "VOTED")

        # Direct Redis inspection
        self.assertEqual(run_redis("HGET", votes_key, "alice"), "optA")
        self.assertEqual(run_redis("HGET", votes_key, "bob"), "optB")
        self.assertEqual(run_redis("HGET", votes_key, "carol"), "optA")
        self.assertEqual(int(run_redis("HLEN", votes_key)), 3)

        # 7. Tally and close
        tally_json = run_eval(LUA_BALLOT, 0, PREFIX, "tally", ballot_id, "close")
        tally = json.loads(tally_json)
        self.assertEqual(tally["ballot_id"], ballot_id)
        self.assertEqual(tally["total_votes"], 3)
        self.assertEqual(tally["tally"]["optA"], 2)
        self.assertEqual(tally["tally"]["optB"], 1)
        self.assertEqual(tally["tally"]["optC"], 0)
        self.assertEqual(tally["winner"], "optA")
        self.assertEqual(tally["status"], "closed")
        self.assertEqual(run_redis("HGET", meta_key, "status"), "closed")

        # 8. Post-close voting rejection: votes cannot be cast on closed ballot
        late_vote = run_eval(LUA_BALLOT, 0, PREFIX, "cast", ballot_id, "alice", "optB")
        self.assertIn("ERR: Ballot is not open", late_vote)

    def test_26_leader_lua_protocol(self):
        """Test leader.lua via Redis EVAL with non-leader rejection, lease renewal, and resign semantics."""
        role = f"role_{int(time.time() * 1000)}"
        leader_key = f"{PREFIX}leader:{{{role}}}"

        # 1. Argument validation negative controls
        res_no_p = run_eval(LUA_LEADER, 0, "", "status", role)
        self.assertIn("ERR: Missing prefix", res_no_p)

        res_no_act = run_eval(LUA_LEADER, 0, PREFIX, "", role)
        self.assertIn("ERR: Missing action", res_no_act)

        res_no_role = run_eval(LUA_LEADER, 0, PREFIX, "status", "")
        self.assertIn("ERR: Missing role", res_no_role)

        res_no_agent = run_eval(LUA_LEADER, 0, PREFIX, "acquire", role, "")
        self.assertIn("ERR: Missing agent", res_no_agent)

        res_bad_act = run_eval(LUA_LEADER, 0, PREFIX, "bogus_action", role)
        self.assertIn("ERR: Unknown leader action 'bogus_action'", res_bad_act)

        # 2. Initial vacant status check
        st0 = json.loads(run_eval(LUA_LEADER, 0, PREFIX, "status", role))
        self.assertEqual(st0["status"], "vacant")
        self.assertEqual(st0["leader"], "")
        self.assertEqual(st0["ttl"], 0)

        # 3. Renew on vacant role negative control
        err_vacant_ren = run_eval(LUA_LEADER, 0, PREFIX, "renew", role, "alice", "15")
        self.assertIn("ERR: No active leader", err_vacant_ren)

        # 4. Alice acquires leadership with 15s lease
        acq = run_eval(LUA_LEADER, 0, PREFIX, "acquire", role, "alice", "15", "2026-09-18T17:00:00Z", "sig_leader_1")
        self.assertEqual(acq, "ELECTED")

        # Direct Redis inspection
        self.assertEqual(run_redis("EXISTS", leader_key), "1")
        ttl = int(run_redis("TTL", leader_key))
        self.assertTrue(10 <= ttl <= 15, f"Leader lease TTL out of range: {ttl}")
        raw_leader = json.loads(run_redis("GET", leader_key))
        self.assertEqual(raw_leader["role"], role)
        self.assertEqual(raw_leader["leader"], "alice")
        self.assertEqual(raw_leader["acquired_at"], "2026-09-18T17:00:00Z")
        self.assertEqual(raw_leader["sig"], "sig_leader_1")

        # 5. Competitor preemption rejected while lease is active
        busy = run_eval(LUA_LEADER, 0, PREFIX, "acquire", role, "bob", "15")
        self.assertEqual(busy, "HELD:alice")

        # 6. Status check for active leadership
        st1 = json.loads(run_eval(LUA_LEADER, 0, PREFIX, "status", role))
        self.assertEqual(st1["role"], role)
        self.assertEqual(st1["leader"], "alice")
        self.assertEqual(st1["status"], "active")
        self.assertTrue(10 <= st1["ttl"] <= 15)

        # 7. Non-leader renew negative control: bob cannot renew alice's leadership
        err_bob_ren = run_eval(LUA_LEADER, 0, PREFIX, "renew", role, "bob", "20")
        self.assertIn("ERR: Not leader", err_bob_ren)

        # 8. Non-leader resign negative control: bob cannot resign alice's leadership
        err_bob_res = run_eval(LUA_LEADER, 0, PREFIX, "resign", role, "bob")
        self.assertIn("ERR: Not leader", err_bob_res)
        self.assertEqual(run_redis("EXISTS", leader_key), "1")

        # 9. Legitimate leader renew: alice renews lease
        ren = run_eval(LUA_LEADER, 0, PREFIX, "renew", role, "alice", "20", "2026-09-18T17:05:00Z")
        self.assertEqual(ren, "RENEWED")

        # 10. Legitimate leader resign: alice resigns
        res = run_eval(LUA_LEADER, 0, PREFIX, "resign", role, "alice")
        self.assertEqual(res, "RESIGNED")
        self.assertEqual(run_redis("EXISTS", leader_key), "0")

        # Status is now vacant
        st_after = json.loads(run_eval(LUA_LEADER, 0, PREFIX, "status", role))
        self.assertEqual(st_after["status"], "vacant")
        self.assertEqual(st_after["leader"], "")

        # 11. Bob can now acquire the vacant role immediately
        bob_acq = run_eval(LUA_LEADER, 0, PREFIX, "acquire", role, "bob", "10")
        self.assertEqual(bob_acq, "ELECTED")
        self.assertEqual(json.loads(run_redis("GET", leader_key))["leader"], "bob")

        # Cleanup: bob resigns
        self.assertEqual(run_eval(LUA_LEADER, 0, PREFIX, "resign", role, "bob"), "RESIGNED")

    def test_27_workflow_dag_lua_protocol(self):
        """Test workflow.lua via Redis EVAL with DAG progression, cycle detection, and failure state preservation."""
        flow_id = f"flow_{int(time.time() * 1000)}"
        flow_key = f"{PREFIX}workflow:{{{flow_id}}}"

        # 1. Argument validation negative controls
        res_no_p = run_eval(LUA_WORKFLOW, 0, "", "status", flow_id)
        self.assertIn("ERR: Missing prefix", res_no_p)

        res_no_act = run_eval(LUA_WORKFLOW, 0, PREFIX, "", flow_id)
        self.assertIn("ERR: Missing action", res_no_act)

        res_no_id = run_eval(LUA_WORKFLOW, 0, PREFIX, "status", "")
        self.assertIn("ERR: Missing flow_id", res_no_id)

        res_no_step = run_eval(LUA_WORKFLOW, 0, PREFIX, "resolve", flow_id, "")
        self.assertIn("ERR: Missing step name", res_no_step)

        res_bad_act = run_eval(LUA_WORKFLOW, 0, PREFIX, "bogus_action", flow_id)
        self.assertIn("ERR: Unknown workflow action 'bogus_action'", res_bad_act)

        # 2. Define DAG: lint -> test -> build; test & build -> deploy
        steps = "lint,test,build,deploy"
        deps = "test:lint;build:lint;deploy:test,build"
        d_res = run_eval(LUA_WORKFLOW, 0, PREFIX, "define", flow_id, steps, deps, "3600")
        self.assertEqual(d_res, "DEFINED")

        # Direct Redis inspection: check key exists and TTL
        self.assertEqual(run_redis("EXISTS", flow_key), "1")
        ttl = int(run_redis("TTL", flow_key))
        self.assertTrue(3500 <= ttl <= 3600, f"Workflow TTL out of range: {ttl}")

        # 3. Next: initially only 'lint' should be ready
        nxt1 = json.loads(run_eval(LUA_WORKFLOW, 0, PREFIX, "next", flow_id))
        self.assertEqual(nxt1["status"], "running")
        self.assertEqual(nxt1["ready"], ["lint"])
        self.assertEqual(set(nxt1["pending"]), {"test", "build", "deploy"})
        self.assertEqual(nxt1["completed"], [])

        # 4. Resolve 'lint': unlocks 'test' and 'build'
        r1 = json.loads(run_eval(LUA_WORKFLOW, 0, PREFIX, "resolve", flow_id, "lint", "lint passed"))
        self.assertEqual(r1["status"], "running")
        self.assertIn("test", r1["unlocked"])
        self.assertIn("build", r1["unlocked"])
        self.assertNotIn("deploy", r1["unlocked"])

        nxt2 = json.loads(run_eval(LUA_WORKFLOW, 0, PREFIX, "next", flow_id))
        self.assertEqual(set(nxt2["ready"]), {"test", "build"})
        self.assertEqual(nxt2["completed"], ["lint"])

        # 5. Resolve 'test': 'deploy' is NOT unlocked yet because 'build' is still pending
        r2 = json.loads(run_eval(LUA_WORKFLOW, 0, PREFIX, "resolve", flow_id, "test", "tests passed"))
        self.assertEqual(r2["status"], "running")
        self.assertNotIn("deploy", r2["unlocked"])

        nxt3 = json.loads(run_eval(LUA_WORKFLOW, 0, PREFIX, "next", flow_id))
        self.assertEqual(nxt3["ready"], ["build"])
        self.assertEqual(set(nxt3["completed"]), {"lint", "test"})

        # 6. Resolve 'build': unlocks 'deploy' (both dependencies test & build now completed)
        r3 = json.loads(run_eval(LUA_WORKFLOW, 0, PREFIX, "resolve", flow_id, "build", "build artifact ok"))
        self.assertEqual(r3["status"], "running")
        self.assertIn("deploy", r3["unlocked"])

        # 7. Resolve 'deploy': completes entire workflow
        r4 = json.loads(run_eval(LUA_WORKFLOW, 0, PREFIX, "resolve", flow_id, "deploy", "deployed v1.0.0"))
        self.assertEqual(r4["status"], "completed")

        nxt_final = json.loads(run_eval(LUA_WORKFLOW, 0, PREFIX, "next", flow_id))
        self.assertEqual(nxt_final["status"], "completed")
        self.assertEqual(nxt_final["ready"], [])
        self.assertEqual(nxt_final["pending"], [])
        self.assertEqual(set(nxt_final["completed"]), {"lint", "test", "build", "deploy"})

        # 8. Failure preservation: if a step fails, subsequent step resolutions MUST PRESERVE 'failed' status
        flow_fail_id = f"flow_fail_{int(time.time() * 1000)}"
        run_eval(LUA_WORKFLOW, 0, PREFIX, "define", flow_fail_id, "s1,s2,s3", "")
        # Fail s1
        fail_res = run_eval(LUA_WORKFLOW, 0, PREFIX, "fail", flow_fail_id, "s1", "out of memory")
        self.assertEqual(json.loads(fail_res)["status"], "failed")

        # Resolve s2: status must remain 'failed', NOT reset to 'running'
        res_after_s2 = json.loads(run_eval(LUA_WORKFLOW, 0, PREFIX, "resolve", flow_fail_id, "s2"))
        self.assertEqual(res_after_s2["status"], "failed")

        # Resolve s3 (all remaining steps completed): status must STILL remain 'failed', NOT reset to 'completed'!
        res_after_s3 = json.loads(run_eval(LUA_WORKFLOW, 0, PREFIX, "resolve", flow_fail_id, "s3"))
        self.assertEqual(res_after_s3["status"], "failed")

        # Status check confirms workflow remains failed with error recorded on s1
        st_fail = json.loads(run_eval(LUA_WORKFLOW, 0, PREFIX, "status", flow_fail_id))
        self.assertEqual(st_fail["status"], "failed")
        self.assertEqual(st_fail["steps"]["s1"]["status"], "failed")
        self.assertEqual(st_fail["steps"]["s1"]["error"], "out of memory")
        self.assertEqual(st_fail["steps"]["s2"]["status"], "completed")
        self.assertEqual(st_fail["steps"]["s3"]["status"], "completed")

        # 9. DAG validation: Mutual cycle detection (a:b;b:a)
        flow_cycle_id = f"flow_cycle_{int(time.time() * 1000)}"
        res_cycle = run_eval(LUA_WORKFLOW, 0, PREFIX, "define", flow_cycle_id, "a,b", "a:b;b:a")
        self.assertIn("Cycle detected", res_cycle)

        # 10. DAG validation: Self-loop cycle detection (a:a)
        flow_self_id = f"flow_self_{int(time.time() * 1000)}"
        res_self = run_eval(LUA_WORKFLOW, 0, PREFIX, "define", flow_self_id, "a", "a:a")
        self.assertIn("Cycle detected", res_self)

        # 11. Dangling dependency validation: undeclared parent step rejected
        flow_dangling_id = f"flow_dangling_{int(time.time() * 1000)}"
        res_dangling = run_eval(LUA_WORKFLOW, 0, PREFIX, "define", flow_dangling_id, "build", "build:compile")
        self.assertIn("Unknown dependency step 'compile'", res_dangling)

    def test_28_sweep_lua_protocol(self):
        """Test sweep.lua auditing and pruning expired heartbeats while preserving living agents and listener metadata."""
        dead_agent = f"dead_bot_{int(time.time() * 1000)}"
        alive_agent = f"alive_bot_{int(time.time() * 1000)}"

        # 1. Argument validation negative controls
        res_no_p = run_eval(LUA_SWEEP, 0, "", "audit", "1")
        self.assertIn("ERR: Missing prefix", res_no_p)

        # 2. Register both agents with distinct multi-tags
        run_redis("SADD", f"{PREFIX}active_agents", dead_agent)
        run_redis("HSET", f"{PREFIX}agent:{dead_agent}", "tags", "sweeptest,backend,python")
        run_redis("SADD", f"{PREFIX}tag:sweeptest", dead_agent)
        run_redis("SADD", f"{PREFIX}tag:backend", dead_agent)
        run_redis("SADD", f"{PREFIX}tag:python", dead_agent)

        run_redis("SADD", f"{PREFIX}active_agents", alive_agent)
        run_redis("HSET", f"{PREFIX}agent:{alive_agent}", "tags", "sweeptest,frontend")
        run_redis("SADD", f"{PREFIX}tag:sweeptest", alive_agent)
        run_redis("SADD", f"{PREFIX}tag:frontend", alive_agent)
        # Give alive_agent a heartbeat key with 60s TTL
        run_redis("SET", f"{PREFIX}heartbeat:{alive_agent}", "1", "EX", "60")
        # dead_agent has NO heartbeat key

        # 3. Dry run audit: should detect dead_agent but NOT mutate any keys
        dry_res = run_eval(LUA_SWEEP, 0, PREFIX, "audit", "1")
        dry_data = json.loads(dry_res)
        self.assertIn(dead_agent, dry_data["dead_agents"])
        self.assertNotIn(alive_agent, dry_data["dead_agents"])
        self.assertTrue(dry_data["dry_run"])

        # Confirm absolute immutability after dry run
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}active_agents", dead_agent), "1")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:{dead_agent}"), "1")
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}tag:sweeptest", dead_agent), "1")
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}tag:backend", dead_agent), "1")
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}tag:python", dead_agent), "1")

        # 4. Prune run: should prune dead_agent from active_agents, delete metadata hash, and remove from ALL tags
        prune_res = run_eval(LUA_SWEEP, 0, PREFIX, "prune", "0")
        prune_data = json.loads(prune_res)
        self.assertIn(dead_agent, prune_data["dead_agents"])
        self.assertFalse(prune_data["dry_run"])

        # Confirm dead_agent is completely purged from keyspace
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}active_agents", dead_agent), "0")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:{dead_agent}"), "0")
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}tag:sweeptest", dead_agent), "0")
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}tag:backend", dead_agent), "0")
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}tag:python", dead_agent), "0")

        # 5. Confirm alive_agent is fully intact and untouched across all sets and keys
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}active_agents", alive_agent), "1")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}agent:{alive_agent}"), "1")
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}tag:sweeptest", alive_agent), "1")
        self.assertEqual(run_redis("SISMEMBER", f"{PREFIX}tag:frontend", alive_agent), "1")
        self.assertEqual(run_redis("EXISTS", f"{PREFIX}heartbeat:{alive_agent}"), "1")
        hb_ttl = int(run_redis("TTL", f"{PREFIX}heartbeat:{alive_agent}"))
        self.assertTrue(hb_ttl > 0, f"Alive agent heartbeat expired prematurely: {hb_ttl}")

        # 6. Cursor-based SCAN listener discovery across multiple listener keys
        l_keys = []
        for idx in range(5):
            lk = f"{PREFIX}listener:agent_scan_{idx}"
            run_redis("SET", lk, json.dumps({"pid": 999000 + idx, "host": "test-host", "started": 12345}))
            l_keys.append(lk)

        scan_res = run_eval(LUA_SWEEP, 0, PREFIX, "audit", "1")
        scan_data = json.loads(scan_res)
        found_agents = [l["agent"] for l in scan_data.get("listeners", [])]
        for idx in range(5):
            self.assertIn(f"agent_scan_{idx}", found_agents)

        # Cleanup listeners
        for lk in l_keys:
            run_redis("DEL", lk)

        # Cleanup living agent
        run_redis("SREM", f"{PREFIX}active_agents", alive_agent)
        run_redis("DEL", f"{PREFIX}agent:{alive_agent}")
        run_redis("DEL", f"{PREFIX}tag:sweeptest")
        run_redis("DEL", f"{PREFIX}heartbeat:{alive_agent}")

    def test_29_lock_fencing_token_lua_protocol(self):
        """Test lock.lua monotonic fencing token generation across repeated lock, unlock, and contention cycles."""
        lock_name = f"fenced_lock_{int(time.time() * 1000)}"
        lock_key = f"{PREFIX}lock:{{{lock_name}}}"
        fencing_key = f"{PREFIX}lock:fencing:{{{lock_name}}}"

        # 1. Argument validation negative controls
        res_no_p = run_eval(LUA_LOCK, 0, "", lock_name, "alice", "10", "1")
        self.assertIn("ERR: Missing prefix", res_no_p)

        res_no_name = run_eval(LUA_LOCK, 0, PREFIX, "", "alice", "10", "1")
        self.assertIn("ERR: Missing lock name", res_no_name)

        res_no_owner = run_eval(LUA_LOCK, 0, PREFIX, lock_name, "", "10", "1")
        self.assertIn("ERR: Missing lock owner", res_no_owner)

        # 2. Cycle 1: Alice acquires with fencing token requested
        tok1 = run_eval(LUA_LOCK, 0, PREFIX, lock_name, "alice", "10", "1")
        self.assertEqual(tok1, "1")
        self.assertEqual(run_redis("GET", lock_key), "alice")
        self.assertEqual(run_redis("GET", fencing_key), "1")

        # 3. Non-owner unlock attempt does not affect lock or fencing token
        res_un_b = run_eval(LUA_UNLOCK, 0, PREFIX, lock_name, "bob")
        self.assertEqual(int(res_un_b), 0)
        self.assertEqual(run_redis("GET", lock_key), "alice")
        self.assertEqual(run_redis("GET", fencing_key), "1")

        # 4. Alice unlocks: lock key is deleted, but fencing counter MUST NOT be reset or deleted!
        res_un1 = run_eval(LUA_UNLOCK, 0, PREFIX, lock_name, "alice")
        self.assertEqual(int(res_un1), 1)
        self.assertEqual(run_redis("EXISTS", lock_key), "0")
        self.assertEqual(run_redis("EXISTS", fencing_key), "1")
        self.assertEqual(run_redis("GET", fencing_key), "1")

        # 5. Cycle 2: Bob acquires with fencing token -> MUST continue monotonically to 2
        tok2 = run_eval(LUA_LOCK, 0, PREFIX, lock_name, "bob", "10", "1")
        self.assertEqual(tok2, "2")
        self.assertEqual(run_redis("GET", lock_key), "bob")
        self.assertEqual(run_redis("GET", fencing_key), "2")

        # 6. Contention failure: Charlie attempts to acquire -> rejected (0)
        # Failed lock acquisition must NOT advance the fencing counter!
        tok_fail = run_eval(LUA_LOCK, 0, PREFIX, lock_name, "charlie", "10", "1")
        self.assertEqual(tok_fail, "0")
        self.assertEqual(run_redis("GET", lock_key), "bob")
        self.assertEqual(run_redis("GET", fencing_key), "2")

        # 7. Bob unlocks: fencing counter preserved at 2
        res_un2 = run_eval(LUA_UNLOCK, 0, PREFIX, lock_name, "bob")
        self.assertEqual(int(res_un2), 1)
        self.assertEqual(run_redis("EXISTS", lock_key), "0")
        self.assertEqual(run_redis("GET", fencing_key), "2")

        # 8. Standard (unfenced) acquisition: returns 1 as success boolean, while Redis fencing counter increments to 3
        res_unfenced = run_eval(LUA_LOCK, 0, PREFIX, lock_name, "charlie", "10", "0")
        self.assertEqual(res_unfenced, "1")
        self.assertEqual(run_redis("GET", lock_key), "charlie")
        self.assertEqual(run_redis("GET", fencing_key), "3")

        # Charlie unlocks
        self.assertEqual(int(run_eval(LUA_UNLOCK, 0, PREFIX, lock_name, "charlie")), 1)
        self.assertEqual(run_redis("GET", fencing_key), "3")

        # 9. Cycle 3: Dan acquires with fencing token -> MUST continue monotonically to 4
        tok4 = run_eval(LUA_LOCK, 0, PREFIX, lock_name, "dan", "10", "1")
        self.assertEqual(tok4, "4")
        self.assertEqual(run_redis("GET", lock_key), "dan")
        self.assertEqual(run_redis("GET", fencing_key), "4")

        # Cleanup
        self.assertEqual(int(run_eval(LUA_UNLOCK, 0, PREFIX, lock_name, "dan")), 1)
        run_redis("DEL", fencing_key)

    def test_30_redis_cluster_hash_tag_slot_affinity(self):
        """Test that all multi-key Lua scripts enclose shared shard roots in {...} for Redis Cluster slot affinity."""
        import re
        hash_tag_pattern = re.compile(r"\{[^}]+\}")

        # Check claim.lua keys
        with open(os.path.join(SCRIPTS_DIR, "claim.lua")) as f:
            claim_src = f.read()
        self.assertIn("queue:{" , claim_src)
        self.assertIn("leases:{" , claim_src)

        # Check floor.lua keys
        with open(os.path.join(SCRIPTS_DIR, "floor.lua")) as f:
            floor_src = f.read()
        self.assertIn("floor:{" , floor_src)

        # Check ballot.lua keys
        with open(os.path.join(SCRIPTS_DIR, "ballot.lua")) as f:
            ballot_src = f.read()
        self.assertIn("ballot:{" , ballot_src)

        # Check workflow.lua keys
        with open(os.path.join(SCRIPTS_DIR, "workflow.lua")) as f:
            wf_src = f.read()
        self.assertIn("workflow:{" , wf_src)

        # Check blackboard.lua keys
        with open(os.path.join(SCRIPTS_DIR, "blackboard.lua")) as f:
            bb_src = f.read()
        self.assertIn("blackboard:{" , bb_src)


if __name__ == "__main__":
    unittest.main()

