"""Pixel grabbers used by ScreenCapture.

MssGrabber   — GDI BitBlt via mss. Works everywhere (Windows, X11), ~35 ms per
               1080p frame on Windows, and cannot tell whether anything changed.
DxgiGrabber  — DXGI Desktop Duplication via dxcam (Windows 8+). ~4 ms per frame on
               the GPU, and when nothing on screen changed Windows says so for free
               (~0.1 ms) — so a static desktop costs almost no CPU.

Both return BGRA ``numpy`` arrays for an absolute desktop rectangle. mss and DXGI
number monitors differently, so DXGI outputs are matched by desktop coordinates.
"""

from __future__ import annotations

import os
import sys

import numpy as np

Rect = tuple[int, int, int, int]  # x, y, w, h


class Unsupported(Exception):
    """This grabber can't capture the requested rectangle (caller falls back)."""


class MssGrabber:
    name = "mss"

    def __init__(self, sct):
        self._sct = sct

    def grab(self, rect: Rect, force: bool = False) -> tuple[np.ndarray | None, bool | None]:
        """Returns (BGRA frame, changed). mss can't detect changes -> changed=None."""
        x, y, w, h = rect
        raw = self._sct.grab({"left": x, "top": y, "width": w, "height": h})
        return np.asarray(raw), None

    def close(self) -> None:
        pass


class DxgiGrabber:
    name = "dxgi"
    MAX_FAILURES = 5

    def __init__(self):
        import dxcam  # Windows only; ImportError -> caller uses mss

        self._dxcam = dxcam
        self._outputs: list[tuple[Rect, object]] = []  # ((left, top, right, bottom), camera)
        for device in range(4):
            for output in range(8):
                try:
                    cam = dxcam.create(device_idx=device, output_idx=output, output_color="BGRA")
                except Exception:
                    break
                if cam is None:
                    break
                d = cam._output.desc.DesktopCoordinates
                self._outputs.append(((d.left, d.top, d.right, d.bottom), cam))
        if not self._outputs:
            raise RuntimeError("no DXGI outputs")
        self._failures = 0
        self._last_cam = None

    def _locate(self, rect: Rect):
        x, y, w, h = rect
        for (l, t, r, b), cam in self._outputs:
            if l <= x and t <= y and x + w <= r and y + h <= b:
                return cam, (x - l, y - t, x - l + w, y - t + h)
        raise Unsupported("rectangle spans monitors or is off-screen")

    def grab(self, rect: Rect, force: bool = False) -> tuple[np.ndarray | None, bool | None]:
        """Returns (BGRA frame, True) or (None, False) when nothing changed."""
        cam, region = self._locate(rect)
        # A different monitor / region than last time must always yield a frame.
        force = force or cam is not self._last_cam
        try:
            frame = cam.grab(region=region, new_frame_only=not force)
        except Exception as exc:
            self._failures += 1
            if self._failures >= self.MAX_FAILURES:
                raise RuntimeError(f"DXGI capture keeps failing: {exc}") from exc
            raise Unsupported(str(exc)) from exc
        self._failures = 0
        self._last_cam = cam
        if frame is None:
            return None, False
        return frame, True

    def close(self) -> None:
        # Deliberately no cam.release(): dxcam keeps its own per-output camera cache,
        # and releasing here made comtypes free the COM objects twice (access
        # violations). The instance is a process-wide singleton and reused.
        self._last_cam = None


_dxgi_singleton: DxgiGrabber | None = None
_dxgi_failed = False


def make_fast_grabber():
    """DXGI grabber when available (Windows + dxcam), else None.
    Set NETSHARE_CAPTURE=mss to force the portable path."""
    global _dxgi_singleton, _dxgi_failed
    if sys.platform != "win32" or os.getenv("NETSHARE_CAPTURE", "").lower() == "mss" or _dxgi_failed:
        return None
    if _dxgi_singleton is None:
        try:
            _dxgi_singleton = DxgiGrabber()
        except Exception as exc:  # ImportError, no GPU output (RDP / VM), driver issues
            print(f"[Capture] DXGI unavailable ({exc}) — using mss")
            _dxgi_failed = True
            return None
    _dxgi_singleton._failures = 0
    return _dxgi_singleton
