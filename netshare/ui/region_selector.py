"""Full-screen translucent overlay for dragging out a capture region."""

from __future__ import annotations

import tkinter as tk
from typing import Callable


class RegionSelector(tk.Toplevel):
    def __init__(self, callback: Callable[[int, int, int, int], None]):
        super().__init__()
        self._callback = callback
        self._sx = self._sy = 0
        self._rect = None

        self.attributes("-fullscreen", True)
        self.attributes("-alpha", 0.28)
        self.attributes("-topmost", True)
        self.configure(bg="#000011")

        self._cv = tk.Canvas(self, cursor="crosshair", bg="#000011", highlightthickness=0)
        self._cv.pack(fill="both", expand=True)
        self._cv.create_text(self.winfo_screenwidth() // 2, 44,
                             text="Click and drag to select region  |  Esc to cancel",
                             fill="white", font=("Segoe UI", 15, "bold"))
        self._cv.bind("<ButtonPress-1>", self._press)
        self._cv.bind("<B1-Motion>", self._drag)
        self._cv.bind("<ButtonRelease-1>", self._release)
        self.bind("<Escape>", lambda _e: self._finish(0, 0, 0, 0))

    def _press(self, e):
        self._sx, self._sy = e.x, e.y
        if self._rect:
            self._cv.delete(self._rect)

    def _drag(self, e):
        if self._rect:
            self._cv.coords(self._rect, self._sx, self._sy, e.x, e.y)
        else:
            self._rect = self._cv.create_rectangle(self._sx, self._sy, e.x, e.y,
                                                   outline="#7C3AED", width=3, fill="#3B1F6E")

    def _release(self, e):
        self._finish(min(self._sx, e.x), min(self._sy, e.y), abs(e.x - self._sx), abs(e.y - self._sy))

    def _finish(self, x, y, w, h):
        self.destroy()
        self._callback(x, y, w, h)
