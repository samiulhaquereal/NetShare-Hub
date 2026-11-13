"""UxPlay (AirPlay receiver) process management for iOS screen mirroring.

Windows: ``uxplay-windows`` (auto-installed with winget when enabled).
Linux:   the distro ``uxplay`` package (``sudo apt install uxplay``) plus a
         running avahi-daemon for Bonjour/mDNS discovery.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

from . import winproc
from .windows import find_window

BROADCAST_NAME = "NetShare-Hub"
IS_WINDOWS = sys.platform == "win32"


def _is_airplay_title(title: str) -> bool:
    t = title.lower()
    return "airplay" in t or "uxplay" in t or BROADCAST_NAME.lower() in t


class AirPlayEngine:
    def __init__(self, exe_path: Path, auto_install: bool):
        self._exe = exe_path
        self._auto_install = auto_install
        self._proc: subprocess.Popen | None = None

    @property
    def installed(self) -> bool:
        return self._exe.exists()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start_async(self) -> None:
        """Start in the background — installers/kill commands must not delay server startup."""
        threading.Thread(target=self._start, daemon=True, name="AirPlayStart").start()

    def _start(self) -> None:
        if IS_WINDOWS:
            winproc.run_quiet(["powershell", "-NoProfile", "-Command",
                               'Set-ItemProperty -Path "HKCU:\\Software\\leapbtw\\uxplay-windows" '
                               f'-Name "Arguments" -Value "-n {BROADCAST_NAME} -nh"'], timeout=15)
            winproc.run_quiet(["taskkill", "/F", "/IM", self._exe.name], timeout=10)
        else:
            # Only our own instance (matched by broadcast name), never the user's other UxPlay.
            winproc.run_quiet(["pkill", "-f", f"{self._exe.name} -n {BROADCAST_NAME}"], timeout=10)

        if not self.installed and self._auto_install and IS_WINDOWS:
            print("📥 [AirPlay] UxPlay not found — installing via winget…")
            winproc.run_quiet(["winget", "install", "--id", "leapbtw.uxplay",
                               "--accept-package-agreements", "--accept-source-agreements", "-h"], timeout=600)
        if not self.installed:
            hint = "" if IS_WINDOWS else " — install with: sudo apt install uxplay avahi-daemon"
            print(f"⚠️ [AirPlay] UxPlay not found at {self._exe}{hint}")
            return
        try:
            self._proc = subprocess.Popen([str(self._exe), "-n", BROADCAST_NAME, "-nh"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          creationflags=winproc.CREATE_NO_WINDOW)
            print(f"🍏 [AirPlay] Broadcasting '{BROADCAST_NAME}' for iOS Screen Mirroring")
        except OSError as exc:
            print(f"⚠️ [AirPlay] Could not start UxPlay: {exc}")

    def stop(self) -> None:
        if self.running:
            self._proc.terminate()

    @staticmethod
    def mirror_window() -> dict | None:
        """The UxPlay window that shows an active iOS mirror, if any."""
        return find_window(_is_airplay_title)
