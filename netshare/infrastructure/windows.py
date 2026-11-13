"""Top-level application window enumeration — Win32 on Windows, EWMH/X11 on Linux.

Window handles are plain ints on both platforms (HWND / X11 window id), so the
rest of the app and the browser API don't care which backend is active.
"""

from __future__ import annotations

import sys
import threading
from typing import Callable, Protocol

Rect = tuple[int, int, int, int]  # x, y, w, h (absolute screen pixels)

MIN_WIDTH, MIN_HEIGHT = 150, 80  # ignore tool-tips, tray stubs, etc.


class WindowBackend(Protocol):
    def list(self) -> list[dict]: ...
    def rect(self, handle: int) -> Rect | None: ...


def _window(handle: int, title: str, rect: Rect) -> dict:
    x, y, w, h = rect
    return {"hwnd": handle, "title": title, "x": x, "y": y, "width": w, "height": h}


class _NullBackend:
    def list(self) -> list[dict]:
        return []

    def rect(self, handle: int) -> Rect | None:
        return None


class _Win32Backend:
    # Desktop / shell surfaces that are "visible" but aren't apps a user would share.
    _SHELL_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"})
    _DWMWA_CLOAKED = 14

    def __init__(self):
        import ctypes
        import win32gui
        self._gui = win32gui
        self._ctypes = ctypes
        try:
            self._dwm = ctypes.windll.dwmapi
        except OSError:
            self._dwm = None

    def _cloaked(self, hwnd) -> bool:
        """UWP/background windows (e.g. 'Microsoft Text Input Application') are
        IsWindowVisible() but cloaked by DWM — the user never sees them."""
        if not self._dwm:
            return False
        value = self._ctypes.c_int(0)
        res = self._dwm.DwmGetWindowAttribute(self._ctypes.c_void_p(hwnd), self._DWMWA_CLOAKED,
                                              self._ctypes.byref(value), self._ctypes.sizeof(value))
        return res == 0 and value.value != 0

    def list(self) -> list[dict]:
        gui, out = self._gui, []

        def _cb(hwnd, _):
            if not gui.IsWindowVisible(hwnd) or self._cloaked(hwnd):
                return
            if gui.GetClassName(hwnd) in self._SHELL_CLASSES:
                return
            title = gui.GetWindowText(hwnd)
            r = self.rect(hwnd) if title else None
            if r and r[2] >= MIN_WIDTH and r[3] >= MIN_HEIGHT:
                out.append(_window(hwnd, title, r))

        gui.EnumWindows(_cb, None)
        return out

    def rect(self, handle: int) -> Rect | None:
        try:
            l, t, r, b = self._gui.GetWindowRect(handle)
        except Exception:
            return None
        return (l, t, r - l, b - t) if r > l and b > t else None


class _X11Backend:
    """Uses the EWMH _NET_CLIENT_LIST that every modern X11 window manager
    (GNOME/Xorg, KDE, XFCE, Cinnamon, i3 …) maintains."""

    def __init__(self):
        from Xlib import X, display  # python-xlib
        self._X = X
        self._display_mod = display
        self._d = None
        self._lock = threading.Lock()  # python-xlib connections are not thread-safe

    def _display(self):
        if self._d is None:
            d = self._display_mod.Display()
            self._atoms = {n: d.intern_atom(n) for n in (
                "_NET_CLIENT_LIST", "_NET_WM_NAME", "UTF8_STRING", "_NET_WM_STATE", "_NET_WM_STATE_HIDDEN")}
            self._d = d
        return self._d

    def _reset(self):
        try:
            if self._d:
                self._d.close()
        except Exception:
            pass
        self._d = None

    def _title(self, w) -> str:
        try:
            prop = w.get_full_property(self._atoms["_NET_WM_NAME"], self._atoms["UTF8_STRING"])
            if prop and prop.value:
                v = prop.value
                return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
            name = w.get_wm_name()
            return name.decode("latin-1") if isinstance(name, bytes) else (name or "")
        except Exception:
            return ""

    def _hidden(self, w) -> bool:
        try:
            prop = w.get_full_property(self._atoms["_NET_WM_STATE"], self._X.AnyPropertyType)
            return bool(prop) and self._atoms["_NET_WM_STATE_HIDDEN"] in prop.value
        except Exception:
            return False

    def _rect_of(self, d, w) -> Rect | None:
        try:
            geom = w.get_geometry()
            origin = w.translate_coords(d.screen().root, 0, 0)  # root origin in window coords
            return (-origin.x, -origin.y, geom.width, geom.height)
        except Exception:
            return None

    def list(self) -> list[dict]:
        with self._lock:
            try:
                d = self._display()
                prop = d.screen().root.get_full_property(self._atoms["_NET_CLIENT_LIST"], self._X.AnyPropertyType)
                out = []
                for wid in (prop.value if prop else []):
                    w = d.create_resource_object("window", int(wid))
                    title = self._title(w)
                    if not title or self._hidden(w):
                        continue
                    r = self._rect_of(d, w)
                    if r and r[2] >= MIN_WIDTH and r[3] >= MIN_HEIGHT:
                        out.append(_window(int(wid), title, r))
                return out
            except Exception as exc:
                print(f"[Windows] X11 enumeration failed: {exc}")
                self._reset()
                return []

    def rect(self, handle: int) -> Rect | None:
        with self._lock:
            try:
                d = self._display()
                return self._rect_of(d, d.create_resource_object("window", int(handle)))
            except Exception:
                self._reset()
                return None


def _make_backend() -> WindowBackend:
    try:
        if sys.platform == "win32":
            return _Win32Backend()
        if sys.platform.startswith("linux"):
            return _X11Backend()
    except ImportError as exc:
        print(f"[Windows] Window enumeration unavailable: {exc}")
    return _NullBackend()


_backend = _make_backend()
WINDOW_CAPTURE_AVAILABLE = not isinstance(_backend, _NullBackend)


def list_windows() -> list[dict]:
    """Visible top-level application windows, sorted by title."""
    return sorted(_backend.list(), key=lambda w: w["title"].lower())


def get_window_rect(handle: int) -> Rect | None:
    return _backend.rect(handle)


def find_window(predicate: Callable[[str], bool]) -> dict | None:
    return next((w for w in list_windows() if predicate(w["title"])), None)
