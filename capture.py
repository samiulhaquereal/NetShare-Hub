"""
capture.py — High-Performance 60fps Screen Capture Engine for ScreenShare

Architecture:
  - Dedicated capture thread runs at target FPS (up to 60)
  - Frames stored in a shared latest-frame buffer (not a queue)
  - A threading.Event signals new frame availability to consumers
  - Supports: full monitor, specific monitor, custom region, application window
"""

import queue
import threading
import time
import cv2
import mss
import numpy as np

# Optional: pywin32 for window enumeration / window capture
try:
    import win32gui
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False


# ── Source type constants ─────────────────────────────────────────────

SOURCE_MONITOR = "monitor"
SOURCE_REGION  = "region"
SOURCE_WINDOW  = "window"


# ── Window / monitor enumeration helpers ─────────────────────────────

def list_monitors() -> list[dict]:
    """Return a list of all monitors detected by mss."""
    with mss.mss() as sct:
        result = []
        for i, m in enumerate(sct.monitors[1:], start=1):  # skip [0] = all-combined
            result.append({
                "index":  i,
                "name":   f"Monitor {i}",
                "x":      m["left"],
                "y":      m["top"],
                "width":  m["width"],
                "height": m["height"],
            })
        return result


def list_windows() -> list[dict]:
    """Return a list of visible top-level application windows (Windows only)."""
    if not WIN32_AVAILABLE:
        return []

    windows: list[dict] = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return
        try:
            rect = win32gui.GetWindowRect(hwnd)
            w = rect[2] - rect[0]
            h = rect[3] - rect[1]
            if w < 150 or h < 80:
                return  # skip tiny/invisible windows
            windows.append({
                "hwnd":   hwnd,
                "title":  title,
                "x":      rect[0],
                "y":      rect[1],
                "width":  w,
                "height": h,
            })
        except Exception:
            pass

    win32gui.EnumWindows(_cb, None)
    windows.sort(key=lambda w: w["title"].lower())
    return windows


def get_window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """Get (left, top, width, height) for a window HWND. Returns None if unavailable."""
    if not WIN32_AVAILABLE:
        return None
    try:
        rect = win32gui.GetWindowRect(hwnd)
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        if w > 0 and h > 0:
            return rect[0], rect[1], w, h
    except Exception:
        pass
    return None


# ── Screen Capture engine ─────────────────────────────────────────────

