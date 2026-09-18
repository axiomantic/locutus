#!/usr/bin/env python3
"""
Deterministic Unit Tests for Redis A2A Embedded Lua Scripts and Protocol.
Tests registration, heartbeats, O2O queueing, multicast fan-out, dead-agent pruning,
and backlog batch draining directly using redis-cli commands.
"""

import json
import subprocess
import time
import unittest

REDIS_URL = "redis://127.0.0.1:6379"
PREFIX = "a2a_test:"

# Lua Scripts extracted directly from SKILL.md
LUA_REGISTER = """
local prefix = ARGV[1]
local name = ARGV[2]
local tags_csv = ARGV[3] or ""
local ttl = tonumber(ARGV[4]) or 150
local now = redis.call('TIME')[1]

redis.call('SET', prefix .. 'heartbeat:' .. name, '1', 'EX', ttl)
redis.call('SADD', prefix .. 'active_agents', name)
redis.call('HSET', prefix .. 'agent:' .. name, 'tags', tags_csv, 'last_seen', now)

for tag in string.gmatch(tags_csv, "([^,]+)") do
    local trimmed = string.match(tag, "^%s*(.-)%s*$")
    if trimmed ~= "" then
        redis.call('SADD', prefix .. 'tag:' .. trimmed, name)
    end
end
return "OK"
"""

LUA_SEND_O2O = """
local prefix = ARGV[1]
local recipient = ARGV[2]
local msg_json = ARGV[3]
local inbox_ttl = tonumber(ARGV[4]) or 604800

redis.call('LPUSH', prefix .. 'inbox:' .. recipient, msg_json)
redis.call('EXPIRE', prefix .. 'inbox:' .. recipient, inbox_ttl)
return "OK"
"""

LUA_MULTICAST = """
local prefix = ARGV[1]
local target_tag = ARGV[2]
local msg_json = ARGV[3]
local targets = {}

if target_tag == "*" then
    targets = redis.call('SMEMBERS', prefix .. 'active_agents')
else
    targets = redis.call('SMEMBERS', prefix .. 'tag:' .. target_tag)
end

local delivered = 0
for _, agent in ipairs(targets) do
    if redis.call('EXISTS', prefix .. 'heartbeat:' .. agent) == 1 then
        redis.call('LPUSH', prefix .. 'inbox:' .. agent, msg_json)
        redis.call('EXPIRE', prefix .. 'inbox:' .. agent, 604800)
        delivered = delivered + 1
    else
        if target_tag == "*" then
            redis.call('SREM', prefix .. 'active_agents', agent)
        else
            redis.call('SREM', prefix .. 'tag:' .. target_tag, agent)
        end
    end
end
return delivered
"""

LUA_DRAIN = """
local prefix = ARGV[1]
local name = ARGV[2]
local count = tonumber(ARGV[3]) or 50
local messages = {}

for i = 1, count do
    local msg = redis.call('RPOP', prefix .. 'inbox:' .. name)
    if not msg then break end
    table.insert(messages, msg)
end
return messages
"""

LUA_DIRECTORY = """
local prefix = ARGV[1]
local agents = redis.call('SMEMBERS', prefix .. 'active_agents')
local result = {}

for _, agent in ipairs(agents) do
    local alive = redis.call('EXISTS', prefix .. 'heartbeat:' .. agent)
    local tags = redis.call('HGET', prefix .. 'agent:' .. agent, 'tags') or ""
    table.insert(result, agent .. "|" .. tostring(alive) .. "|" .. tags)
end
return result
"""

LUA_UNREGISTER = """
local prefix = ARGV[1]
local name = ARGV[2]
local tags_csv = redis.call('HGET', prefix .. 'agent:' .. name, 'tags') or ""

redis.call('DEL', prefix .. 'heartbeat:' .. name)
redis.call('SREM', prefix .. 'active_agents', name)
redis.call('DEL', prefix .. 'agent:' .. name)

for tag in string.gmatch(tags_csv, "([^,]+)") do
    local trimmed = string.match(tag, "^%s*(.-)%s*$")
    if trimmed ~= "" then
        redis.call('SREM', prefix .. 'tag:' .. trimmed, name)
    end
end
return "OK"
"""

