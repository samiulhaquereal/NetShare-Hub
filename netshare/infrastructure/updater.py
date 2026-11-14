"""Self-update from the project's GitHub "latest" release.

CI writes ``netshare/_build_info.py`` (build number, commit, version) into every
release build and publishes a ``version.json`` beside the executables with the
build number and the SHA-256 + size of each file. The app compares build numbers,
downloads its platform's file with progress, verifies the hash, runs the new
binary's ``--check`` self-test, then swaps it in:

* Windows can't overwrite a running .exe but can *rename* it: the running file
  becomes ``…exe.old``, the new one takes its name.
* Linux: the new binary replaces the old one atomically (``os.replace``).

The new process is started with ``--after-update <pid>``; it waits for the old
one to exit (so it gets the same port), deletes the ``.old`` file once it has run
for a while, and puts the previous version back if it can't start.

Only official release builds update themselves (running from source → ``git pull``).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO = os.getenv("RETCORE_UPDATE_REPO", "samiulhaquereal/RetCore-OnLAN")
# Where version.json and the release files are downloaded from (a LAN mirror, for example).
BASE_URL = os.getenv("RETCORE_UPDATE_URL", "").rstrip("/")
AUTO_INSTALL = os.getenv("RETCORE_UPDATE_AUTO", "").strip().lower() in ("1", "true", "yes")  # unattended PCs
MANIFEST = "version.json"
USER_AGENT = "RetCore-OnLAN-Updater"
CHUNK = 256 * 1024
OLD_SUFFIX = ".old"
PLATFORM = "windows" if sys.platform == "win32" else "linux" if sys.platform.startswith("linux") else None


class UpdateError(Exception):
    """Shown to the user as is."""


class UpdateCancelled(UpdateError):
    pass


@dataclass(frozen=True)
class BuildInfo:
    version: str
    build: int = 0          # CI run number; 0 = not an official build
    commit: str = ""

    @property
    def label(self) -> str:
        return f"v{self.version}" + (f" (build {self.build})" if self.build else "")


@dataclass(frozen=True)
class Release:
    version: str
    build: int
    commit: str
    built_at: str
    asset: str
    sha256: str
    size: int

    @property
    def label(self) -> str:
        return f"v{self.version} (build {self.build})"

    def url(self, repo: str = REPO, base: str = "") -> str:
        return f"{base or _latest(repo)}/{self.asset}"


def _latest(repo: str) -> str:
    return f"https://github.com/{repo}/releases/latest/download"


def current_build() -> BuildInfo:
    from .. import __version__
    try:
        from .. import _build_info as b  # written by CI; absent when running from source
        return BuildInfo(getattr(b, "VERSION", __version__), int(getattr(b, "BUILD", 0)), getattr(b, "COMMIT", ""))
    except ImportError:
        return BuildInfo(__version__)


def _open(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 — fixed https GitHub URLs


class Updater:
    def __init__(self, work_dir: Path, build: BuildInfo | None = None, exe: Path | None = None,
                 repo: str = REPO, platform: str | None = PLATFORM, opener: Callable = _open, base_url: str = BASE_URL):
        self.work_dir = Path(work_dir)
        self.build = build or current_build()
        self.exe = Path(exe) if exe else (Path(sys.executable) if getattr(sys, "frozen", False) else None)
        self.repo = repo
        self.base = base_url or _latest(repo)
        self.platform = platform
        self._open = opener

    # ── Can this copy update itself? ──────────────────────────────────
    @property
    def unsupported_reason(self) -> str | None:
        if self.exe is None:
            return "Running from source — update with git pull."
        if not self.build.build:
            return "This is a local build — only official releases update themselves."
        if not self.platform:
            return "Automatic updates aren't available on this system."
        return None

    @property
    def supported(self) -> bool:
        return self.unsupported_reason is None

    # ── 1. Check ───────────────────────────────────────────────────────
    def check(self, timeout: float = 10) -> Release | None:
        """The latest release if it is newer than this build, else None."""
        url = f"{self.base}/{MANIFEST}"
        try:
            with self._open(url, timeout) as r:
                data = json.loads(r.read(64 * 1024).decode("utf-8"))
        except Exception as exc:
            raise UpdateError(f"Couldn't reach GitHub ({exc.__class__.__name__})") from exc
        try:
            a = data["assets"][self.platform]
            rel = Release(str(data["version"]), int(data["build"]), str(data.get("commit", "")),
                          str(data.get("built_at", "")), str(a["name"]), str(a["sha256"]).lower(), int(a["size"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise UpdateError("The release information is incomplete") from exc
        if "/" in rel.asset or "\\" in rel.asset or len(rel.sha256) != 64:
            raise UpdateError("The release information is invalid")
        return rel if rel.build > self.build.build else None

    # ── 2. Download (with progress) and verify ────────────────────────
    def download(self, rel: Release, progress: Callable[[int, int], None] | None = None,
                 cancel: threading.Event | None = None) -> Path:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        part = self.work_dir / (rel.asset + ".part")
        digest, done = hashlib.sha256(), 0
        try:
            with self._open(rel.url(self.repo, self.base), 30) as r, open(part, "wb") as out:
                total = int(r.headers.get("Content-Length") or rel.size)
                while chunk := r.read(CHUNK):
                    if cancel is not None and cancel.is_set():
                        raise UpdateCancelled("Update cancelled")
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        except UpdateError:
            part.unlink(missing_ok=True)
            raise
        except Exception as exc:
            part.unlink(missing_ok=True)
            raise UpdateError(f"Download failed ({exc.__class__.__name__})") from exc
        if done != rel.size or digest.hexdigest() != rel.sha256:
            part.unlink(missing_ok=True)
            raise UpdateError("The download is damaged or the release just changed — please try again")
        final = self.work_dir / rel.asset
        os.replace(part, final)
        return final

    # ── 3. Unpack + self-test the new version ─────────────────────────
    def prepare(self, downloaded: Path) -> Path:
        """Path of the new executable (Linux releases are a .tar.gz)."""
        if self.platform != "linux" or not downloaded.name.endswith((".tar.gz", ".tgz")):
            return downloaded
        target = self.work_dir / "new"
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)
        with tarfile.open(downloaded) as tar:
            members = [m for m in tar.getmembers() if m.isfile()]
            name = members[0].name if len(members) == 1 else ""
            name = name[2:] if name.startswith("./") else name  # tar's "./file" form only
            if not name or "/" in name or "\\" in name or ".." in name:
                raise UpdateError("Unexpected release archive layout")
            new = target / name  # written by hand: no paths, links or permissions from the archive
            with tar.extractfile(members[0]) as src, open(new, "wb") as out:
                shutil.copyfileobj(src, out, CHUNK)
        new.chmod(0o755)
        return new

    def self_test(self, new_exe: Path, timeout: float = 240) -> None:
        """The new binary must pass its own ``--check`` before it replaces this one."""
        try:
            proc = subprocess.run([str(new_exe), "--check"], env=_clean_env(), capture_output=True,
                                  timeout=timeout, cwd=str(self.work_dir))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError(f"The new version didn't start ({exc.__class__.__name__})") from exc
        if proc.returncode != 0:
            raise UpdateError("The new version failed its self-test — keeping the current one")

    # ── 4. Swap and restart ───────────────────────────────────────────
    def install(self, new_exe: Path) -> Path:
        """Put ``new_exe`` where the running executable is; the old one is kept as ``.old``."""
        exe = self.exe
        old = exe.with_name(exe.name + OLD_SUFFIX)
        try:
            old.unlink(missing_ok=True)
            if self.platform == "windows":
                os.replace(exe, old)          # allowed for a running .exe (deleting is not)
                try:
                    shutil.move(str(new_exe), str(exe))
                except OSError:
                    os.replace(old, exe)      # put the running version back
                    raise
            else:
                shutil.copy2(exe, old)
                staged = exe.with_name(exe.name + ".new")
                shutil.copy2(new_exe, staged)
                staged.chmod(0o755)
                os.replace(staged, exe)       # atomic; the running process keeps its inode
        except PermissionError as exc:
            raise UpdateError(f"No permission to replace {exe.name} in {exe.parent} — "
                              "move the app to a folder you can write to, or download it manually") from exc
        except OSError as exc:
            raise UpdateError(f"Couldn't install the update ({exc})") from exc
        return exe

    def relaunch(self, args: list[str]) -> None:
        """Start the (new) executable; it waits for this process to exit before binding ports."""
        cmd = [str(self.exe), *[a for a in args if a], "--after-update", str(os.getpid())]
        kwargs: dict = {"env": _clean_env(), "cwd": str(self.exe.parent), "close_fds": True}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)


def _clean_env() -> dict:
    """Environment for starting another PyInstaller build from inside this one:
    it must unpack itself fresh, not reuse this process's temporary folder."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("_MEI", "_PYI"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


