"""Screen capture engine (mss — works on Windows and X11 Linux).

ScreenCapture
  * one background thread, grabs -> downscales -> JPEG-encodes once, then hands
    the bytes to ``on_frame`` (fan-out to clients happens in StreamHub)
  * idles when nobody is watching (``has_consumers``)
  * skips re-encoding/sending identical frames, with a 1 s keep-alive
"""

from __future__ import annotations

import threading
import time
import zlib
from typing import Callable

import cv2
import mss
import numpy as np

from .windows import Rect, find_window, get_window_rect, list_windows  # noqa: F401  (re-exported)

SOURCE_MONITOR = "monitor"
SOURCE_REGION = "region"
SOURCE_WINDOW = "window"


def list_monitors() -> list[dict]:
    with mss.mss() as sct:
        return [{"index": i, "name": f"Monitor {i}", "x": m["left"], "y": m["top"],
                 "width": m["width"], "height": m["height"]}
                for i, m in enumerate(sct.monitors[1:], start=1)]


def _as_bgra(raw) -> np.ndarray:
    """Zero-copy (h, w, 4) BGRA view over an mss screenshot buffer."""
    return np.asarray(raw)


_thumb_lock = threading.Lock()


def grab_thumbnail(region: dict | None = None, monitor_index: int = 1,
                   size: tuple[int, int] = (256, 144), quality: int = 70) -> bytes:
    """Small JPEG of a monitor or region. Serialised: mss is not thread-safe."""
    if not _thumb_lock.acquire(timeout=0.5):
        return b""
    try:
        with mss.mss() as sct:
            if region:
                raw = sct.grab(region)
            else:
                mons = sct.monitors
                raw = sct.grab(mons[monitor_index if 0 < monitor_index < len(mons) else 1])
            img = _as_bgra(raw)
            h, w = img.shape[:2]
            scale = min(size[0] / w, size[1] / h)
            img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", cv2.cvtColor(img, cv2.COLOR_BGRA2BGR),
                                   [cv2.IMWRITE_JPEG_QUALITY, quality])
            return buf.tobytes() if ok else b""
    except Exception as exc:
        print(f"[Thumb] {exc}")
        return b""
    finally:
        _thumb_lock.release()


# ── Screen capture ────────────────────────────────────────────────────

