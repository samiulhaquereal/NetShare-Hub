"""Control-panel side of self-updating: background checks, the "new version"
banner, and the progress window that downloads, verifies, installs and restarts.

The heavy lifting lives in ``infrastructure.updater``; this module only shows it.
"""

from __future__ import annotations

import os
import queue
import threading
import time
import webbrowser
from tkinter import messagebox

import customtkinter as ctk

from ..infrastructure.updater import AUTO_INSTALL, Release, UpdateCancelled, UpdateError, Updater

CHECK_EVERY_MS = 6 * 3600 * 1000   # background check interval
FIRST_CHECK_MS = 8000               # after start-up settles
GREEN, GREEN_HOVER = "#10B981", "#059669"
BANNER_BG = ("#ECFDF5", "#0F2A22")
BANNER_TEXT = ("#065F46", "#A7F3D0")
MUTED = ("#6B7280", "#8B95A7")


def _mb(n: float) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


class UpdateMixin:
    """Mixed into ControlPanel (needs: self.c, self._button, self._label, fonts, self.notify)."""

    # ── Set-up ─────────────────────────────────────────────────────────
    def _init_updates(self, header_right, before_widget, header):
        self.updater = Updater(self.c.settings.app_dir / "updates")
        # Tk isn't thread-safe: worker threads queue UI work, the main thread runs it.
        self._ui_queue: queue.Queue = queue.Queue()
        self.after(50, self._drain_ui_queue)
        self._release: Release | None = None
        self._update_btn = ctk.CTkButton(header_right, text="⬆ Update", width=104, height=34, corner_radius=10,
                                         font=self.f_bold, fg_color=GREEN, hover_color=GREEN_HOVER, text_color="#fff",
                                         command=self._start_update)
        self._update_btn_before = before_widget
        self._banner = ctk.CTkFrame(header, fg_color=BANNER_BG, corner_radius=12)
        self._banner.grid_columnconfigure(0, weight=1)
        self._banner_text = ctk.CTkLabel(self._banner, text="", font=self.f_bold, text_color=BANNER_TEXT, anchor="w",
                                         justify="left")
        self._banner_text.grid(row=0, column=0, sticky="ew", padx=(16, 8), pady=10)
        ctk.CTkButton(self._banner, text="Update now", width=110, height=32, corner_radius=9, font=self.f_bold,
                      fg_color=GREEN, hover_color=GREEN_HOVER, text_color="#fff",
                      command=self._start_update).grid(row=0, column=1, padx=4, pady=8)
        ctk.CTkButton(self._banner, text="What's new", width=96, height=32, corner_radius=9, font=self.f_small,
                      fg_color="transparent", border_width=1, border_color=GREEN, text_color=BANNER_TEXT,
                      hover_color=("#D1FAE5", "#14382D"),
                      command=lambda: webbrowser.open(f"https://github.com/{self.updater.repo}/releases/latest")
                      ).grid(row=0, column=2, padx=4, pady=8)
        ctk.CTkButton(self._banner, text="Later", width=64, height=32, corner_radius=9, font=self.f_small,
                      fg_color="transparent", text_color=BANNER_TEXT, hover_color=("#D1FAE5", "#14382D"),
                      command=self._hide_banner).grid(row=0, column=3, padx=(4, 10), pady=8)
        if self.updater.supported:
            self.after(FIRST_CHECK_MS, self._check_updates)

    def _ui_call(self, fn) -> None:
        """Run ``fn`` on the Tk main thread (safe to call from any thread)."""
        self._ui_queue.put(fn)

    def _drain_ui_queue(self):
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception as exc:  # a closed dialog etc. must not stop the queue
                    print(f"[Update] UI: {exc}")
        except queue.Empty:
            pass
        self.after(50, self._drain_ui_queue)

    def _version_text(self) -> str:
        return self.updater.build.label

    # ── Checking ───────────────────────────────────────────────────────
    def _check_updates(self, manual: bool = False):
        if not self.updater.supported:
            if manual:
                self.notify(self.updater.unsupported_reason, "warn")
            return
        if manual:
            self.notify("Checking for updates…")

        def work():
            try:
                rel = self.updater.check()
                self._ui_call(lambda: self._on_checked(rel, manual))
            except UpdateError as exc:
                msg = str(exc)
                self._ui_call(lambda: manual and self.notify(msg, "error"))
        threading.Thread(target=work, daemon=True, name="update-check").start()
        if not manual:
            self.after(CHECK_EVERY_MS, self._check_updates)

    def _on_checked(self, rel: Release | None, manual: bool):
        if rel is None:
            if manual:
                self.notify(f"You're up to date — {self._version_text()}", "ok")
            return
        new = self._release is None or rel.build != self._release.build
        self._release = rel
        self._update_btn.configure(text=f"⬆ Update")
        if not self._update_btn.winfo_ismapped():
            self._update_btn.pack(side="left", padx=(0, 8), before=self._update_btn_before)
        if AUTO_INSTALL and not manual and not self.c.stream_hub.count:  # unattended PC: just do it
            UpdateDialog(self, rel)
            return
        if new or manual:
            self._banner_text.configure(
                text=f"🎉  A new version is available: {rel.label}  ·  you have {self._version_text()}")
            self._banner.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
            self.notify(f"Update available: {rel.label}", "ok")

    def _hide_banner(self):
        self._banner.grid_remove()  # the header button stays until updated

    # ── Updating ───────────────────────────────────────────────────────
    def _start_update(self):
        rel = self._release
        if rel is None:
            return
        viewers = self.c.stream_hub.count
        if viewers and not messagebox.askyesno(
                "Update RetCore OnLAN", f"{viewers} device(s) are connected. They'll reconnect by themselves "
                                        f"after the restart (about 10 seconds).\n\nUpdate now?", parent=self):
            return
        UpdateDialog(self, rel)

    def _restart_for_update(self):
        """Close everything cleanly (ports, capture, AirPlay, unsaved data), then exit;
        the new version is already starting and waits for this process to end."""
        for stop in (self.c.capture.stop, self.c.audio.stop, self.c.airplay.stop, self.c.clipboard.flush):
            try:
                stop()
            except Exception:
                pass
        self.destroy()
        os._exit(0)


