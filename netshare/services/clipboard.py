"""Shared clipboard: one text value synced between every connected device."""

from __future__ import annotations

import threading

from .events import ChangeNotifier
from .stream_hub import StreamClient, StreamHub

TOPIC = "clipboard"


class ClipboardService:
    def __init__(self, hub: StreamHub, notifier: ChangeNotifier):
        self._hub = hub
        self._notifier = notifier
        self._lock = threading.Lock()
        self.text = ""
        self.version = 0

    def update(self, text: str, origin: StreamClient | None = None) -> bool:
        with self._lock:
            if text == self.text:
                return False
            self.text = text
            self.version += 1
        self._hub.publish_clipboard(text, exclude=origin)
        self._notifier.bump(TOPIC)
        return True