class ScreenCapture:
    KEEPALIVE_SECONDS = 1.0   # resend an unchanged frame at least this often
    IDLE_POLL_SECONDS = 0.1   # sleep while no client is connected

    def __init__(self, target_fps: int = 30, jpeg_quality: int = 70, max_width: int = 1280):
        self.target_fps = target_fps
        self.jpeg_quality = jpeg_quality
        self.max_width = max_width  # 0 = native

        self.on_frame: Callable[[bytes], None] | None = None
        self.has_consumers: Callable[[], bool] = lambda: True

        self.is_running = False
        self._thread: threading.Thread | None = None

        self._source_lock = threading.Lock()
        self._source_type = SOURCE_MONITOR
        self._monitor_index = 1
        self._region: dict | None = None
        self._window_hwnd: int | None = None
        self._window_title = ""
        self._force_frame = threading.Event()

        self._latest_frame: bytes | None = None
        self._active_rect: Rect | None = None  # updated by the capture thread

        self.actual_fps = 0.0
        self.frame_count = 0

    # ── Source selection ──────────────────────────────────────────────
    def set_source_monitor(self, index: int = 1) -> None:
        with self._source_lock:
            self._source_type, self._monitor_index = SOURCE_MONITOR, max(1, index)
        self._force_frame.set()

    def set_source_region(self, x: int, y: int, w: int, h: int) -> None:
        with self._source_lock:
            self._source_type = SOURCE_REGION
            self._region = {"top": y, "left": x, "width": w, "height": h}
        self._force_frame.set()

    def set_source_window(self, hwnd: int, title: str = "") -> None:
        with self._source_lock:
            self._source_type, self._window_hwnd, self._window_title = SOURCE_WINDOW, hwnd, title
        self._force_frame.set()

    def get_source_info(self) -> dict:
        with self._source_lock:
            return {"type": self._source_type, "monitor_index": self._monitor_index,
                    "window_hwnd": self._window_hwnd, "window_title": self._window_title,
                    "region": dict(self._region) if self._region else None}

    def get_active_rect(self) -> Rect:
        """Absolute screen rect of the current source (for remote-control mapping).
        Served from the capture thread's cache — no new mss instance per mouse move."""
        rect = self._active_rect
        if rect:
            return rect
        with mss.mss() as sct:
            return self._resolve_rect(sct.monitors)

    def _resolve_rect(self, monitors: list[dict]) -> Rect:
        with self._source_lock:
            src, midx, rgn, hwnd = self._source_type, self._monitor_index, self._region, self._window_hwnd
        if src == SOURCE_REGION and rgn:
            return rgn["left"], rgn["top"], rgn["width"], rgn["height"]
        if src == SOURCE_WINDOW and hwnd:
            r = get_window_rect(hwnd)
            if r:
                return r
        m = monitors[midx if src == SOURCE_MONITOR and 0 < midx < len(monitors) else 1]
        return m["left"], m["top"], m["width"], m["height"]

    # ── Lifecycle ─────────────────────────────────────────────────────
    def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="CaptureThread")
        self._thread.start()

    def stop(self) -> None:
        self.is_running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def request_frame(self) -> None:
        """Force the next frame out even if unchanged (e.g. new viewer joined)."""
        self._force_frame.set()

    def get_latest_frame(self) -> bytes | None:
        return self._latest_frame

    # ── Loop ──────────────────────────────────────────────────────────
    def _loop(self) -> None:
        last_crc: int | None = None
        last_sent = 0.0
        fps_frames, fps_timer = 0, time.perf_counter()

        with mss.mss() as sct:
            while self.is_running:
                t0 = time.perf_counter()
                if not self.has_consumers():
                    self.actual_fps = 0.0
                    last_crc = None
                    time.sleep(self.IDLE_POLL_SECONDS)
                    continue
                try:
                    img = self._grab(sct)

                    if self.max_width > 0 and img.shape[1] > self.max_width:
                        new_h = int(img.shape[0] * self.max_width / img.shape[1])
                        img = cv2.resize(img, (self.max_width, new_h), interpolation=cv2.INTER_LINEAR)

                    crc = zlib.crc32(img)
                    forced = self._force_frame.is_set()
                    if crc != last_crc or forced or (t0 - last_sent) >= self.KEEPALIVE_SECONDS:
                        self._force_frame.clear()
                        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(img, cv2.COLOR_BGRA2BGR),
                                               [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
                        if ok:
                            jpeg = buf.tobytes()
                            self._latest_frame = jpeg
                            last_crc, last_sent = crc, t0
                            self.frame_count += 1
                            fps_frames += 1
                            if self.on_frame:
                                self.on_frame(jpeg)
                except Exception as exc:
                    print(f"[Capture] {exc}")
                    time.sleep(0.05)
                    continue

                now = time.perf_counter()
                if now - fps_timer >= 1.0:
                    self.actual_fps = fps_frames / (now - fps_timer)
                    fps_frames, fps_timer = 0, now

                sleep_t = 1.0 / max(1, self.target_fps) - (time.perf_counter() - t0)
                if sleep_t > 0.0005:
                    time.sleep(sleep_t)

    def _grab(self, sct) -> np.ndarray:
        rect = self._resolve_rect(sct.monitors)
        try:
            raw = sct.grab(self._rect_dict(rect))
        except Exception:
            # Window moved off-screen / region invalid -> fall back to the primary monitor.
            m = sct.monitors[1]
            rect = (m["left"], m["top"], m["width"], m["height"])
            raw = sct.grab(self._rect_dict(rect))
        self._active_rect = rect
        return _as_bgra(raw)

    @staticmethod
    def _rect_dict(rect: Rect) -> dict:
        x, y, w, h = rect
        return {"left": x, "top": y, "width": w, "height": h}

    # ── Tuning ────────────────────────────────────────────────────────
    def set_fps(self, fps: int) -> None:
        self.target_fps = max(1, min(int(fps), 60))

    def set_quality(self, quality: int) -> None:
        self.jpeg_quality = max(20, min(int(quality), 100))
        self._force_frame.set()

    def set_max_width(self, width: int) -> None:
        self.max_width = max(0, int(width))
        self._force_frame.set()

    def get_stats(self) -> dict:
        return {"fps": round(self.actual_fps, 1), "target_fps": self.target_fps,
                "quality": self.jpeg_quality, "max_width": self.max_width,
                "frame_count": self.frame_count, "is_running": self.is_running,
                "source": self.get_source_info()}
