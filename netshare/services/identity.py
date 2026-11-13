"""Who is this browser? — stable chat identity per device.

Previously identity was `arp -a <ip>` on every request, which (a) cost a
subprocess per call and (b) collapsed every ngrok visitor into one user because
they all arrive from 127.0.0.1. Now the device key is resolved once and pinned
in a signed cookie:

  * LAN visitor      -> MAC from ARP (keeps existing chat history)
  * host on loopback -> "localhost-mac-id" (legacy key, keeps history)
  * via ngrok proxy  -> fresh random key per browser
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from itsdangerous import BadSignature, URLSafeSerializer

from ..infrastructure.network import MacResolver

COOKIE_NAME = "nsh_device"
COOKIE_MAX_AGE = 5 * 365 * 24 * 3600
LOCALHOST_KEY = "localhost-mac-id"


@dataclass(frozen=True)
class ClientInfo:
    ip: str            # real client IP (proxy-aware)
    is_proxied: bool   # arrived through the ngrok tunnel
    is_loopback: bool  # TCP peer is this machine


@dataclass(frozen=True)
class Identity:
    key: str
    is_new: bool  # caller should persist the cookie


class IdentityService:
    def __init__(self, secret: bytes, mac_resolver: MacResolver):
        self._signer = URLSafeSerializer(secret, salt="netshare-device")
        self._macs = mac_resolver

    def resolve(self, cookie_value: str | None, client: ClientInfo) -> Identity:
        if cookie_value:
            try:
                key = self._signer.loads(cookie_value)
                if isinstance(key, str) and key:
                    return Identity(key, is_new=False)
            except BadSignature:
                pass
        return Identity(self._derive(client), is_new=True)

    def _derive(self, client: ClientInfo) -> str:
        if client.is_proxied:
            return "dev_" + uuid.uuid4().hex
        if client.is_loopback:
            return LOCALHOST_KEY
        return self._macs.lookup(client.ip)

    def cookie_value(self, key: str) -> str:
        return self._signer.dumps(key)
