#!/usr/bin/env bash
# scripts/security.sh
# Core cryptographic utility for Locutus inter-assistant communication bus.
# Provides HMAC-SHA256 signing, verification, and optional AES-256-CBC encryption.

set -euo pipefail

get_secret() {
    if [ -n "${LOCUTUS_SECRET:-}" ]; then
        printf "%s" "$LOCUTUS_SECRET"
        return 0
    fi

    local secret_file="${LOCUTUS_SECRET_FILE:-$HOME/.config/locutus/secret}"
    if [ ! -f "$secret_file" ]; then
        mkdir -p "$(dirname "$secret_file")"
        (umask 077 && openssl rand -hex 32 > "$secret_file")
        chmod 600 "$secret_file"
    fi
    tr -d '\r\n' < "$secret_file"
}

compute_hmac() {
    local secret="$1"
    local data="$2"
    printf "%s" "$data" | openssl dgst -sha256 -hmac "$secret" | awk '{print $NF}'
}

case "${1:-}" in
    get-secret)
        get_secret
        ;;
    sign)
        # Usage: security.sh sign <id> <from> <to> <type> <subject> <body> <ts>
        secret=$(get_secret)
        data="${2}|${3}|${4}|${5}|${6}|${7}|${8}"
        compute_hmac "$secret" "$data"
        ;;
    verify)
        # Usage: security.sh verify <sig> <id> <from> <to> <type> <subject> <body> <ts>
        expected_sig="$2"
        secret=$(get_secret)
        data="${3}|${4}|${5}|${6}|${7}|${8}|${9}"
        actual_sig=$(compute_hmac "$secret" "$data")
        if [ "$actual_sig" = "$expected_sig" ]; then
            exit 0
        else
            exit 1
        fi
        ;;
    encrypt)
        # Usage: security.sh encrypt <body>
        secret=$(get_secret)
        body="$2"
        printf "%s" "$body" | openssl enc -aes-256-cbc -pbkdf2 -iter 10000 -pass "pass:$secret" -base64 -A
        ;;
    decrypt)
        # Usage: security.sh decrypt <ciphertext>
        secret=$(get_secret)
        ciphertext="$2"
        printf "%s" "$ciphertext" | openssl enc -d -aes-256-cbc -pbkdf2 -iter 10000 -pass "pass:$secret" -base64 -A
        ;;
    *)
        echo "Usage: $0 {get-secret|sign|verify|encrypt|decrypt} [args...]" >&2
        exit 1
        ;;
esac
