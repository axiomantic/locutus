#!/usr/bin/env bash
# scripts/listen.sh
# Air-gapped background listener for Locutus inter-assistant communication bus.
# Intercepts incoming messages, verifies HMAC-SHA256, decrypts if needed,
# and discards forged/tampered messages BEFORE they can enter the LLM's context window.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REDIS_URL="${LOCUTUS_REDIS_URL:-${REDIS_URL:-redis://127.0.0.1:6379}}"
PREFIX="${LOCUTUS_REDIS_PREFIX:-locutus:}"

NAME=""
TIMEOUT=90

if [ $# -ge 1 ]; then
    if [[ "$1" =~ ^[0-9]+$ ]]; then
        TIMEOUT="$1"
    else
        NAME="$1"
        if [ $# -ge 2 ] && [[ "$2" =~ ^[0-9]+$ ]]; then
            TIMEOUT="$2"
        fi
    fi
fi

if [ -z "$NAME" ]; then
    NAME="${LOCUTUS_AGENT_NAME:-${MY_NAME:-}}"
    AGENT_FILE="$HOME/.config/locutus/current_agent"
    if [ -z "$NAME" ] && [ -f "$AGENT_FILE" ]; then
        NAME="$(cat "$AGENT_FILE" 2>/dev/null || true)"
    fi
fi

if [ -z "$NAME" ]; then
    PROJECT="${LOCUTUS_PROJECT:-$(basename "$PWD")}"
    NAME="${PROJECT}-worker"
fi

while true; do
    # Refresh heartbeat so active listener never expires
    redis-cli -u "$REDIS_URL" SET "${PREFIX}heartbeat:${NAME}" "1" EX 150 >/dev/null 2>&1 || true

    RAW=$(redis-cli -u "$REDIS_URL" BRPOP "${PREFIX}inbox:${NAME}" "$TIMEOUT" 2>/dev/null || true)

    if [ -z "$RAW" ] || [ "$RAW" = "(nil)" ]; then
        # Timeout expired with no messages
        exit 0
    fi

    # Extract JSON payload (BRPOP output line 2+)
    JSON_PAYLOAD=$(printf "%s\n" "$RAW" | sed '1d')
    if [ -z "$JSON_PAYLOAD" ]; then
        exit 0
    fi

    # Verify HMAC and decrypt using security.sh helper
    set +e
    VERIFIED=$(python3 -c '
import sys, json, subprocess

script_dir = sys.argv[1]
raw_json = sys.argv[2]

try:
    data = json.loads(raw_json)
except Exception:
    sys.exit(2)

sig = data.get("sig", "")
msg_id = data.get("id", "")
from_agent = data.get("from", "")
to_agent = data.get("to", "")
msg_type = data.get("type", "")
subject = data.get("subject", "")
body = data.get("body", "")
ts = data.get("timestamp", "")
is_enc = data.get("encrypted", False)

if not sig:
    sys.exit(3)

verify_res = subprocess.run([
    f"{script_dir}/security.sh", "verify",
    sig, msg_id, from_agent, to_agent, msg_type, subject, body, ts
], capture_output=True)

if verify_res.returncode != 0:
    sys.exit(4)

if is_enc:
    dec_res = subprocess.run([
        f"{script_dir}/security.sh", "decrypt", body
    ], capture_output=True, text=True)
    if dec_res.returncode != 0:
        sys.exit(5)
    data["body"] = dec_res.stdout
    data["encrypted"] = False

print(json.dumps(data))
sys.exit(0)
' "$SCRIPT_DIR" "$JSON_PAYLOAD" 2>/dev/null)
    EXIT_CODE=$?
    set -e

    if [ $EXIT_CODE -eq 0 ] && [ -n "$VERIFIED" ]; then
        # Signature verified successfully -> deliver to stdout
        printf "%s\n" "$VERIFIED"
        exit 0
    else
        # Signature missing, invalid, or tampered!
        # Drop message completely to prevent prompt injection from reaching LLM context.
        echo "[LOCUTUS SECURITY] Dropped unauthenticated message (invalid or missing HMAC). Discarded before context." >&2
        # If timeout is very short (e.g. <= 1s in tests), exit immediately
        if [ "$TIMEOUT" -le 1 ]; then
            exit 0
        fi
        continue
    fi
done
