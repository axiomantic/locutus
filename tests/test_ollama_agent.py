#!/usr/bin/env python3
"""
Integration test using a local Ollama model to test the Redis A2A Skill end-to-end.
Tests that a local LLM given SKILL.md and a bash tool can:
1. Register on Redis with tags
2. Send a structured JSON task message via LUA_SEND_O2O
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
MODEL_NAME = os.environ.get("OLLAMA_MODEL", "gemma4:e4b")

SKILL_PATH = os.path.expanduser("/Users/eek/Development/locutus/SKILL.md")

with open(SKILL_PATH, "r") as f:
    SYSTEM_PROMPT = f.read()

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
        env.setdefault("A2A_REDIS_URL", "redis://127.0.0.1:6379")
        env.setdefault("REDIS_URL", "redis://127.0.0.1:6379")
        env.setdefault("A2A_REDIS_PREFIX", "a2a:")
        env.setdefault("A2A_PREFIX", "a2a:")
        env.setdefault("A2A_SCRIPTS_DIR", os.path.abspath("scripts"))
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
            "temperature": 0.2
        }
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))

def main():
    print(f"Starting A2A Ollama Agent Test using model '{MODEL_NAME}'...")

    # Flush test recipient inbox
    subprocess.run(["redis-cli", "DEL", "a2a:inbox:bob", "a2a:heartbeat:alice", "a2a:agent:alice"], check=False)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "You are agent 'alice'.\n"
                "Please follow the redis-a2a protocol instructions:\n"
                "1. Check if Redis is running, and register as 'alice' with tag 'calc'.\n"
                "2. Send a direct task message to 'bob' with subject 'Math Task' asking him to compute '25 * 4'.\n"
                "Execute the bash commands to do this."
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
    hb = subprocess.run(["redis-cli", "GET", "a2a:heartbeat:alice"], capture_output=True, text=True).stdout.strip()
    print(f"Alice heartbeat: {hb}")
    assert hb == "1", f"Expected Alice heartbeat to be '1', got '{hb}'"
    print("✓ Alice heartbeat is active ('1')")

    inbox_len = subprocess.run(["redis-cli", "LLEN", "a2a:inbox:bob"], capture_output=True, text=True).stdout.strip()
    print(f"Bob inbox length: {inbox_len}")
    assert inbox_len and int(inbox_len) > 0, "Bob inbox is empty! No message delivered."
    print("✓ Bob inbox has pending message(s)")

    raw_msg = subprocess.run(["redis-cli", "RPOP", "a2a:inbox:bob"], capture_output=True, text=True).stdout.strip()
    print(f"\nRaw Message Received:\n{raw_msg}")

    try:
        msg = json.loads(raw_msg)
    except json.JSONDecodeError as e:
        print(f"FAILURE: Message is not valid JSON: {e}")
        return 1

    # 1. Verify Sender and Recipient
    assert msg.get("from") == "alice", f"Expected from='alice', got '{msg.get('from')}'"
    print("✓ Sender is 'alice'")
    assert msg.get("to") == "bob", f"Expected to='bob', got '{msg.get('to')}'"
    print("✓ Recipient is 'bob'")

    # 2. Verify Protocol Message Type
    assert msg.get("type") == "task", f"Expected type='task', got '{msg.get('type')}'"
    print("✓ Protocol type is 'task'")

    # 3. Verify Subject and Body Content
    subject = msg.get("subject", "")
    assert "Math Task" in subject or "math" in subject.lower(), f"Subject unexpected: '{subject}'"
    print(f"✓ Subject matches: '{subject}'")

    body = msg.get("body", "")
    assert "25" in body and "4" in body, f"Body does not contain expected calculation (25 * 4): '{body}'"
    print(f"✓ Body contains expected task instructions: '{body}'")

    # 4. Verify Envelope Metadata
    msg_id = msg.get("id")
    assert msg_id and len(msg_id) > 0, "Message ID is missing"
    timestamp = msg.get("timestamp")
    assert timestamp and len(timestamp) > 0, "Timestamp is missing"
    print(f"✓ Envelope metadata valid (id='{msg_id}', timestamp='{timestamp}')")

    print("\nALL CONTENT AND ENVELOPE CHECKS PASSED!")
    return 0

if __name__ == "__main__":
    sys.exit(main())
