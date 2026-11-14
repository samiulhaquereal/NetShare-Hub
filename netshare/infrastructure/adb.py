"""Android Debug Bridge integration: device discovery, Wi-Fi pairing, screen
capture and scrcpy launching."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from . import winproc


class AdbClient:
    def __init__(self, bundled_dirs: list[Path], candidates: tuple[str, ...]):
        self._bundled_dirs = bundled_dirs
        self._candidates = candidates
        self._path: str | None = None

    @property
    def path(self) -> str:
        if self._path is None:
            self._path = self._find()
        return self._path

    def _find(self) -> str:
        exe = "adb.exe" if os.name == "nt" else "adb"
        found = shutil.which(exe)
        if found:
            return found
        for d in self._bundled_dirs:
            p = d / exe
            if p.exists():
                return str(p)
        for p in self._candidates:
            if os.path.exists(p):
                return p
        return "adb"

    def _cmd(self, serial: str | None, *args: str) -> list[str]:
        cmd = [self.path]
        if serial:
            cmd += ["-s", serial]
        return cmd + list(args)

    def devices(self) -> list[dict]:
        proc = winproc.run([self.path, "devices", "-l"], timeout=10, text=True)
        lines = [l.strip() for l in (proc.stdout or proc.stderr or "").splitlines()
                 if l.strip() and not l.startswith("*")]
        devices = []
        for line in lines[1:]:  # skip "List of devices attached"
            parts = line.split()
            if len(parts) < 2:
                continue
            serial, status = parts[0], parts[1]
            model = next((p.split(":", 1)[1].replace("_", " ") for p in parts[2:] if p.startswith("model:")),
                         "Android Device")
            if model == "Android Device" and status == "device":
                model = self._prop(serial, "ro.product.model") or model
            devices.append({
                "serial": serial, "status": status, "model": model,
                "connection": "Wireless Wi-Fi" if (":" in serial or "_adb-tls" in serial) else "USB Cable",
            })
        return devices

    def _prop(self, serial: str, name: str) -> str:
        try:
            return (winproc.run(self._cmd(serial, "shell", "getprop", name), timeout=3, text=True).stdout or "").strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    def pair(self, host_port: str, code: str) -> tuple[bool, str]:
        proc = winproc.run([self.path, "pair", host_port, code], timeout=15, text=True)
        out = f"{proc.stdout}\n{proc.stderr}".strip()
        return ("Successfully paired" in out or proc.returncode == 0), out

    def connect(self, host_port: str) -> tuple[bool, str]:
        proc = winproc.run([self.path, "connect", host_port], timeout=15, text=True)
        out = f"{proc.stdout}\n{proc.stderr}".strip()
        low = out.lower()
        ok = "connected to" in low or (proc.returncode == 0 and "cannot connect" not in low and "failed" not in low)
        return ok, out

    def screencap(self, serial: str | None, timeout: float = 4) -> bytes:
        try:
            proc = winproc.run(self._cmd(serial, "exec-out", "screencap", "-p"), timeout=timeout)
            return proc.stdout if proc.returncode == 0 and proc.stdout else b""
        except (OSError, subprocess.SubprocessError):
            return b""


class _ScreenWorker:
    """Continuously refreshes one device's screenshot while someone is viewing it."""

    def __init__(self, adb: AdbClient, serial: str, idle_timeout: float):
        self._adb, self.serial, self._idle_timeout = adb, serial, idle_timeout
        self._lock = threading.Lock()
        self._frame: bytes | None = None
        self._last_access = time.monotonic()
        self.running = True
        threading.Thread(target=self._loop, daemon=True, name=f"adb-{serial}").start()

    def _loop(self) -> None:
        while self.running:
            if time.monotonic() - self._last_access > self._idle_timeout:
                self.running = False  # nobody is watching any more — stop hammering adb
                break
            frame = self._adb.screencap(self.serial)
            if frame:
                with self._lock:
                    self._frame = frame
            else:
                time.sleep(0.1)

    def frame(self) -> bytes | None:
        self._last_access = time.monotonic()
        with self._lock:
            return self._frame


class AdbScreenPool:
    def __init__(self, adb: AdbClient, idle_timeout: float = 15.0):
        self._adb, self._idle_timeout = adb, idle_timeout
        self._workers: dict[str, _ScreenWorker] = {}
        self._lock = threading.Lock()

    def frame(self, serial: str) -> bytes:
        with self._lock:
            worker = self._workers.get(serial)
            if worker is None or not worker.running:
                worker = self._workers[serial] = _ScreenWorker(self._adb, serial, self._idle_timeout)
        return worker.frame() or self._adb.screencap(serial, timeout=5)


class ScrcpyLauncher:
    def __init__(self, bin_dir: Path, candidates: tuple[str, ...], download_url: str):
        self._bin_dir, self._candidates, self._url = bin_dir, candidates, download_url

    def find(self) -> str | None:
        found = shutil.which("scrcpy.exe") or shutil.which("scrcpy")
        if found:
            return found
        if self._bin_dir.exists():
            for root, _dirs, files in os.walk(self._bin_dir):
                if "scrcpy.exe" in files:
                    return os.path.join(root, "scrcpy.exe")
        return next((p for p in self._candidates if os.path.exists(p)), None)

    def ensure_installed(self) -> str | None:
        path = self.find()
        if path or not self._url:  # Linux: no portable build — install via apt/snap
            return path
        print("[scrcpy] Downloading portable scrcpy package…")
        self._bin_dir.mkdir(parents=True, exist_ok=True)
        zip_path = self._bin_dir / "scrcpy.zip"
        try:
            req = urllib.request.Request(self._url, headers={"User-Agent": "RetCore-OnLAN"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(zip_path, "wb") as out:
                shutil.copyfileobj(resp, out)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(self._bin_dir)
            return self.find()
        except Exception as exc:
            print(f"[scrcpy] Download failed: {exc}")
            return None
        finally:
            zip_path.unlink(missing_ok=True)

    def launch(self, serial: str | None) -> None:
        path = self.ensure_installed()
        if not path:
            raise FileNotFoundError("scrcpy is not available")
        cmd = [path] + (["-s", serial] if serial else []) + ["--max-fps=60", "--video-bit-rate=8M", "--stay-awake"]
        subprocess.Popen(cmd, creationflags=winproc.CREATE_NO_WINDOW)