class UpdateDialog(ctk.CTkToplevel):
    """Download → verify → test → install → restart, with progress."""

    def __init__(self, panel, rel: Release):
        super().__init__(panel)
        self.panel, self.rel, self.updater = panel, rel, panel.updater
        self.cancel = threading.Event()
        self._started = time.monotonic()
        self.title("Updating RetCore OnLAN")
        self.geometry(f"460x230+{panel.winfo_rootx() + 120}+{panel.winfo_rooty() + 120}")
        self.resizable(False, False)
        self.transient(panel)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.after(150, self.grab_set)
        try:
            panel._set_icon(self)
        except Exception:
            pass

        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text=f"Updating to {rel.label}", font=panel.f_h2, anchor="w").grid(
            row=0, column=0, sticky="ew", padx=22, pady=(20, 4))
        self.step = ctk.CTkLabel(self, text="Downloading…", font=panel.f_body, anchor="w")
        self.step.grid(row=1, column=0, sticky="ew", padx=22)
        self.bar = ctk.CTkProgressBar(self, height=12, corner_radius=6, progress_color=GREEN)
        self.bar.set(0)
        self.bar.grid(row=2, column=0, sticky="ew", padx=22, pady=(12, 6))
        self.detail = ctk.CTkLabel(self, text=f"0 / {_mb(rel.size)}", font=panel.f_small, text_color=MUTED, anchor="w")
        self.detail.grid(row=3, column=0, sticky="ew", padx=22)
        self.btn = ctk.CTkButton(self, text="Cancel", width=100, height=32, corner_radius=9, font=panel.f_bold,
                                 fg_color=("#E5E7EB", "#2A2A44"), hover_color=("#D1D5DB", "#34345A"),
                                 text_color=("#111827", "#E2E8F0"), command=self._cancel)
        self.btn.grid(row=4, column=0, sticky="e", padx=22, pady=(18, 18))
        threading.Thread(target=self._run, daemon=True, name="update").start()

    # thread → UI (through the panel's main-thread queue)
    def _ui(self, fn):
        def guarded():
            if self.winfo_exists():
                fn()
        self.panel._ui_call(guarded)

    def _progress(self, done: int, total: int):
        now = time.monotonic()
        if getattr(self, "_last_paint", 0) > now - 0.1 and done < total:
            return
        self._last_paint = now
        speed = done / max(0.001, now - self._started)
        left = (total - done) / speed if speed > 0 else 0
        eta = f"{int(left)} s left" if left < 90 else f"{int(left // 60)} min left"
        self._ui(lambda: (self.bar.set(done / max(1, total)),
                          self.detail.configure(text=f"{_mb(done)} / {_mb(total)}  ·  {_mb(speed)}/s  ·  {eta}")))

    def _set_step(self, text: str, indeterminate: bool = False, cancellable: bool = True):
        def apply():
            self.step.configure(text=text)
            if indeterminate:
                self.bar.configure(mode="indeterminate")
                self.bar.start()
            self.btn.configure(state="normal" if cancellable else "disabled")
        self._ui(apply)

    def _run(self):
        try:
            path = self.updater.download(self.rel, self._progress, self.cancel)
            self._set_step("Checking the new version…", indeterminate=True)
            self._ui(lambda: self.detail.configure(text="Verified the download (SHA-256) — running its self-test"))
            new = self.updater.prepare(path)
            self.updater.self_test(new)
            if self.cancel.is_set():
                raise UpdateCancelled("Update cancelled")
            self._set_step("Installing…", indeterminate=True, cancellable=False)
            self.updater.install(new)
            self._set_step("Restarting RetCore OnLAN…", indeterminate=True, cancellable=False)
            self._ui(lambda: self.detail.configure(text="The app will be back in a few seconds."))
            self.updater.relaunch(self._relaunch_args())
            self.panel._ui_call(lambda: self.panel.after(400, self.panel._restart_for_update))
        except UpdateCancelled:
            self._ui(self.destroy)
            self.panel._ui_call(lambda: self.panel.notify("Update cancelled", "warn"))
        except UpdateError as exc:
            self._fail(str(exc))
        except Exception as exc:  # pragma: no cover - unexpected
            self._fail(f"Update failed: {exc}")

    @staticmethod
    def _relaunch_args() -> list[str]:
        import sys
        return [a for a in sys.argv[1:] if not a.startswith("--after-update") and not a.isdigit()]

    def _fail(self, message: str):
        def show():
            self._failed = True
            self.bar.stop()
            self.bar.configure(mode="determinate", progress_color="#EF4444")
            self.bar.set(1)
            self.step.configure(text="Update failed — nothing was changed")
            self.detail.configure(text=message, text_color=("#B91C1C", "#FCA5A5"))
            self.btn.configure(text="Close", state="normal", command=self.destroy)
        self._ui(show)

    def _cancel(self):
        if getattr(self, "_failed", False):
            return self.destroy()
        if self.btn.cget("state") == "disabled":
            return  # installing: can't stop half-way
        self.cancel.set()
        self.btn.configure(state="disabled", text="Cancelling…")
