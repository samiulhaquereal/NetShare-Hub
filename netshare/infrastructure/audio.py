"""System audio ("what you hear") capture as Int16 LE PCM chunks.

Backends
  * Windows: WASAPI loopback of the default output device (pyaudiowpatch)
  * Linux:   monitor source of the default sink via ``parec`` — works with both
             PulseAudio and PipeWire (pipewire-pulse); needs ``pulseaudio-utils``
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Protocol

CHUNK_FRAMES = 512  # ~11 ms at 44.1/48 kHz — low latency


class AudioBackend(Protocol):
    sample_rate: int
    channels: int
    device_name: str

    def open(self) -> None: ...
    def read(self) -> bytes: ...
    def close(self) -> None: ...


class WasapiLoopback:
    def __init__(self):
        self.sample_rate, self.channels, self.device_name = 44100, 2, "Not found"
        self._pa = self._stream = None

    def open(self) -> None:
        import pyaudiowpatch as pyaudio

        self._pa = pa = pyaudio.PyAudio()
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        loopback = next((lb for lb in pa.get_loopback_device_info_generator()
                         if default_out["name"] in lb["name"]), None)
        if loopback is None:
            raise RuntimeError("WASAPI loopback device not found")
        self.sample_rate = int(loopback["defaultSampleRate"])
        self.channels = min(int(loopback["maxInputChannels"]), 2)
        self.device_name = loopback["name"]
        self._stream = pa.open(format=pyaudio.paInt16, channels=self.channels, rate=self.sample_rate,
                               frames_per_buffer=CHUNK_FRAMES, input=True, input_device_index=loopback["index"])

    def read(self) -> bytes:
        return self._stream.read(CHUNK_FRAMES, exception_on_overflow=False)

    def close(self) -> None:
        for fn in (lambda: self._stream.stop_stream(), lambda: self._stream.close(), lambda: self._pa.terminate()):
            try:
                fn()
            except Exception:
                pass


class PulseMonitor:
    """Records the default sink's monitor with ``parec``. ``@DEFAULT_MONITOR@`` is
    understood by PulseAudio and pipewire-pulse, and follows output changes."""

    DEVICE = "@DEFAULT_MONITOR@"

    def __init__(self, sample_rate: int = 48000, channels: int = 2):
        self.sample_rate, self.channels = sample_rate, channels
        self.device_name = "Not found"
        self._proc: subprocess.Popen | None = None
        self._pending = b""
        self._chunk_bytes = CHUNK_FRAMES * channels * 2

    @staticmethod
    def _default_sink() -> str:
        for cmd in (["pactl", "get-default-sink"], ["pactl", "info"]):
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
            except (OSError, subprocess.SubprocessError):
                continue
            if cmd[1] == "get-default-sink" and out.strip():
                return out.strip()
            for line in out.splitlines():
                if line.startswith("Default Sink:"):
                    return line.split(":", 1)[1].strip()
        return "default output"

    def command(self) -> list[str]:
        return ["parec", f"--device={self.DEVICE}", "--format=s16le", f"--rate={self.sample_rate}",
                f"--channels={self.channels}", "--raw", "--latency-msec=20"]

    def open(self) -> None:
        if not shutil.which("parec"):
            raise RuntimeError("'parec' not found — install it with: sudo apt install pulseaudio-utils")
        self._proc = subprocess.Popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        first = self._proc.stdout.read(self._chunk_bytes)  # fails fast if the device is missing
        if not first:
            err = (self._proc.stderr.read() or b"").decode(errors="replace").strip()
            raise RuntimeError(f"parec produced no audio: {err or 'no PulseAudio/PipeWire server?'}")
        self._pending = first
        self.device_name = f"{self._default_sink()} [Monitor]"

    def read(self) -> bytes:
        if self._pending:
            data, self._pending = self._pending, b""
            return data
        data = self._proc.stdout.read(self._chunk_bytes)
        if not data:
            raise EOFError("parec exited")
        return data

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()


def default_backend() -> AudioBackend | None:
    if sys.platform == "win32":
        try:
            import pyaudiowpatch  # noqa: F401
            return WasapiLoopback()
        except ImportError:
            print("[Audio] pyaudiowpatch not installed — no audio streaming.")
            return None
    if sys.platform.startswith("linux"):
        return PulseMonitor()
    return None


class AudioCapture:
    """Runs a backend on a background thread and pushes chunks to ``on_chunk``."""

    def __init__(self, backend_factory: Callable[[], AudioBackend | None] = default_backend):
        self._factory = backend_factory
        self.on_chunk: Callable[[bytes], None] | None = None
        self.enabled = True  # soft mute: keep draining the device, just don't publish
        self.is_running = False
        self._thread: threading.Thread | None = None
        self.available = False
        self.sample_rate = 44100
        self.channels = 2
        self.device_name = "Not found"

    def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="AudioThread")
        self._thread.start()

    def stop(self) -> None:
        self.is_running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        backend = self._factory()
        if backend is None:
            self.is_running = False
            return
        try:
            backend.open()
            self.sample_rate, self.channels = backend.sample_rate, backend.channels
            self.device_name, self.available = backend.device_name, True
            print(f"[Audio] Device  : {self.device_name}")
            print(f"[Audio] Format  : {self.sample_rate} Hz, {self.channels}ch")
            while self.is_running:
                try:
                    chunk = backend.read()
                except EOFError:
                    raise
                except Exception:
                    time.sleep(0.01)
                    continue
                if self.enabled and self.on_chunk:
                    self.on_chunk(chunk)
        except Exception as exc:
            print(f"[Audio] Disabled: {exc}")
        finally:
            backend.close()
            self.is_running = False

    def get_info(self) -> dict:
        return {"available": self.available, "is_running": self.is_running, "enabled": self.enabled,
                "sample_rate": self.sample_rate, "channels": self.channels, "device_name": self.device_name}
