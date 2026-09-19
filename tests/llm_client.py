"""
Universal LLM Client for Locutus Integration Tests.
Supports both local Ollama and OpenRouter (or any OpenAI-compatible API) seamlessly.
"""

import json
import os
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_API_BASE = os.environ.get("LLM_API_BASE", "")
if not LLM_API_BASE:
    if LLM_API_KEY:
        LLM_API_BASE = "https://openrouter.ai/api/v1/chat/completions"
    else:
        LLM_API_BASE = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")

if LLM_API_KEY:
    DEFAULT_MODEL = "deepseek/deepseek-chat"
else:
    DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:e4b")

MODEL_NAME = os.environ.get("LLM_MODEL") or DEFAULT_MODEL

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


def is_llm_available() -> bool:
    """Check if an LLM provider (OpenRouter or local Ollama) is reachable."""
    if LLM_API_KEY:
        return True

    check_url = LLM_API_BASE
    if check_url.endswith("/api/chat"):
        check_url = check_url[:-9] + "/api/tags"
    elif check_url.endswith("/v1/chat/completions"):
        check_url = check_url[:-20] + "/v1/models"
    try:
        req = urllib.request.Request(check_url, method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def call_llm(messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Calls either OpenRouter/OpenAI endpoint or Ollama native endpoint.
    Returns (assistant_message_dict, parsed_tool_calls_list).
    """
    active_tools = tools if tools is not None else TOOLS
    headers = {"Content-Type": "application/json"}
    
    is_openrouter_or_openai = bool(LLM_API_KEY) or ("openrouter.ai" in LLM_API_BASE) or ("/v1" in LLM_API_BASE)
    
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"
        headers["HTTP-Referer"] = "https://github.com/axiomantic/locutus"
        headers["X-Title"] = "Locutus Inter-Assistant Bus"

    if is_openrouter_or_openai:
        payload = {
            "model": MODEL_NAME,
            "messages": messages,
            "tools": active_tools,
            "temperature": 0.1
        }
    else:
        # Native Ollama /api/chat payload
        payload = {
            "model": MODEL_NAME,
            "messages": messages,
            "tools": active_tools,
            "stream": False,
            "options": {
                "temperature": 0.1
            }
        }

    endpoint = LLM_API_BASE
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers
    )
    
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8")
            except Exception:
                pass
            print(f"[LLM ERROR] HTTP {e.code} on attempt {attempt}/{max_retries}: {error_body}")
            if attempt == max_retries or e.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"LLM API request failed with HTTP {e.code}: {error_body}") from e
            import time
            sleep_sec = attempt * 2
            print(f"[LLM RETRY] Waiting {sleep_sec}s before retry...")
            time.sleep(sleep_sec)
        except Exception as e:
            print(f"[LLM ERROR] Exception on attempt {attempt}/{max_retries}: {e}")
            if attempt == max_retries:
                raise
            import time
            time.sleep(attempt * 2)

    # Extract message from OpenAI/OpenRouter choices or Ollama root
    if "choices" in data and len(data["choices"]) > 0:
        msg = data["choices"][0].get("message", {})
    else:
        msg = data.get("message", {})

    tool_calls = msg.get("tool_calls") or []
    return msg, tool_calls

