"""At-rest obfuscation for chat text stored in SQLite.

NOTE: this is XOR + base64 — obfuscation, not cryptography. It only keeps
casual readers of chat.db from seeing plaintext. The key never leaves the
server any more; clients receive already-decoded text over the API.
The format (``ENC:`` + base64) is kept for compatibility with existing data.
"""

from __future__ import annotations

import base64

PREFIX = "ENC:"


class TextObfuscator:
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
