"""Open a folder in the desktop's file manager (Explorer, Files/Nautilus, Finder)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def open_folder(path: Path) -> None:
    """Show ``path`` in the file manager; creates it first if needed.
    Raises OSError when no file manager can be started."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 — opens Explorer on our own folder
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        opener = shutil.which("xdg-open") or shutil.which("gio")
        if not opener:
            raise OSError("no file manager found (install xdg-utils)")
        args = [opener, "open", str(path)] if opener.endswith("gio") else [opener, str(path)]
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
