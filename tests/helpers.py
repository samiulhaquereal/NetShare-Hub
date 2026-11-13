"""Shared fixtures: an isolated app instance backed by a temp directory."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from netshare.config import load_settings
from netshare.container import build_container
from netshare.services.identity import IdentityService
from netshare.web.app import create_app

LAN_IP = "192.168.1.50"
LAN_MAC = "AA:BB:CC:DD:EE:01"


class FakeMacResolver:
    def __init__(self, table: dict[str, str] | None = None):
        self.table = table or {LAN_IP: LAN_MAC}
        self.calls = 0

    def lookup(self, ip: str) -> str:
        self.calls += 1
        return self.table.get(ip, ip)


class AppTestCase(unittest.TestCase):
    """Fresh data dir + container per test; the real chat.db/uploads are never touched."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="netshare-test-"))
        os.environ.pop("SERVER_PIN", None)
        self.settings = load_settings(app_dir=self._tmp)
        self.c = build_container(self.settings)
        self.macs = FakeMacResolver()
        self.c.identity = IdentityService(self.settings.secret_key, self.macs)
        self.app = create_app(self.c)
        self.app.testing = True

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def host(self):
        return self.app.test_client()  # REMOTE_ADDR 127.0.0.1

    def lan(self, ip: str = LAN_IP):
        client = self.app.test_client()
        client.environ_base["REMOTE_ADDR"] = ip
        return client

    def tunnel(self, visitor_ip: str = "203.0.113.9"):
        client = self.app.test_client()
        client.environ_base["HTTP_X_FORWARDED_FOR"] = visitor_ip
        return client
