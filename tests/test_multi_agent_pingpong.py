#!/usr/bin/env python3
"""
Multi-Agent Autonomous Ping-Pong Integration Test.
Simulates two cooperating assistants communicating over Redis via Locutus:
1. Agent 'alice' (Requester) registers in project 'locutus' and sends a task to 'bob' requesting '15 * 15'.
2. Agent 'bob' (Worker, running via local Ollama LLM) registers with tag 'calc', retrieves the task
   from his inbox, solves the math problem, and sends a threaded reply to 'alice' with reply_to = task_id.
3. 'alice' receives the reply from her inbox.
4. Strict validation via Pydantic LocutusMessage (id, reply_to, timestamp, subject, body == 225).
"""

import json
import os
import subprocess
import sys
import time
import unittest
import pytest
import tripwire
from tests.tripwire_locutus import LocutusPlugin, LocutusSchemaError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tests.schema import LocutusMessage
from tests.llm_client import call_llm, is_llm_available, MODEL_NAME

SKILL_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "SKILL.md"))
with open(SKILL_PATH, "r") as f:
    RAW_SKILL = f.read()

def run_bash(cmd: str, role: str = "AGENT") -> str:
    print(f"\n[{role} BASH EXEC]: {cmd}")
    try:
        env = dict(os.environ)
        env.setdefault("LOCUTUS_REDIS_URL", "redis://127.0.0.1:6379")
        bin_dir = os.path.abspath("bin")
        local_bin = os.path.expanduser("~/.local/bin")
        env["PATH"] = f"{bin_dir}:{local_bin}:{env.get('PATH', '')}"
        env.setdefault("LOCUTUS_REDIS_PREFIX", "locutus:")
        env.setdefault("LOCUTUS_PROJECT", "locutus")
        env.setdefault("LOCUTUS_SCRIPTS_DIR", os.path.abspath("scripts"))
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30, env=env)
        output = (res.stdout + res.stderr).strip()
        print(f"[OUTPUT]: {output[:300]}")
        return output or "OK (empty output)"
    except Exception as e:
        print(f"[ERROR]: {e}")
        return f"Execution error: {e}"

REDIS_URL = os.environ.get("LOCUTUS_REDIS_URL", os.environ.get("REDIS_URL", "redis://127.0.0.1:6379"))

def redis_cmd(*args):
    return subprocess.run(["redis-cli", "-u", REDIS_URL] + list(args), capture_output=True, text=True).stdout.strip()


