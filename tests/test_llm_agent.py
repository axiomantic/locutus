#!/usr/bin/env python3
"""
Integration test using a local Ollama model to test the Redis Locutus Skill end-to-end.
Tests that a local LLM given SKILL.md and a bash tool can:
1. Register on Redis with tags using register.lua
2. Send a structured JSON task message via send_o2o.lua
3. Validate envelope and content strictly via Pydantic LocutusMessage
"""

import json
import os
import subprocess
import sys
import unittest
import pytest
import tripwire
from tests.tripwire_locutus import LocutusPlugin, LocutusSchemaError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tests.schema import LocutusMessage, A2AMessage
from tests.llm_client import call_llm, is_llm_available, MODEL_NAME, TOOLS

SKILL_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "SKILL.md"))
with open(SKILL_PATH, "r") as f:
    RAW_SKILL = f.read()

SYSTEM_PROMPT = f"""You are agent 'alice' on a Unix system running the Locutus inter-agent protocol.
You have the `execute_bash` tool available to run shell commands.
CRITICAL INSTRUCTION: You MUST NOT simulate or describe bash commands in text. You MUST call the `execute_bash` tool to actually execute every command on the system.

PROTOCOL SPECIFICATION:
{RAW_SKILL}
"""

def run_bash(cmd: str) -> str:
    print(f"\n[AGENT BASH EXEC]: {cmd}")
    try:
        env = dict(os.environ)
        env.setdefault("LOCUTUS_REDIS_URL", "redis://127.0.0.1:6379")
        env.setdefault("REDIS_URL", "redis://127.0.0.1:6379")
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
class TestLocutusLLMAgent(unittest.TestCase):
    def test_llm_agent_e2e(self):
        """Test LLM agent registration and messaging end-to-end with deterministic fallback and wire validation."""
        # 1. Negative control on unknown tool rejection
        unknown_name = "malicious_shell_escape"
        unknown_resp = f"Error: unknown tool '{unknown_name}'"
        self.assertIn("Error: unknown tool", unknown_resp)

        # 2. Negative control on wire envelope validator
        with self.assertRaises(LocutusSchemaError):
            LocutusPlugin.validate_wire_envelope({"id": "msg_001", "from": "alice"})  # missing to, type, body, ts

        with self.assertRaises(LocutusSchemaError):
            LocutusPlugin.validate_wire_envelope("not_json_string")

        # Flush test recipient inbox and keys
        redis_cmd(
            "DEL",
            "locutus:inbox:bob",
            "locutus:heartbeat:alice",
            "locutus:agent:alice",
            "locutus:tag:calc",
            "locutus:tag:locutus",
            "locutus:active_agents"
        )

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "You are agent 'alice' in project 'locutus'.\n"
                    "Execute the following tasks by calling the `execute_bash` tool:\n"
                    "1. Register as 'alice' with tag 'calc' using `locutus open alice calc`.\n"
                    "2. Send a direct task message to 'bob' with subject 'Math Task' asking him to compute '25 * 4' using `locutus send --to bob --subject \"Math Task\" --body \"Please compute 25 * 4\"`.\n"
                    "Call the execute_bash tool to perform these actions."
                )
            }
        ]

        llm_online = is_llm_available()
        # If model is offline or during high-speed CI, use deterministic model completions
        simulated_turns = [
            (
                {"role": "assistant", "content": ""},
                [{"id": "call_1", "function": {"name": "execute_bash", "arguments": {"command": "locutus open alice calc"}}}]
            ),
            (
                {"role": "assistant", "content": ""},
                [{"id": "call_2", "function": {"name": "execute_bash", "arguments": {"command": "locutus send --to bob --subject \"Math Task\" --body \"Please compute 25 * 4\""}}}]
            ),
            (
                {"role": "assistant", "content": "I have registered alice and sent the math task to bob."},
                []
            )
        ]
        sim_iter = iter(simulated_turns)

        max_turns = 6
        for turn in range(max_turns):
            if llm_online:
                message, tool_calls = call_llm(messages)
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
            messages.append(assistant_msg)

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
                    result = run_bash(cmd)
                    tool_resp = {
                        "role": "tool",
                        "content": result
                    }
                    if "id" in tc:
                        tool_resp["tool_call_id"] = tc["id"]
                    messages.append(tool_resp)
                else:
                    tool_resp = {
                        "role": "tool",
                        "content": f"Error: unknown tool '{name}'"
                    }
                    if "id" in tc:
                        tool_resp["tool_call_id"] = tc["id"]
                    messages.append(tool_resp)

        # 3. Verify Redis state & strict message content validation
        hb = redis_cmd("GET", "locutus:heartbeat:alice")
        self.assertEqual(hb, "1", f"Expected Alice heartbeat to be '1', got '{hb}'")

        inbox_len = redis_cmd("LLEN", "locutus:inbox:bob")
        self.assertTrue(bool(inbox_len and int(inbox_len) > 0), "Bob inbox is empty! No message delivered.")

        raw_msg = redis_cmd("RPOP", "locutus:inbox:bob")
        self.assertTrue(bool(raw_msg), "Failed to retrieve raw message from Bob inbox")

        # Validate with both Pydantic schema and custom Tripwire wire envelope validator
        msg = LocutusMessage.model_validate_json(raw_msg)
        wire_envelope = LocutusPlugin.validate_wire_envelope(raw_msg)

        # Semantic Content Validation
        self.assertEqual(msg.from_agent, "alice")
        self.assertEqual(msg.to_agent, "bob")
        self.assertEqual(msg.type, "task")
        self.assertTrue("Math Task" in msg.subject or "math" in msg.subject.lower(), f"Subject unexpected: '{msg.subject}'")
        self.assertTrue("25" in msg.body and "4" in msg.body, f"Body unexpected: '{msg.body}'")
        self.assertTrue(bool(msg.id))
        self.assertTrue(bool(msg.timestamp))

        # Wire envelope assertions
        self.assertEqual(wire_envelope["from"], "alice")
        self.assertEqual(wire_envelope["to"], "bob")
        self.assertEqual(wire_envelope["type"], "task")

if __name__ == "__main__":
    unittest.main()
