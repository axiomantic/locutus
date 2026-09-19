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
import time
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

def main():
    print(f"Starting Locutus Ollama Agent Test using model '{MODEL_NAME}'...")

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
    print(f"Alice heartbeat: {hb}")
    assert hb == "1", f"Expected Alice heartbeat to be '1', got '{hb}'"
    print("✓ Alice heartbeat is active ('1')")

    inbox_len = redis_cmd("LLEN", "locutus:inbox:bob")
    print(f"Bob inbox length: {inbox_len}")
    assert inbox_len and int(inbox_len) > 0, "Bob inbox is empty! No message delivered."
    print("✓ Bob inbox has pending message(s)")

    raw_msg = redis_cmd("RPOP", "locutus:inbox:bob")
    print(f"\nRaw Message Received:\n{raw_msg}")

    try:
        msg = LocutusMessage.model_validate_json(raw_msg)
        print("✓ Pydantic LocutusMessage schema validation passed (id, types, envelope, timestamp)!")
    except Exception as e:
        print(f"FAILURE: Message violates Pydantic LocutusMessage schema: {e}")
        return 1

    # Semantic Content Validation
    assert msg.from_agent == "alice", f"Expected from='alice', got '{msg.from_agent}'"
    print("✓ Sender is 'alice'")
    assert msg.to_agent == "bob", f"Expected to='bob', got '{msg.to_agent}'"
    print("✓ Recipient is 'bob'")
    assert msg.type == "task", f"Expected type='task', got '{msg.type}'"
    print("✓ Protocol type is 'task'")
    assert "Math Task" in msg.subject or "math" in msg.subject.lower(), f"Subject unexpected: '{msg.subject}'"
    print(f"✓ Subject matches: '{msg.subject}'")
    assert "25" in msg.body and "4" in msg.body, f"Body unexpected: '{msg.body}'"
    print(f"✓ Body contains expected task calculation: '{msg.body}'")
    print(f"✓ Envelope metadata valid (id='{msg.id}', timestamp='{msg.timestamp}')")

    print("\nALL CONTENT AND ENVELOPE CHECKS PASSED VIA PYDANTIC!")
    return 0

if __name__ == "__main__":
    sys.exit(main())
