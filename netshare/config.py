"""Application settings: filesystem layout, ports and environment overrides.

Everything that used to be a module-level constant scattered across server.py
lives here, resolved once at startup and passed explicitly to the services.
"""

from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _app_dir() -> Path:
    """Writable directory beside the script/executable that holds user data."""
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _resource_dir() -> Path:
    """Read-only bundled resources (templates, static, ngrok binary)."""
    if _is_frozen():
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _load_or_create_secret(path: Path) -> bytes:
    """Persistent random key used to sign cookies and PIN tokens."""
    env = os.getenv("NETSHARE_SECRET_KEY", "").strip()
    if env:
        return env.encode()
    try:
        if path.exists():
            data = path.read_bytes().strip()
            if data:
                return data
        key = secrets.token_hex(32).encode()
        path.write_bytes(key)
        return key
    except OSError:
        # Read-only location: fall back to a per-process key (sessions reset on restart).
        return secrets.token_hex(32).encode()


IS_WINDOWS = sys.platform == "win32"

# Well-known install locations probed when the tool is not on PATH.
if IS_WINDOWS:
    _ADB_CANDIDATES: tuple[str, ...] = (
        r"F:\Install\Android\Sdk\platform-tools\adb.exe",
        r"~\AppData\Local\Android\Sdk\platform-tools\adb.exe",
        r"C:\platform-tools\adb.exe",
        r"C:\Program Files\Android\platform-tools\adb.exe",
        r"C:\Android\sdk\platform-tools\adb.exe",
        r"D:\Android\Sdk\platform-tools\adb.exe",
    )
    _SCRCPY_CANDIDATES: tuple[str, ...] = (
        r"C:\scrcpy\scrcpy.exe",
        r"D:\scrcpy\scrcpy.exe",
        r"F:\scrcpy\scrcpy.exe",
        r"F:\Install\scrcpy\scrcpy.exe",
        r"~\AppData\Local\scrcpy\scrcpy.exe",
        r"C:\Program Files\scrcpy\scrcpy.exe",
    )
    # An existing install is reused; otherwise a pinned portable build goes into bin/.
    _UXPLAY_CANDIDATES: tuple[str, ...] = (r"C:\Program Files\uxplay-windows\uxplay-windows.exe",)
    # Portable scrcpy build exists only for Windows; Linux uses the distro package.
    _SCRCPY_URL = "https://github.com/Genymobile/scrcpy/releases/download/v2.7/scrcpy-win64-v2.7.zip"
else:
    _ADB_CANDIDATES = (
        "~/Android/Sdk/platform-tools/adb",
        "/opt/android-sdk/platform-tools/adb",
        "/usr/lib/android-sdk/platform-tools/adb",
        "/snap/bin/adb",
    )
    _SCRCPY_CANDIDATES = ("/usr/local/bin/scrcpy", "/snap/bin/scrcpy", "/usr/bin/scrcpy")
    _UXPLAY_CANDIDATES = ("/usr/bin/uxplay", "/usr/local/bin/uxplay")
    _SCRCPY_URL = ""


@dataclass(frozen=True)
class Settings:
    http_port: int
    https_port: int        # 0 = no HTTPS listener (voice messages then work on localhost only)
    cert_dir: Path
    app_dir: Path
    resource_dir: Path
    upload_dir: Path
    chat_upload_dir: Path
    bin_dir: Path
    db_path: Path
    db_key_path: Path      # AES key for chat/clipboard text (made on first run)
    config_path: Path
    template_dir: Path
    static_dir: Path
    secret_key: bytes
    chat_cipher_key: str
    initial_pin: str
    airplay_enabled: bool
    airplay_auto_install: bool
    uxplay_path: Path | None  # explicit override (UXPLAY_PATH)
    uxplay_candidates: tuple[str, ...]
    adb_candidates: tuple[str, ...]
    scrcpy_candidates: tuple[str, ...]
    scrcpy_download_url: str
    capture_fps: int
    capture_quality: int
    capture_max_width: int


def load_settings(app_dir: Path | None = None) -> Settings:
    app_dir = app_dir or _app_dir()
    resource_dir = _resource_dir()

    load_dotenv(app_dir / ".env")
    load_dotenv()  # also honour a .env in the current working directory

    upload_dir = app_dir / "uploads"
    chat_upload_dir = app_dir / "chat uploads"
    bin_dir = app_dir / "bin"
    for d in (upload_dir, chat_upload_dir):
        d.mkdir(parents=True, exist_ok=True)

    return Settings(
        http_port=int(os.getenv("NETSHARE_PORT", "5000")),
        https_port=int(os.getenv("NETSHARE_HTTPS_PORT", "5443")),
        cert_dir=app_dir / "certs",
        app_dir=app_dir,
        resource_dir=resource_dir,
        upload_dir=upload_dir,
        chat_upload_dir=chat_upload_dir,
        bin_dir=bin_dir,
        db_path=app_dir / "retcore_onlan.db",  # an older chat.db is moved here on first start
        db_key_path=app_dir / ".retcore_db_key",
        config_path=app_dir / "netshare_config.json",
        template_dir=resource_dir / "templates",
        static_dir=resource_dir / "static",
        secret_key=_load_or_create_secret(app_dir / ".netshare_secret"),
        chat_cipher_key=os.getenv("CHAT_CIPHER_KEY", "DefaultSecret123"),  # only to read pre-AES data
        initial_pin=os.getenv("SERVER_PIN", "").strip(),
        airplay_enabled=_env_bool("AIRPLAY_ENABLED", True),
        airplay_auto_install=_env_bool("AIRPLAY_AUTO_INSTALL", IS_WINDOWS),  # pinned download; Linux uses apt
        uxplay_path=Path(os.environ["UXPLAY_PATH"]) if os.getenv("UXPLAY_PATH") else None,
        uxplay_candidates=_UXPLAY_CANDIDATES,
        adb_candidates=tuple(os.path.expanduser(p) for p in _ADB_CANDIDATES),
        scrcpy_candidates=tuple(os.path.expanduser(p) for p in _SCRCPY_CANDIDATES),
        scrcpy_download_url=_SCRCPY_URL,
        capture_fps=30,
        capture_quality=75,
        capture_max_width=1280,
    )
