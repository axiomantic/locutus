#!/usr/bin/env bash
# scripts/send.sh
# Secure message dispatcher for Locutus inter-assistant communication bus.
# Automatically stamps HMAC-SHA256 signature and optionally encrypts payload.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REDIS_URL="${LOCUTUS_REDIS_URL:-${REDIS_URL:-redis://127.0.0.1:6379}}"
PREFIX="${LOCUTUS_REDIS_PREFIX:-locutus:}"
PROJECT="${LOCUTUS_PROJECT:-$(basename "$PWD")}"

RECIPIENT="${1:-}"
TYPE="${2:-task}"
SUBJECT="${3:-}"
BODY="${4:-}"
TAGS="${5:-$PROJECT}"
REPLY_TO="${6:-}"
MSG_ID="${7:-}"
TIMESTAMP="${8:-}"

if [ -z "$RECIPIENT" ] || [ -z "$SUBJECT" ] || [ -z "$BODY" ]; then
    echo "Usage: $0 <recipient> <type> <subject> <body> [tags] [reply_to] [id] [timestamp]" >&2
    exit 1
fi

FROM="${MY_NAME:-${USER:-agent}}"
[ -z "$TIMESTAMP" ] && TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
[ -z "$MSG_ID" ] && MSG_ID="msg_$(date +%s)_${FROM}_$RANDOM"

# 1. Optional Encryption
IS_ENCRYPTED=0
PAYLOAD_BODY="$BODY"
if [ "${LOCUTUS_ENCRYPT:-0}" = "1" ]; then
    PAYLOAD_BODY="$("$SCRIPT_DIR/security.sh" encrypt "$BODY")"
    IS_ENCRYPTED=1
fi

# 2. Compute HMAC-SHA256 signature
SIG="$("$SCRIPT_DIR/security.sh" sign "$MSG_ID" "$FROM" "$RECIPIENT" "$TYPE" "$PAYLOAD_BODY" "$TIMESTAMP")"

# 3. Construct JSON Payload safely using python3
MSG_JSON=$(python3 -c '
import sys, json
payload = {
    "id": sys.argv[1],
    "from": sys.argv[2],
    "to": sys.argv[3],
    "type": sys.argv[4],
    "reply_to": sys.argv[5] or None,
    "tags": [t.strip() for t in sys.argv[6].split(",") if t.strip()],
    "subject": sys.argv[7],
    "body": sys.argv[8],
    "sig": sys.argv[9],
    "encrypted": sys.argv[10] == "1",
    "timestamp": sys.argv[11]
}
print(json.dumps(payload))
' "$MSG_ID" "$FROM" "$RECIPIENT" "$TYPE" "$REPLY_TO" "$TAGS" "$SUBJECT" "$PAYLOAD_BODY" "$SIG" "$IS_ENCRYPTED" "$TIMESTAMP")

# 4. Dispatch via Redis
if [[ "$RECIPIENT" == "@"* || "$RECIPIENT" == "*" ]]; then
    TARGET_TAGS="${RECIPIENT#@}"
    redis-cli -u "$REDIS_URL" EVAL "$(cat "$SCRIPT_DIR/multicast.lua")" 0 "$PREFIX" "$TARGET_TAGS" "$MSG_JSON" 604800
else
    redis-cli -u "$REDIS_URL" EVAL "$(cat "$SCRIPT_DIR/send_o2o.lua")" 0 "$PREFIX" "$RECIPIENT" "$MSG_JSON" 604800
fi
