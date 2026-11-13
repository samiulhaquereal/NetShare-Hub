"""Tiny in-process change notifier used to drive Server-Sent Events.

Producers call ``bump(topic)``; SSE streams block on ``wait`` instead of
polling the filesystem every second per connected browser.
"""

from __future__ import annotations

import threading


class ChangeNotifier:
    def __init__(self):
        self._cond = threading.Condition()
        self._versions: dict[str, int] = {}

    def bump(self, topic: str) -> None:
        with self._cond:
            self._versions[topic] = self._versions.get(topic, 0) + 1
            self._cond.notify_all()

    def snapshot(self) -> dict[str, int]:
        with self._cond:
            return dict(self._versions)

    def wait(self, since: dict[str, int], timeout: float) -> dict[str, int]:
        """Block until any topic version differs from `since` (or timeout)."""
        with self._cond:
            self._cond.wait_for(lambda: self._versions != since, timeout=timeout)
            return dict(self._versions)
