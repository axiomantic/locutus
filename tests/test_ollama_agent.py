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
import urllib.request

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tests.schema import LocutusMessage, A2AMessage

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
MODEL_NAME = os.environ.get("OLLAMA_MODEL", "gemma4:e4b")

SKILL_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "SKILL.md"))
with open(SKILL_PATH, "r") as f:
    RAW_SKILL = f.read()

SYSTEM_PROMPT = f"""You are agent 'alice' on a Unix system running the Locutus inter-agent protocol.
You have the `execute_bash` tool available to run shell commands.
CRITICAL INSTRUCTION: You MUST NOT simulate or describe bash commands in text. You MUST call the `execute_bash` tool to actually execute every command on the system.

PROTOCOL SPECIFICATION:
{RAW_SKILL}
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_bash",
            "description": "Execute a bash shell command on the system and return stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The exact bash command to execute"
                    }
                },
                "required": ["command"]
            }
        }
    }
]

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

def call_ollama(messages):
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "tools": TOOLS,
        "stream": False,
        "options": {
            "temperature": 0.1
        }
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))

REDIS_URL = os.environ.get("LOCUTUS_REDIS_URL", os.environ.get("REDIS_URL", "redis://127.0.0.1:6379"))

def redis_cmd(*args):
    return subprocess.run(["redis-cli", "-u", REDIS_URL] + list(args), capture_output=True, text=True).stdout.strip()

def is_ollama_available() -> bool:
    if os.environ.get("RUN_LLM_TESTS") != "1":
        return False
    check_url = OLLAMA_URL
    if check_url.endswith("/api/chat"):
        check_url = check_url[:-9] + "/api/tags"
    try:
        req = urllib.request.Request(check_url, method="GET")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            return resp.status == 200
    except Exception:
        return False

class TestLocutusOllamaAgent(unittest.TestCase):
    @unittest.skipUnless(is_ollama_available(), "Requires local Ollama service and RUN_LLM_TESTS=1")
    def test_ollama_agent_e2e(self):
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

        max_turns = 6
        for turn in range(max_turns):
            print(f"\n--- Turn {turn + 1} ---")
            resp = call_ollama(messages)
            message = resp.get("message", {})
            messages.append(message)

            tool_calls = message.get("tool_calls", [])
            if not tool_calls:
                print(f"\n[AGENT FINAL RESPONSE]:\n{message.get('content')}")
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
                    messages.append({
                        "role": "tool",
                        "content": result
                    })

        # Verify Redis state & strict message content validation
        print("\n--- Verifying Redis State & Message Content ---")
        hb = redis_cmd("GET", "locutus:heartbeat:alice")
        self.assertEqual(hb, "1", f"Expected Alice heartbeat to be '1', got '{hb}'")

        inbox_len = redis_cmd("LLEN", "locutus:inbox:bob")
        self.assertTrue(bool(inbox_len and int(inbox_len) > 0), "Bob inbox is empty! No message delivered.")

        raw_msg = redis_cmd("RPOP", "locutus:inbox:bob")
        self.assertTrue(bool(raw_msg), "Failed to retrieve raw message from Bob inbox")

        msg = LocutusMessage.model_validate_json(raw_msg)

        # Semantic Content Validation
        self.assertEqual(msg.from_agent, "alice")
        self.assertEqual(msg.to_agent, "bob")
        self.assertEqual(msg.type, "task")
        self.assertTrue("Math Task" in msg.subject or "math" in msg.subject.lower(), f"Subject unexpected: '{msg.subject}'")
        self.assertTrue("25" in msg.body and "4" in msg.body, f"Body unexpected: '{msg.body}'")
        self.assertTrue(bool(msg.id))
        self.assertTrue(bool(msg.timestamp))

if __name__ == "__main__":
    unittest.main()
