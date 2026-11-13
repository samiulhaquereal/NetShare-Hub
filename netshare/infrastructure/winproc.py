"""Subprocess helpers that never flash a console window on Windows."""

from __future__ import annotations

import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if IS_WINDOWS else 0

_installed = False


def install_silent_popen() -> None:
    """Make every subprocess (including ones spawned by libraries such as pyngrok)
    default to CREATE_NO_WINDOW. Uses subclassing so PyInstaller-frozen code that
    inherits from Popen keeps working."""
    global _installed
    if _installed or not IS_WINDOWS:
        return

    original = subprocess.Popen

    class SilentPopen(original):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
            super().__init__(*args, **kwargs)

    subprocess.Popen = SilentPopen  # type: ignore[misc]
    _installed = True


def run(cmd: list[str], timeout: float, text: bool = False) -> subprocess.CompletedProcess:
    """subprocess.run with captured output, a mandatory timeout and no window."""
    return subprocess.run(cmd, capture_output=True, text=text, timeout=timeout,
                          creationflags=CREATE_NO_WINDOW)


def run_quiet(cmd: list[str], timeout: float) -> None:
    """Fire-and-wait for housekeeping commands whose result we don't need."""
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout, check=False, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        pass
