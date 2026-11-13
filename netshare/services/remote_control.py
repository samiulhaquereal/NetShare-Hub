"""Translate viewer input events (normalised 0..1 coordinates) into host
mouse/keyboard actions. Disabled unless the host ticks "Allow Remote Control"."""

from __future__ import annotations

import json
from typing import Callable

Rect = tuple[int, int, int, int]
_MOUSE_EVENTS = frozenset({"mm", "md", "mu", "clk"})
_KEY_EVENTS = frozenset({"kd", "ku", "kp"})
_BUTTONS = frozenset({"left", "right", "middle"})
# Browser KeyboardEvent.key (lower-cased) -> pyautogui key name
_KEY_ALIASES = {
    "control": "ctrl", "arrowleft": "left", "arrowright": "right", "arrowup": "up",
    "arrowdown": "down", "escape": "esc", "meta": "win", "os": "win",
}


class RemoteController:
    def __init__(self, active_rect: Callable[[], Rect]):
        self._active_rect = active_rect
        self.enabled = False
        self._gui = None

    def _pyautogui(self):
        if self._gui is None:
            import pyautogui  # slow import; only paid when remote control is used
            pyautogui.FAILSAFE = True  # slam the mouse into a corner to abort
            pyautogui.PAUSE = 0        # default 0.1 s sleep after every call made input laggy
            self._gui = pyautogui
        return self._gui

    def handle(self, payload: bytes) -> None:
        if not self.enabled:
            return
        try:
            event = json.loads(payload.decode("utf-8"))
            self._dispatch(event)
        except Exception as exc:
            print(f"[Control] {exc}")

    def _dispatch(self, ev: dict) -> None:
        etype = ev.get("type")
        gui = self._pyautogui()

        if etype in _MOUSE_EVENTS:
            sx, sy, sw, sh = self._active_rect()
            x = int(sx + float(ev.get("x", 0)) * sw)
            y = int(sy + float(ev.get("y", 0)) * sh)
            btn = ev.get("btn", "left")
            btn = btn if btn in _BUTTONS else "left"
            if etype == "mm":
                gui.moveTo(x, y, _pause=False)
            elif etype == "md":
                gui.mouseDown(x, y, button=btn)
            elif etype == "mu":
                gui.mouseUp(x, y, button=btn)
            else:
                gui.click(x, y, button=btn)

        elif etype == "sc":
            v = int(ev.get("v", 0))
            if v:
                gui.scroll(v)

        elif etype in _KEY_EVENTS:
            key = str(ev.get("key") or "").lower()
            if not key:
                return
            key = _KEY_ALIASES.get(key, key)
            if key not in gui.KEYBOARD_KEYS:
                return
            {"kd": gui.keyDown, "ku": gui.keyUp, "kp": gui.press}[etype](key)
