"""Public internet access through a single ngrok HTTPS tunnel (HTTP + WebSockets)."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from . import winproc


class NgrokTunnel:
    def __init__(self, resource_dir: Path, port: int):
        self._resource_dir = resource_dir
        self._port = port
        self._lock = threading.Lock()
        self.public_url: str | None = None

    @property
    def public_ws_url(self) -> str | None:
        if not self.public_url:
            return None
        return self.public_url.replace("https://", "wss://").replace("http://", "ws://")

    def start(self, token: str) -> str:
        """Open the tunnel and return its public URL. Raises on failure."""
        from pyngrok import conf, ngrok

        with self._lock:
            exe = self._resource_dir / ("ngrok.exe" if os.name == "nt" else "ngrok")
            if exe.exists():
                conf.get_default().ngrok_path = str(exe)

            # A stale agent keeps the endpoint online (ERR_NGROK_334) — clear it first.
            if os.name == "nt":
                winproc.run_quiet(["taskkill", "/f", "/im", "ngrok.exe"], timeout=10)
            else:
                winproc.run_quiet(["pkill", "-x", "ngrok"], timeout=10)  # pkill ships with procps everywhere
            time.sleep(1.5)

            if token:
                ngrok.set_auth_token(token)
            tunnel = ngrok.connect(self._port, bind_tls=True)
            self.public_url = tunnel.public_url
            print("=" * 60)
            print(f" PUBLIC ACCESS ENABLED: {self.public_url}")
            print("=" * 60)
            return self.public_url

    def stop(self) -> None:
        from pyngrok import ngrok

        with self._lock:
            ngrok.kill()
            self.public_url = None