def run_redis(*args):
    cmd = ["redis-cli", "-u", REDIS_URL] + list(args)
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return res.stdout.strip()

def run_eval(script, numkeys, *args):
    cmd = ["redis-cli", "-u", REDIS_URL, "EVAL", script, str(numkeys)] + list(args)
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

    def test_registration_and_directory(self):
        # Register alice with tags worker,math
        res = run_eval(LUA_REGISTER, 0, PREFIX, "alice", "worker,math", "120")
        self.assertEqual(res, "OK")

        # Verify heartbeat exists
        hb = run_redis("GET", f"{PREFIX}heartbeat:alice")
        self.assertEqual(hb, "1")

        # Verify active roster
        roster = run_redis("SMEMBERS", f"{PREFIX}active_agents")
        self.assertIn("alice", roster)

        # Verify tag indexing
        math_tag = run_redis("SMEMBERS", f"{PREFIX}tag:math")
        self.assertIn("alice", math_tag)

        # Query directory
        directory = run_eval(LUA_DIRECTORY, 0, PREFIX)
        self.assertIn("alice|1|worker,math", directory)

    def test_direct_o2o_send_and_drain(self):
        # Alice registers
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "worker", "120")

        # Bob sends message to Alice
        msg = json.dumps({"id": "msg_001", "from": "bob", "to": "alice", "body": "hello"})
        res = run_eval(LUA_SEND_O2O, 0, PREFIX, "alice", msg, "604800")
        self.assertEqual(res, "OK")

        # Verify message queued in inbox
        inbox_len = run_redis("LLEN", f"{PREFIX}inbox:alice")
        self.assertEqual(inbox_len, "1")

        # Alice drains message
        drained = run_eval(LUA_DRAIN, 0, PREFIX, "alice", "10")
        self.assertIn("msg_001", drained)

        # Inbox is now empty
        inbox_len_after = run_redis("LLEN", f"{PREFIX}inbox:alice")
        self.assertEqual(inbox_len_after, "0")

    def test_multicast_with_automatic_dead_agent_pruning(self):
        # Register alice (alive) with tag 'qa'
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "qa", "120")

        # Register charlie (ghost/dead) with tag 'qa', then delete his heartbeat
        run_eval(LUA_REGISTER, 0, PREFIX, "charlie", "qa", "120")
        run_redis("DEL", f"{PREFIX}heartbeat:charlie")

        # Verify both in tag set before multicast
        qa_agents = run_redis("SMEMBERS", f"{PREFIX}tag:qa")
        self.assertIn("alice", qa_agents)
        self.assertIn("charlie", qa_agents)

        # Multicast to tag 'qa'
        msg = json.dumps({"id": "msg_qa_01", "from": "lead", "to": "@qa", "body": "run tests"})
        delivered_count = run_eval(LUA_MULTICAST, 0, PREFIX, "qa", msg)
        
        # Only 1 delivered (to alice)
        self.assertIn(delivered_count, ["1", "(integer) 1"])

        # Verify alice received it
        alice_inbox = run_redis("LLEN", f"{PREFIX}inbox:alice")
        self.assertEqual(alice_inbox, "1")

        # Verify charlie did NOT receive it
        charlie_inbox = run_redis("LLEN", f"{PREFIX}inbox:charlie")
        self.assertEqual(charlie_inbox, "0")

        # Verify charlie was automatically pruned from tag:qa set!
        qa_agents_after = run_redis("SMEMBERS", f"{PREFIX}tag:qa")
        self.assertIn("alice", qa_agents_after)
        self.assertNotIn("charlie", qa_agents_after)

    def test_unregister(self):
        run_eval(LUA_REGISTER, 0, PREFIX, "alice", "math", "120")
        run_eval(LUA_UNREGISTER, 0, PREFIX, "alice")

        roster = run_redis("SMEMBERS", f"{PREFIX}active_agents")
        self.assertNotIn("alice", roster)

        tag = run_redis("SMEMBERS", f"{PREFIX}tag:math")
        self.assertNotIn("alice", tag)

        hb = run_redis("EXISTS", f"{PREFIX}heartbeat:alice")
        self.assertEqual(hb, "0")


if __name__ == "__main__":
    unittest.main()