# ── Started by an update ──────────────────────────────────────────────
def wait_for_exit(pid: int, timeout: float = 30) -> bool:
    """Block until process ``pid`` has exited (the previous version releasing its ports)."""
    deadline = time.monotonic() + timeout
    if sys.platform == "win32":
        import ctypes
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return True  # already gone
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.2)
    return False


def cleanup_after_update(exe: Path | None, work_dir: Path, delay: float = 60) -> None:
    """Once the new version has run for ``delay`` s, drop the old binary and downloads."""
    def run():
        time.sleep(delay)
        if exe:
            for _ in range(10):
                try:
                    exe.with_name(exe.name + OLD_SUFFIX).unlink(missing_ok=True)
                    break
                except OSError:  # still locked for a moment on Windows
                    time.sleep(3)
        shutil.rmtree(work_dir, ignore_errors=True)
    threading.Thread(target=run, daemon=True, name="update-cleanup").start()


def rollback(exe: Path) -> bool:
    """The new version couldn't start: bring back ``.old`` and start it. True if done."""
    old = exe.with_name(exe.name + OLD_SUFFIX)
    if not old.exists():
        return False
    try:
        failed = exe.with_name(exe.name + ".failed")
        failed.unlink(missing_ok=True)
        os.replace(exe, failed)
        os.replace(old, exe)
        kwargs = {"creationflags": 0x00000008 | 0x00000200} if sys.platform == "win32" else {"start_new_session": True}
        subprocess.Popen([str(exe)], env=_clean_env(), cwd=str(exe.parent), close_fds=True, **kwargs)
        return True
    except OSError:
        return False
