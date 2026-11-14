"""AirPlay receiver (UxPlay) for iOS screen mirroring.

Windows: a *pinned* portable build of ``uxplay-windows`` is downloaded once from
its official GitHub release, SHA-256 verified, and kept in ``<app>/bin`` — no
winget, no installer, no admin rights (Bonjour aside). An already-installed copy
is used when present. Linux uses the distro ``uxplay`` package plus avahi-daemon.

uxplay-windows ignores command-line arguments; it reads them from
``%APPDATA%\\leapbtw\\uxplay-windows\\arguments.txt``. We write our arguments there
while running and put the user's own file back when we stop.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import winproc
from .downloads import DownloadError, Package, install_zip
from .windows import find_window

BROADCAST_NAME = "RetCore-OnLAN"
ARGS = ("-n", BROADCAST_NAME, "-nh")
IS_WINDOWS = sys.platform == "win32"

UXPLAY_WINDOWS = Package(
    name="uxplay-windows",
    version="2.0.0.1736",
    url="https://github.com/leapbtw/uxplay-windows/releases/download/2.0.0.1736/uxplay-windows.zip",
    sha256="9d3a51c15fc9db857351195e7eb7bbb21700d9ae25d936a54bcf8536b62cca18",
    size=113_529_789,
)
_EXE = "uxplay-windows.exe" if IS_WINDOWS else "uxplay"
_HELPERS = ("uxplay-windows.exe", "uxplay-bluetooth-beacon.exe")


def _is_airplay_title(title: str) -> bool:
    t = title.lower()
    return "airplay" in t or "uxplay" in t or BROADCAST_NAME.lower() in t


def _args_file() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / "leapbtw" / "uxplay-windows" / "arguments.txt"


class ArgsFile:
    """Swap our arguments into uxplay-windows' config file and restore the user's afterwards."""

    def __init__(self, path: Path):
        self.path = path
        self.backup = path.with_name(path.name + ".netshare-backup")

    @property
    def wanted(self) -> str:
        return " ".join(ARGS)

    def apply(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        current = self.path.read_text(encoding="utf-8", errors="replace") if self.path.exists() else None
        if current is not None and current.strip() != self.wanted and not self.backup.exists():
            self.backup.write_text(current, encoding="utf-8")
        self.path.write_text(self.wanted, encoding="utf-8")

    def restore(self) -> None:
        if self.backup.exists():
            os.replace(self.backup, self.path)


class AirPlayEngine:
    def __init__(self, bin_dir: Path, candidates: tuple[str, ...], auto_install: bool,
                 custom_path: Path | None = None, package: Package = UXPLAY_WINDOWS,
                 args_file: ArgsFile | None = None):
        self._bundle_dir = bin_dir / package.name / package.version
        self._candidates = candidates
        self._custom = custom_path
        self._auto_install = auto_install and IS_WINDOWS
        self._pkg = package
        self._args = args_file or (ArgsFile(_args_file()) if IS_WINDOWS else None)
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.state = "idle"        # idle | downloading | running | missing | error
        self.progress = 0.0        # download progress, 0..1
        self.error = ""

    # ── where is UxPlay? ─────────────────────────────────────────────
    @property
    def bundled_exe(self) -> Path:
        return self._bundle_dir / _EXE

    def locate(self) -> tuple[Path | None, str]:
        """(executable, source) — source is custom | bundled | system | ''."""
        if self._custom:
            return (self._custom, "custom") if self._custom.exists() else (None, "")
        if IS_WINDOWS and self.bundled_exe.exists():
            return self.bundled_exe, "bundled"
        for c in self._candidates:
            if c and Path(c).exists():
                return Path(c), "system"
        found = shutil.which(_EXE)
        return (Path(found), "system") if found else (None, "")

    @property
    def installed(self) -> bool:
        return self.locate()[0] is not None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @staticmethod
    def bonjour_running() -> bool | None:
        """Windows needs Apple's Bonjour service for discovery; None = not applicable/unknown."""
        if not IS_WINDOWS:
            return None
        try:
            out = winproc.run(["sc", "query", "Bonjour Service"], timeout=5, text=True).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        return "RUNNING" in out

    def status(self) -> dict:
        exe, source = self.locate()
        return {
            "state": "running" if self.running and self.state != "downloading" else self.state,
            "installed": exe is not None,
            "running": self.running,
            "source": source,
            "version": self._pkg.version if source == "bundled" else "",
            "pinned_version": self._pkg.version,
            "progress": round(self.progress, 3),
            "error": self.error,
            "bonjour": self.bonjour_running(),
            "name": BROADCAST_NAME,
            "can_download": IS_WINDOWS,
        }

    # ── lifecycle ────────────────────────────────────────────────────
    def start_async(self) -> None:
        """Start in the background — downloads must not delay server startup."""
        threading.Thread(target=self._start, daemon=True, name="AirPlayStart").start()

    def install(self) -> Path:
        """Download and verify the pinned portable build (Windows)."""
        if not IS_WINDOWS:
            raise DownloadError("install uxplay with: sudo apt install uxplay avahi-daemon")
        self.state, self.progress, self.error = "downloading", 0.0, ""
        print(f"📥 [AirPlay] Downloading {self._pkg.name} {self._pkg.version} (pinned, SHA-256 verified)…")

        def on_progress(done: int, total: int) -> None:
            self.progress = done / total if total else 0.0

        try:
            install_zip(self._pkg, self._bundle_dir, on_progress)
        except DownloadError as exc:
            self.state, self.error = "error", str(exc)
            raise
        self.progress, self.state = 1.0, "idle"
        return self.bundled_exe

    def _start(self) -> None:
        with self._lock:
            if self.running:
                return
            exe, _source = self.locate()
            if exe is None and self._auto_install:
                try:
                    exe = self.install()
                except DownloadError as exc:
                    print(f"⚠️ [AirPlay] {exc}")
                    return
            if exe is None:
                self.state = "missing"
                hint = "" if IS_WINDOWS else " — install with: sudo apt install uxplay avahi-daemon"
                print(f"⚠️ [AirPlay] UxPlay not found{hint}")
                return
            self._kill_strays(exe)
            try:
                if self._args:
                    self._args.apply()
                cmd = [str(exe)] if IS_WINDOWS else [str(exe), *ARGS]
                self._proc = subprocess.Popen(cmd, cwd=str(exe.parent), stdin=subprocess.DEVNULL,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                              creationflags=winproc.CREATE_NO_WINDOW)
                self.state, self.error = "running", ""
                print(f"🍏 [AirPlay] Broadcasting '{BROADCAST_NAME}' for iOS Screen Mirroring ({exe})")
                if self.bonjour_running() is False:
                    print("⚠️ [AirPlay] Bonjour service is not running — iPhones won't see the PC. "
                          "Use 'Set up Bonjour' on the Mobile page (needs admin once).")
            except OSError as exc:
                self.state, self.error = "error", str(exc)
                print(f"⚠️ [AirPlay] Could not start UxPlay: {exc}")

    def _kill_strays(self, exe: Path) -> None:
        """Two receivers can't share AirPlay's ports — stop a previous instance first."""
        if IS_WINDOWS:
            for name in _HELPERS:
                winproc.run_quiet(["taskkill", "/F", "/IM", name], timeout=10)
        else:
            # Only our own instance (matched by broadcast name), never the user's other UxPlay.
            winproc.run_quiet(["pkill", "-f", f"{exe.name} -n {BROADCAST_NAME}"], timeout=10)

    def stop(self) -> None:
        with self._lock:
            if self.running:
                if IS_WINDOWS:  # /T also ends the bluetooth-beacon helper it spawned
                    winproc.run_quiet(["taskkill", "/F", "/T", "/PID", str(self._proc.pid)], timeout=10)
                else:
                    self._proc.terminate()
            self._proc = None
            if self.state == "running":
                self.state = "idle"
            if self._args:
                self._args.restore()

    def restart_async(self) -> None:
        def run():
            self.stop()
            self._start()
        threading.Thread(target=run, daemon=True, name="AirPlayRestart").start()

    def setup_bonjour(self) -> bool:
        """Register the mDNSResponder shipped with the bundle as the Bonjour service (UAC prompt)."""
        exe, _ = self.locate()
        responder = exe.parent / "mDNSResponder.exe" if exe else None
        if not IS_WINDOWS or not responder or not responder.exists():
            return False
        quoted = str(responder).replace("'", "''")
        script = (f"Start-Process -FilePath '{quoted}'-ArgumentList '-install' -Verb RunAs -Wait; "
                  "Start-Service -Name 'Bonjour Service' -ErrorAction SilentlyContinue")
        winproc.run_quiet(["powershell", "-NoProfile", "-Command", script], timeout=120)
        return bool(self.bonjour_running())

    @staticmethod
    def mirror_window() -> dict | None:
        """The UxPlay window that shows an active iOS mirror, if any."""
        return find_window(_is_airplay_title)
