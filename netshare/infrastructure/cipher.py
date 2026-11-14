"""Encryption at rest for chat messages and clipboards.

New data: AES-256-GCM (authenticated) with a random key made once per
installation (``.retcore_db_key`` next to the database) → ``ENC2:`` + base64
(12-byte nonce ‖ ciphertext). Copying the database file alone is not enough to
read the messages.

Older data: ``ENC:`` + base64 of a repeating-key XOR (``TextObfuscator``). That
was obfuscation, not cryptography, and its default key was public. It is only
*read* now — the database migration re-encrypts it with AES-GCM.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from pathlib import Path

PREFIX = "ENC:"     # legacy XOR obfuscation
PREFIX2 = "ENC2:"   # AES-256-GCM
LEGACY_DEFAULT_KEY = "DefaultSecret123"


class TextObfuscator:
    """Legacy XOR + base64 (read-only use: decoding old databases)."""

    def __init__(self, key: str):
        if not key:
            raise ValueError("cipher key must not be empty")
        self._key = key

    def _xor(self, text: str) -> str:
        key, n = self._key, len(self._key)
        return "".join(chr(ord(c) ^ ord(key[i % n])) for i, c in enumerate(text))

    def encode(self, text: str) -> str:
        if not text:
            return ""
        return PREFIX + base64.b64encode(self._xor(text).encode()).decode()

    def decode(self, stored: str | None) -> str:
        if not stored:
            return ""
        if not stored.startswith(PREFIX):
            return stored  # legacy plaintext / system text
        try:
            return self._xor(base64.b64decode(stored[len(PREFIX):]).decode())
        except (ValueError, UnicodeDecodeError):
            return stored


class TextCipher:
    """AES-256-GCM for new data; still decodes legacy ``ENC:`` and plain text."""

    def __init__(self, key: bytes, legacy: TextObfuscator | None = None):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if len(key) != 32:
            raise ValueError("key must be 32 bytes")
        self._aead = AESGCM(key)
        self._legacy = legacy

    def encode(self, text: str) -> str:
        if not text:
            return ""
        nonce = secrets.token_bytes(12)
        sealed = self._aead.encrypt(nonce, text.encode("utf-8"), None)
        return PREFIX2 + base64.b64encode(nonce + sealed).decode("ascii")

    def decode(self, stored: str | None) -> str:
        if not stored:
            return ""
        if stored.startswith(PREFIX2):
            try:
                raw = base64.b64decode(stored[len(PREFIX2):])
                return self._aead.decrypt(raw[:12], raw[12:], None).decode("utf-8")
            except Exception:  # wrong key / damaged row: never crash the chat over one message
                return "🔒 (message can't be decrypted)"
        if stored.startswith(PREFIX):
            return self._legacy.decode(stored) if self._legacy else stored
        return stored  # plain text (very old rows, system text)


def load_or_create_key(path: Path) -> bytes:
    """32-byte database key: ``RETCORE_DB_KEY`` (base64) or a file made on first run."""
    env = os.getenv("RETCORE_DB_KEY", "").strip()
    if env:
        try:
            key = base64.b64decode(env)
            if len(key) == 32:
                return key
        except ValueError:
            pass
        return hashlib.sha256(env.encode()).digest()  # a passphrase: derive a key from it
    path = Path(path)
    try:
        if path.exists():
            key = base64.b64decode(path.read_text(encoding="ascii").strip())
            if len(key) == 32:
                return key
        key = secrets.token_bytes(32)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(base64.b64encode(key).decode("ascii"), encoding="ascii")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        return key
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"can't read or create the database key at {path}: {exc}") from exc