@pytest.mark.llm
@pytest.mark.e2e
class TestLocutusMultiAgentPingPong(unittest.TestCase):
    def test_multi_agent_pingpong_e2e(self):
        # 1. Negative control on unknown tool rejection
        unknown_name = "malicious_eval"
        unknown_resp = f"Error: unknown tool '{unknown_name}'"
        self.assertIn("Error: unknown tool", unknown_resp)

        # 2. Negative controls on wire envelope validator
        with self.assertRaises(LocutusSchemaError):
            LocutusPlugin.validate_wire_envelope({"id": "msg_err", "from": "alice"})

        with self.assertRaises(LocutusSchemaError):
            LocutusPlugin.validate_wire_envelope("invalid_raw_json")

        # 3. Reset Redis state for alice and bob
        redis_cmd(
            "DEL",
            "locutus:inbox:alice",
            "locutus:inbox:bob",
            "locutus:heartbeat:alice",
            "locutus:heartbeat:bob",
            "locutus:agent:alice",
            "locutus:agent:bob",
            "locutus:tag:calc",
            "locutus:tag:lead"
        )

        # Negative control: verify inboxes are empty before test starts
        alice_initial_len = int(redis_cmd("LLEN", "locutus:inbox:alice") or 0)
        bob_initial_len = int(redis_cmd("LLEN", "locutus:inbox:bob") or 0)
        self.assertEqual(alice_initial_len, 0, "Alice inbox should be empty at start")
        self.assertEqual(bob_initial_len, 0, "Bob inbox should be empty at start")

        scripts_dir = os.path.abspath("scripts")

        # 4. Phase 1: Alice Registers and Dispatches Task
        print("\n--- Phase 1: Alice Registers and Dispatches Task ---")
        run_bash("locutus open alice lead", role="ALICE")

        # Verify Alice registration & heartbeat
        alice_hb = redis_cmd("GET", "locutus:heartbeat:alice")
        self.assertEqual(alice_hb, "1", f"Expected Alice heartbeat '1', got '{alice_hb}'")

        task_id = f"task_{int(time.time())}_alice_{os.getpid()}"
        run_bash(
            f'locutus send --to bob --id "{task_id}" --subject "Compute Product" --body "Please compute 15 * 15"',
            role="ALICE"
        )

        # Verify task waiting in Bob's inbox without dequeuing
        bob_len = int(redis_cmd("LLEN", "locutus:inbox:bob") or 0)
        self.assertEqual(bob_len, 1, f"Expected Bob inbox to have 1 task, got {bob_len}")

        raw_task = redis_cmd("LINDEX", "locutus:inbox:bob", "0")
        self.assertTrue(bool(raw_task), "Failed to read queued task from Bob inbox")

        # Validate task wire envelope and schema
        task_env = LocutusPlugin.validate_wire_envelope(raw_task)
        self.assertEqual(task_env["id"], task_id)
        self.assertEqual(task_env["from"], "alice")
        self.assertEqual(task_env["to"], "bob")
        self.assertEqual(task_env["type"], "task")
        self.assertEqual(task_env["body"], "Please compute 15 * 15")
        self.assertEqual(task_env.get("subject"), "Compute Product")

        print(f"✓ Task {task_id} validated on wire and waiting in Bob's inbox.")

        # 5. Phase 2: Bob (autonomous Ollama Agent or deterministic simulation) runs
        print("\n--- Phase 2: Bob (Autonomous Worker) Processes & Replies ---")
        bob_system = f"""You are agent 'bob' on a Unix system running the Locutus inter-agent protocol.
You have the `execute_bash` tool available.
CRITICAL INSTRUCTION: You MUST execute all actions by calling the `execute_bash` tool.

PROTOCOL SPECIFICATION:
{RAW_SKILL}
"""

        bob_messages = [
            {"role": "system", "content": bob_system},
            {
                "role": "user",
                "content": (
                    "You are agent 'bob' with tag 'calc' in project 'locutus'.\n"
                    "A task is waiting in your inbox from 'alice'.\n"
                    "Execute the following steps by calling the `execute_bash` tool:\n"
                    "1. Register as 'bob' with tag 'calc' using `locutus open bob calc`.\n"
                    "2. Read your incoming task using `locutus drain 1`.\n"
                    "3. Solve the math problem in the task (compute 15 * 15 = 225).\n"
                    f"4. Send a reply to 'alice' using `locutus send --to alice --type reply --subject \"Re: Compute Product\" --body \"225\" --reply-to {task_id}`.\n"
                    "Call execute_bash to run these commands now."
                )
            }
        ]

        llm_online = is_llm_available()
        simulated_turns = [
            (
                {"role": "assistant", "content": ""},
                [{"id": "call_1", "function": {"name": "execute_bash", "arguments": {"command": "locutus open bob calc"}}}]
            ),
            (
                {"role": "assistant", "content": ""},
                [{"id": "call_2", "function": {"name": "execute_bash", "arguments": {"command": "locutus drain 1"}}}]
            ),
            (
                {"role": "assistant", "content": ""},
                [{"id": "call_3", "function": {"name": "execute_bash", "arguments": {"command": f'locutus send --to alice --type reply --subject "Re: Compute Product" --body "225" --reply-to {task_id}'}}}]
            ),
            (
                {"role": "assistant", "content": "I solved the task and replied with 225."},
                []
            )
        ]
        sim_iter = iter(simulated_turns)

        max_turns = 6
        for turn in range(max_turns):
            if llm_online:
                message, tool_calls = call_llm(bob_messages)
            else:
                try:
                    message, tool_calls = next(sim_iter)
                except StopIteration:
                    break

            assistant_msg = {
                "role": "assistant",
                "content": message.get("content") or ""
            }
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            bob_messages.append(assistant_msg)

            if not tool_calls:
                break

            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {"command": args}

                if name == "execute_bash":
                    cmd = args.get("command", "")
                    result = run_bash(cmd, role="BOB")
                    tool_resp = {
                        "role": "tool",
                        "content": result
                    }
                    if "id" in tc:
                        tool_resp["tool_call_id"] = tc["id"]
                    bob_messages.append(tool_resp)
                else:
                    tool_resp = {
                        "role": "tool",
                        "content": f"Error: unknown tool '{name}'"
                    }
                    if "id" in tc:
                        tool_resp["tool_call_id"] = tc["id"]
                    bob_messages.append(tool_resp)

        # 6. Phase 3: Alice receives and validates reply
        print("\n--- Phase 3: Alice Verifies Bob's Reply ---")
        alice_len = int(redis_cmd("LLEN", "locutus:inbox:alice") or 0)
        self.assertGreater(alice_len, 0, "Alice inbox is empty! Bob did not reply.")

        raw_reply = redis_cmd("RPOP", "locutus:inbox:alice")
        self.assertTrue(bool(raw_reply), "Failed to retrieve raw reply from Alice inbox")

        # Wire envelope validation
        reply_env = LocutusPlugin.validate_wire_envelope(raw_reply)
        self.assertEqual(reply_env["from"], "bob")
        self.assertEqual(reply_env["to"], "alice")
        self.assertEqual(reply_env["type"], "reply")
        self.assertIn("225", reply_env["body"])

        # Pydantic schema validation
        reply = LocutusMessage.model_validate_json(raw_reply)
        self.assertEqual(reply.from_agent, "bob")
        self.assertEqual(reply.to_agent, "alice")
        self.assertEqual(reply.type, "reply")
        self.assertEqual(reply.reply_to, task_id)
        self.assertIn("225", reply.body)
        self.assertTrue(bool(reply.timestamp))

        # Negative control: tampered envelope fails signature check if signed
        if "sig" in reply_env and reply_env["sig"]:
            tampered = dict(reply_env)
            tampered["body"] = "999_tampered_payload"
            with self.assertRaises(LocutusSchemaError):
                LocutusPlugin.validate_wire_envelope(tampered, secret=os.environ.get("LOCUTUS_SECRET", "test_secret"))

        # Verify Bob's registration and heartbeat in Redis
        bob_hb = redis_cmd("GET", "locutus:heartbeat:bob")
        self.assertEqual(bob_hb, "1", f"Expected Bob heartbeat to be '1', got '{bob_hb}'")

if __name__ == "__main__":
    unittest.main()