class ScreenCapture:
    """
    High-performance screen capture. Runs a dedicated background thread
    targeting up to 60 FPS. Uses a shared latest-frame buffer so consumers
    always get the newest frame with zero queue delay.
    """

    def __init__(self, target_fps: int = 60, jpeg_quality: int = 80, max_width: int = 1280):
        self.target_fps   = target_fps
        self.jpeg_quality = jpeg_quality
        self.max_width    = max_width   # 0 = native resolution

        self.is_running   = False
        self._thread: threading.Thread | None = None

        # ── Source config ──
        self._source_type    = SOURCE_MONITOR
        self._monitor_index  = 1        # mss 1-based
        self._region: dict | None = None
        self._window_hwnd: int | None = None
        self._window_title: str = ""
        self._source_lock    = threading.Lock()

        # ── Latest frame buffer ──
        self._latest_frame: bytes | None = None
        self._frame_lock   = threading.Lock()
        # Event: set briefly whenever a new frame lands; consumers wait on it
        self._frame_event  = threading.Event()

        # ── Stats ──
        self.actual_fps   = 0.0
        self.frame_count  = 0
        self._fps_timer   = time.perf_counter()
        self._fps_frames  = 0

    # ------------------------------------------------------------------
    # Source setters  (thread-safe, take effect on the next grab)
    # ------------------------------------------------------------------

    def set_source_monitor(self, index: int = 1) -> None:
        with self._source_lock:
            self._source_type   = SOURCE_MONITOR
            self._monitor_index = max(1, index)

    def set_source_region(self, x: int, y: int, w: int, h: int) -> None:
        with self._source_lock:
            self._source_type = SOURCE_REGION
            self._region = {"top": y, "left": x, "width": w, "height": h}

    def set_source_window(self, hwnd: int, title: str = "") -> None:
        with self._source_lock:
            self._source_type  = SOURCE_WINDOW
            self._window_hwnd  = hwnd
            self._window_title = title

    def get_source_info(self) -> dict:
        with self._source_lock:
            return {
                "type":          self._source_type,
                "monitor_index": self._monitor_index,
                "window_hwnd":   self._window_hwnd,
                "window_title":  self._window_title,
                "region":        self._region.copy() if self._region else None,
            }

    def get_active_rect(self) -> tuple[int, int, int, int]:
        """
        Return (x, y, w, h) of the current capture source in absolute screen coordinates.
        Used for mapping remote terminal coordinates to the correct screen location.
        """
        with self._source_lock:
            src   = self._source_type
            midx  = self._monitor_index
            rgn   = self._region.copy() if self._region else None
            hwnd  = self._window_hwnd

        if src == SOURCE_MONITOR:
            with mss.mss() as sct:
                monitors = sct.monitors
                idx = midx if midx < len(monitors) else 1
                m = monitors[idx]
                return m["left"], m["top"], m["width"], m["height"]

        if src == SOURCE_REGION and rgn:
            return rgn["left"], rgn["top"], rgn["width"], rgn["height"]

        if src == SOURCE_WINDOW and hwnd:
            r = get_window_rect(hwnd)
            if r: return r  # (x, y, w, h)

        # Fallback to primary monitor
        with mss.mss() as sct:
            m = sct.monitors[1]
            return m["left"], m["top"], m["width"], m["height"]


    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        self._frame_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="CaptureThread"
        )
        self._thread.start()

    def stop(self) -> None:
        self.is_running = False
        self._frame_event.set()   # unblock any threads waiting for a frame
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def restart(self) -> None:
        self.stop()
        self.start()

    # ------------------------------------------------------------------
    # Capture loop
    # ------------------------------------------------------------------

    def _capture_loop(self) -> None:
        interval = 1.0 / max(1, self.target_fps)

        with mss.mss() as sct:
            while self.is_running:
                t0 = time.perf_counter()

                try:
                    img = self._grab(sct)
                    if img is None:
                        time.sleep(0.05)
                        continue

                    # ── Optional downscale ──
                    if self.max_width > 0 and img.shape[1] > self.max_width:
                        scale = self.max_width / img.shape[1]
                        new_h = int(img.shape[0] * scale)
                        img = cv2.resize(img, (self.max_width, new_h),
                                         interpolation=cv2.INTER_LINEAR)

                    # ── JPEG encode ──
                    ok, buf = cv2.imencode(
                        ".jpg", img,
                        [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
                    )
                    if not ok:
                        continue

                    jpeg = buf.tobytes()

                    # ── Atomic frame update ──
                    with self._frame_lock:
                        self._latest_frame = jpeg

                    # Signal once — consumers that were waiting get woken up
                    self._frame_event.set()
                    self._frame_event.clear()

                    # Trigger external callback if set
                    if hasattr(self, 'on_frame_ready') and self.on_frame_ready:
                        self.on_frame_ready()

                    # ── Stats ──
                    self.frame_count   += 1
                    self._fps_frames   += 1
                    now     = time.perf_counter()
                    elapsed = now - self._fps_timer
                    if elapsed >= 1.0:
                        self.actual_fps   = self._fps_frames / elapsed
                        self._fps_frames  = 0
                        self._fps_timer   = now

                except Exception as exc:
                    print(f"[Capture] {exc}")
                    time.sleep(0.05)
                    continue

                # ── Precise FPS throttle ──
                spent = time.perf_counter() - t0
                # Re-read target_fps in case it changed
                interval = 1.0 / max(1, self.target_fps)
                sleep_t = interval - spent
                if sleep_t > 0.0005:
                    time.sleep(sleep_t)

    def _grab(self, sct) -> np.ndarray | None:
        """Grab one frame from the active source. Returns BGR numpy array or None."""
        with self._source_lock:
            src   = self._source_type
            midx  = self._monitor_index
            rgn   = self._region.copy() if self._region else None
            hwnd  = self._window_hwnd

        # ── Monitor ──
        if src == SOURCE_MONITOR:
            monitors = sct.monitors
            idx = midx if midx < len(monitors) else 1
            raw = sct.grab(monitors[idx])
            return np.array(raw)[:, :, :3]

        # ── Region ──
        if src == SOURCE_REGION and rgn:
            raw = sct.grab(rgn)
            return np.array(raw)[:, :, :3]

        # ── Window ──
        if src == SOURCE_WINDOW and hwnd:
            r = get_window_rect(hwnd)
            if r:
                x, y, w, h = r
                region = {"top": y, "left": x, "width": w, "height": h}
                try:
                    raw = sct.grab(region)
                    return np.array(raw)[:, :, :3]
                except Exception:
                    pass

        # Fallback — primary monitor
        raw = sct.grab(sct.monitors[1])
        return np.array(raw)[:, :, :3]

    # ------------------------------------------------------------------
    # Frame access
    # ------------------------------------------------------------------

    def get_latest_frame(self) -> bytes | None:
        """Non-blocking: return the most recent JPEG bytes (or None)."""
        with self._frame_lock:
            return self._latest_frame

    def wait_for_next_frame(self, timeout: float = 0.05) -> bytes | None:
        """
        Block (up to *timeout* seconds) until a new frame is produced,
        then return it. Used by the WebSocket broadcaster.
        """
        self._frame_event.wait(timeout)
        with self._frame_lock:
            return self._latest_frame

    # ------------------------------------------------------------------
    # Config (hot-change, applied next loop iteration)
    # ------------------------------------------------------------------

    def set_fps(self, fps: int) -> None:
        self.target_fps = max(1, min(int(fps), 60))

    def set_quality(self, quality: int) -> None:
        self.jpeg_quality = max(20, min(int(quality), 100))

    def set_max_width(self, width: int) -> None:
        self.max_width = max(0, int(width))

    def get_stats(self) -> dict:
        return {
            "fps":         round(self.actual_fps, 1),
            "target_fps":  self.target_fps,
            "quality":     self.jpeg_quality,
            "max_width":   self.max_width,
            "frame_count": self.frame_count,
            "is_running":  self.is_running,
            "source":      self.get_source_info(),
        }


# ═══════════════════════════════════════════════════════════════════════
# Audio Capture — WASAPI Loopback ("What You Hear")
# ═══════════════════════════════════════════════════════════════════════

class AudioCapture:
    """
    Captures system audio output (speaker loopback) using WASAPI via pyaudiowpatch.
    Produces raw Int16 LE PCM chunks for WebSocket streaming.

    The sample_rate and channels are auto-detected from the default output device.
    Call get_info() after start() to retrieve them for the client config message.
    """

    # ~11.6 ms per chunk at 44100 Hz — small enough for low latency
    CHUNK_FRAMES = 512

    def __init__(self):
        self.is_running   = False
        self.enabled      = True          # soft mute: still capture, just don't enqueue
        self._thread: threading.Thread | None = None
        self._queue: queue.Queue = queue.Queue(maxsize=60)

        # Filled after the device is successfully opened
        self.available   = False
        self.sample_rate = 44100
        self.channels    = 2
        self.device_name = "Not found"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="AudioThread"
        )
        self._thread.start()

    def stop(self) -> None:
        self.is_running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ------------------------------------------------------------------
    # Frame access  (called from the WS broadcaster)
    # ------------------------------------------------------------------

    def get_chunk(self) -> bytes | None:
        """Non-blocking: return the oldest queued PCM chunk or None."""
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    # ------------------------------------------------------------------
    # Capture loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            print("[Audio] pyaudiowpatch not installed — no audio streaming.")
            self.is_running = False
            return

        pa     = pyaudio.PyAudio()
        stream = None

        try:
            # ── Find WASAPI host API ──
            wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_out = pa.get_device_info_by_index(
                wasapi_info["defaultOutputDevice"]
            )

            # ── Find the loopback variant of the default output ──
            loopback = None
            for lb in pa.get_loopback_device_info_generator():
                if default_out["name"] in lb["name"]:
                    loopback = lb
                    break

            if loopback is None:
                print("[Audio] WASAPI loopback device not found — no audio streaming.")
                self.is_running = False
                return

            # Store device info for client config
            self.sample_rate = int(loopback["defaultSampleRate"])
            self.channels    = min(int(loopback["maxInputChannels"]), 2)
            self.device_name = loopback["name"]
            self.available   = True

            print(f"[Audio] Device  : {self.device_name}")
            print(f"[Audio] Format  : {self.sample_rate} Hz, {self.channels}ch")

            stream = pa.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.sample_rate,
                frames_per_buffer=self.CHUNK_FRAMES,
                input=True,
                input_device_index=loopback["index"],
            )

            while self.is_running:
                try:
                    raw = stream.read(self.CHUNK_FRAMES, exception_on_overflow=False)
                    if self.enabled:
                        # Drop the oldest chunk if the queue is full
                        if self._queue.full():
                            try:
                                self._queue.get_nowait()
                            except queue.Empty:
                                pass
                        self._queue.put_nowait(raw)
                except Exception:
                    time.sleep(0.01)  # brief pause on error, then retry

        except Exception as exc:
            print(f"[Audio] Error: {exc}")
        finally:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            try:
                pa.terminate()
            except Exception:
                pass
            self.is_running = False

    # ------------------------------------------------------------------
    # Info
    # ------------------------------------------------------------------

    def get_info(self) -> dict:
        return {
            "available":   self.available,
            "is_running":  self.is_running,
            "enabled":     self.enabled,
            "sample_rate": self.sample_rate,
            "channels":    self.channels,
            "device_name": self.device_name,
        }
