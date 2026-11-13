"""Fan-out of video frames, audio and clipboard updates to /ws/stream viewers.

Per client:
  * a *latest-frame slot* (not a queue) — a slow phone skips frames instead of
    accumulating latency
  * a bounded audio buffer — every client receives the full audio stream
    (previously all clients pulled from one shared queue, so with two viewers
    each one heard only half the chunks)
  * a single sender thread and a send lock, so clipboard pushes from other
    threads can never interleave with a frame write and corrupt the socket
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Protocol

from ..domain.protocol import AUD, CLP, VID


class Socket(Protocol):
    def send(self, data: bytes | str) -> None: ...


class StreamClient:
    AUDIO_BUFFER = 48  # ~0.5 s of 512-frame chunks

    def __init__(self, ws: Socket):
        self._ws = ws
        self._send_lock = threading.Lock()
        self._cond = threading.Condition()
        self._frame: bytes | None = None
        self._audio: deque[bytes] = deque(maxlen=self.AUDIO_BUFFER)
        self.closed = False

    # Producers (capture / audio threads) ----------------------------------
    def offer_frame(self, jpeg: bytes) -> None:
        with self._cond:
            self._frame = jpeg
            self._cond.notify()

    def offer_audio(self, chunk: bytes) -> None:
        with self._cond:
            self._audio.append(chunk)
            self._cond.notify()

    # Transport -------------------------------------------------------------
    def send(self, data: bytes) -> bool:
        if self.closed:
            return False
        try:
            with self._send_lock:
                self._ws.send(data)
            return True
        except Exception:
            self.close()
            return False

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._cond.notify()

    def run_sender(self) -> None:
        """Drain pending audio + the newest frame until the client closes."""
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self.closed or self._frame is not None or self._audio, timeout=1.0)
                if self.closed:
                    return
                frame, self._frame = self._frame, None
                audio = b"".join(self._audio) if self._audio else b""
                self._audio.clear()
            if audio and not self.send(AUD + audio):
                return
            if frame and not self.send(VID + frame):
                return


class StreamHub:
    def __init__(self):
        self._clients: set[StreamClient] = set()
        self._lock = threading.Lock()

    def _snapshot(self) -> list[StreamClient]:
        with self._lock:
            return list(self._clients)

    def add(self, client: StreamClient) -> None:
        with self._lock:
            self._clients.add(client)

    def remove(self, client: StreamClient) -> None:
        client.close()
        with self._lock:
            self._clients.discard(client)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._clients)

    def has_clients(self) -> bool:
        return self.count > 0

    def publish_frame(self, jpeg: bytes) -> None:
        for c in self._snapshot():
            c.offer_frame(jpeg)

    def publish_audio(self, chunk: bytes) -> None:
        for c in self._snapshot():
            c.offer_audio(chunk)

    def publish_clipboard(self, text: str, exclude: StreamClient | None = None) -> None:
        msg = CLP + text.encode("utf-8")
        for c in self._snapshot():
            if c is not exclude:
                c.send(msg)
