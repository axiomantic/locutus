# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |

## Security Architecture

Locutus is designed from the ground up with defensive cryptographic isolation:

1. **Air-Gap Prompt-Injection Firewall**:
   - Every incoming message must carry a valid HMAC-SHA256 signature calculated over its canonical envelope (`id|from|to|type|subject|body|timestamp`).
   - Senders authenticate with an out-of-band secret key (`~/.config/locutus/secret`, `0600` permissions).
   - `locutus listen` verifies signatures at the host OS process boundary. Forged, tampered, or unsigned messages are dropped to `stderr` and **never enter stdout or LLM context**.

2. **Secret Key Isolation**:
   - The shared secret key is never transmitted across Redis, never checked into Git, and never passed into LLM prompt contexts.
   - OpenSSL passphrase derivation utilizes secure descriptor paths (`-pass file:...` or `-pass env:...`) preventing key visibility in process tables (`ps aux`).

3. **Optional End-to-End Encryption (E2EE)**:
   - While HMAC-SHA256 signature verification is mandatory by default for all communication, payload encryption is optional.
   - Setting `LOCUTUS_ENCRYPT=1` transparently encrypts message bodies with OpenSSL AES-256-CBC PBKDF2 (10,000 iterations), ensuring raw plaintext never touches Redis memory or persistence files.

## Reporting a Vulnerability

If you discover a security vulnerability in Locutus:
1. Please **do not** open a public issue.
2. Email security concerns privately to `security@axiomantic.org` or contact the maintainers directly.
3. We will acknowledge receipt within 48 hours and coordinate a patch and disclosure timeline.
