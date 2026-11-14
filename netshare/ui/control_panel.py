"""Host control panel — CustomTkinter, responsive, dark/light.

Layout
  wide  (≥ 940 px): [ connect card · stats · quick toggles ] | [ tabs ]
  narrow:           [ connect card | stats · toggles ]
                    [ tabs                           ]

Pure presentation: every action delegates to a service on the container.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox

import customtkinter as ctk
from PIL import Image

from ..container import Container
from ..infrastructure.capture import list_monitors, list_windows
from ..infrastructure.network import LOOPBACK, get_local_ip, make_qr_image
from ..infrastructure.shell import open_folder
from .region_selector import RegionSelector
from .update_ui import UpdateMixin


class _Tooltip:
    """Small hover hint for a widget (shown after a short delay)."""

    def __init__(self, widget, text: str, delay_ms: int = 450):
        self.widget, self.text, self.delay = widget, text, delay_ms
        self._after = self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._after = self.widget.after(self.delay, self._show)

    def _show(self):
        if self._tip:
            return
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.attributes("-topmost", True)
        tk.Label(self._tip, text=self.text, justify="left", bg="#1e1e38", fg="#E2E8F0",
                 font=("Segoe UI", 9), padx=8, pady=5, relief="solid", borderwidth=1).pack()
        self._tip.update_idletasks()
        x = min(x, self.widget.winfo_screenwidth() - self._tip.winfo_width() - 8)
        self._tip.wm_geometry(f"+{max(0, x)}+{y}")

    def _hide(self, _e=None):
        if self._after:
            self.widget.after_cancel(self._after)
            self._after = None
        if self._tip:
            self._tip.destroy()
            self._tip = None

# ── Palette: (light, dark) ───────────────────────────────────────────
ACCENT, ACCENT_HOVER = "#7C3AED", "#6D28D9"
GREEN, AMBER, RED, RED_HOVER = "#10B981", "#F59E0B", "#EF4444", "#DC2626"
BG = ("#F3F3F8", "#0F0F1A")
CARD = ("#FFFFFF", "#181829")
CARD2 = ("#EEEEF6", "#222238")
HOVER = ("#E2E2EE", "#2C2C48")
TEXT = ("#111827", "#E2E8F0")
MUTED = ("#6B7280", "#8B95A7")
BORDER = ("#E3E3EC", "#2A2A45")

FAMILY = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"
MONO = "Consolas" if sys.platform == "win32" else "DejaVu Sans Mono"
BREAKPOINT = 940
NGROK_TOKEN_URL = "https://dashboard.ngrok.com/get-started/your-authtoken"

# name: (fps, jpeg quality, max width (0 = native), HD-when-still, description)
PRESETS = {
    "Data saver": (15, 55, 960, False, "Lowest Wi-Fi usage — good for slow networks or many viewers."),
    "Balanced": (30, 75, 1280, True, "Smooth while things move, full-HD sharp text the moment the screen is still. "
                                     "Recommended."),
    "Sharp": (30, 85, 0, True, "Full resolution all the time — best for reading documents and code. "
                               "Needs good Wi-Fi."),
    "Smooth": (60, 70, 1280, True, "Highest frame rate for videos and games, HD when still."),
}
WIDTHS = ["640", "960", "1280", "1920", "Native"]


def _ago(ts: float) -> str:
    s = int(time.time() - ts)
    if s < 60:
        return "just now" if s < 5 else f"{s}s ago"
    return f"{s // 60}m ago" if s < 3600 else f"{s // 3600}h ago"


class ControlPanel(UpdateMixin, ctk.CTk):
    def __init__(self, container: Container):
        ctk.set_appearance_mode(container.prefs.get("appearance", "Dark"))
        ctk.set_default_color_theme("blue")
        super().__init__(fg_color=BG)
        self.c = container
        self.title("RetCore OnLAN — Control Panel")
        self.minsize(720, 600)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._set_icon()
        w, h = 1120, 740
        self.geometry(f"{w}x{h}+{max(0, (self.winfo_screenwidth() - w) // 2)}+{max(0, (self.winfo_screenheight() - h) // 3)}")

        self.f_title = ctk.CTkFont(FAMILY, 20, "bold")
        self.f_h2 = ctk.CTkFont(FAMILY, 15, "bold")
        self.f_body = ctk.CTkFont(FAMILY, 13)
        self.f_bold = ctk.CTkFont(FAMILY, 13, "bold")
        self.f_small = ctk.CTkFont(FAMILY, 11)
        self.f_stat = ctk.CTkFont(FAMILY, 20, "bold")
        self.f_small_bold = ctk.CTkFont(FAMILY, 11, "bold")
        self.f_mono = ctk.CTkFont(MONO, 13, "bold")

        self.server_url = self._local_url()
        self._shown_url = None
        self._layout = None
        self._sources: list[dict] = []
        self._source_buttons: list[ctk.CTkButton] = []
        self._devices_sig = None
        self._status_reset = None
        self._busy_public = False

        self._build()
        self.bind("<Configure>", self._on_resize, add="+")
        self.after(50, self._apply_layout)
        self._refresh_sources()
        self._tick()
        self._devices_loop()

    # ── Helpers ───────────────────────────────────────────────────────
    def _local_url(self) -> str:
        return f"http://{get_local_ip()}:{self.c.settings.http_port}"

    def _set_icon(self, window=None):
        """App icon for the title bar / taskbar / Alt-Tab.

        CustomTkinter replaces the window icon with its own logo ~200 ms after
        creation unless iconbitmap() was already called — so on Windows we call it
        with our .ico (all sizes 16–256 px) right away. Linux uses iconphoto().
        """
        window = window or self
        static = self.c.settings.static_dir
        ico, png = os.path.join(static, "icon.ico"), os.path.join(static, "icon.png")
        try:
            if sys.platform == "win32" and os.path.exists(ico):
                window.iconbitmap(ico)
                if window is self:
                    window.iconbitmap(default=ico)  # inherited by dialogs / message boxes
            elif os.path.exists(png):
                if not hasattr(self, "_icon_photo"):
                    self._icon_photo = tk.PhotoImage(file=png)
                window.iconphoto(window is self, self._icon_photo)
        except tk.TclError:
            pass

    def _card(self, parent, **kw) -> ctk.CTkFrame:
        kw.setdefault("corner_radius", 16)
        return ctk.CTkFrame(parent, fg_color=CARD, border_width=1, border_color=BORDER, **kw)

    def _button(self, parent, text, command, kind="primary", **kw) -> ctk.CTkButton:
        styles = {
            "primary": dict(fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="white"),
            "secondary": dict(fg_color=CARD2, hover_color=HOVER, text_color=TEXT),
            "danger": dict(fg_color=RED, hover_color=RED_HOVER, text_color="white"),
            "ghost": dict(fg_color="transparent", hover_color=HOVER, text_color=ACCENT),
        }
        kw.setdefault("height", 34)
        return ctk.CTkButton(parent, text=text, command=command, corner_radius=10, font=self.f_bold,
                             **styles[kind], **kw)

    def _label(self, parent, text="", font=None, color=TEXT, **kw) -> ctk.CTkLabel:
        return ctk.CTkLabel(parent, text=text, font=font or self.f_body, text_color=color, **kw)

    def notify(self, message: str, kind: str = "info"):
        """Non-blocking feedback in the status bar (replaces most popups)."""
        color = {"info": TEXT, "ok": GREEN, "warn": AMBER, "error": RED}[kind]
        self._status_msg.configure(text=message, text_color=color)
        if self._status_reset:
            self.after_cancel(self._status_reset)
        self._status_reset = self.after(5000, lambda: self._status_msg.configure(text="", text_color=MUTED))

    # ── Layout ────────────────────────────────────────────────────────
    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_header()

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid(row=1, column=0, sticky="nsew", padx=18, pady=(4, 0))

        self.side = ctk.CTkFrame(self.body, fg_color="transparent")
        self._build_connect_card(self.side)
        self._build_stats(self.side)
        self._build_quick_toggles(self.side)

        self.tabs = ctk.CTkTabview(self.body, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER,
                                   segmented_button_selected_color=ACCENT,
                                   segmented_button_selected_hover_color=ACCENT_HOVER,
                                   segmented_button_unselected_color=CARD2,
                                   segmented_button_unselected_hover_color=HOVER,
                                   segmented_button_fg_color=CARD2, text_color=TEXT,
                                   segmented_button_font=self.f_bold, anchor="w")
        for name in ("Source", "Quality", "Internet", "Security", "Devices"):
            self.tabs.add(name)
        self._build_source_tab(self.tabs.tab("Source"))
        self._build_quality_tab(self.tabs.tab("Quality"))
        self._build_internet_tab(self.tabs.tab("Internet"))
        self._build_security_tab(self.tabs.tab("Security"))
        self._build_devices_tab(self.tabs.tab("Devices"))

        self._build_statusbar()

    def _scaling(self) -> float:
        try:
            return float(self._get_window_scaling())
        except Exception:
            return 1.0

    def _on_resize(self, event):
        if event.widget is self:
            self._apply_layout()

    def _apply_layout(self):
        width = self.winfo_width() / self._scaling()
        mode = "wide" if width >= BREAKPOINT else "narrow"
        if mode == self._layout:
            return
        self._layout = mode
        for w in (self.side, self.tabs, self.connect_card, self.stats_frame, self.toggles_card):
            w.grid_forget()
        b, s = self.body, self.side
        if mode == "wide":
            self._subtitle.grid()
            b.grid_columnconfigure(0, weight=0, minsize=330)
            b.grid_columnconfigure(1, weight=1)
            b.grid_rowconfigure(0, weight=1)
            b.grid_rowconfigure(1, weight=0)
            s.grid(row=0, column=0, sticky="nsew", padx=(0, 14), pady=(0, 6))
            self.tabs.grid(row=0, column=1, sticky="nsew", pady=(0, 6))
            s.grid_columnconfigure(0, weight=1)
            s.grid_columnconfigure(1, weight=0)
            self.connect_card.grid(row=0, column=0, sticky="ew")
            self.stats_frame.grid(row=1, column=0, sticky="ew", pady=12)
            self.toggles_card.grid(row=2, column=0, sticky="ew")
        else:
            self._subtitle.grid_remove()
            b.grid_columnconfigure(0, weight=1, minsize=0)
            b.grid_columnconfigure(1, weight=0)
            b.grid_rowconfigure(0, weight=0)
            b.grid_rowconfigure(1, weight=1)
            s.grid(row=0, column=0, sticky="ew", pady=(0, 12))
            self.tabs.grid(row=1, column=0, sticky="nsew", pady=(0, 6))
            s.grid_columnconfigure(0, weight=0)
            s.grid_columnconfigure(1, weight=1)
            self.connect_card.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 12))
            self.stats_frame.grid(row=0, column=1, sticky="new", pady=(0, 12))
            self.toggles_card.grid(row=1, column=1, sticky="nsew")

    # ── Header ────────────────────────────────────────────────────────
    def _build_header(self):
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 10))
        hdr.grid_columnconfigure(1, weight=1)

        icon = os.path.join(self.c.settings.static_dir, "icon.png")
        if os.path.exists(icon):
            img = ctk.CTkImage(Image.open(icon), size=(38, 38))
            ctk.CTkLabel(hdr, image=img, text="").grid(row=0, column=0, rowspan=2, padx=(0, 12))
        self._label(hdr, "RetCore OnLAN", self.f_title).grid(row=0, column=1, sticky="sw")
        self._subtitle = self._label(hdr, "Share your screen, files, clipboard and chat with any device on your network",
                                     self.f_small, MUTED)
        self._subtitle.grid(row=1, column=1, sticky="nw")

        right = ctk.CTkFrame(hdr, fg_color="transparent")
        right.grid(row=0, column=2, rowspan=2, sticky="e")
        self._pill = ctk.CTkLabel(right, text="", font=self.f_bold, corner_radius=14, height=30, fg_color=CARD2)
        self._pill.pack(side="left", padx=(0, 10))
        self._pause_btn = self._button(right, "Pause", self._toggle_stream, "secondary", width=96)
        self._pause_btn.pack(side="left", padx=(0, 8))
        files_btn = self._button(right, "📂 Files", self._open_uploads, "secondary", width=96)
        files_btn.pack(side="left", padx=(0, 8))
        _Tooltip(files_btn, f"Open the shared files folder\n{self.c.settings.upload_dir}")
        self._button(right, "Open in browser", lambda: webbrowser.open(self.server_url), width=140).pack(side="left")
        self._init_updates(right, files_btn, hdr)  # "⬆ Update" button + new-version banner

    # ── Sidebar: connect card, stats, quick toggles ───────────────────
    def _build_connect_card(self, parent):
        card = self.connect_card = self._card(parent)
        self._label(card, "Connect a device", self.f_h2).pack(anchor="w", padx=18, pady=(16, 2))
        self._label(card, "Scan the QR code or open the link on any phone,\ntablet or PC on the same Wi-Fi.",
                    self.f_small, MUTED, justify="left").pack(anchor="w", padx=18)
        self._qr = ctk.CTkLabel(card, text="", cursor="hand2")
        self._qr.pack(pady=(10, 6))
        self._qr.bind("<Button-1>", lambda _e: self._copy_url())
        self._url_label = ctk.CTkLabel(card, text="", font=self.f_mono, text_color=ACCENT, cursor="hand2",
                                       wraplength=280)
        self._url_label.pack(padx=18)
        self._url_label.bind("<Button-1>", lambda _e: self._copy_url())
        self._mode_chip = self._label(card, "", self.f_small, MUTED)
        self._mode_chip.pack(pady=(2, 10))
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(0, 16))
        row.grid_columnconfigure((0, 1), weight=1)
        self._button(row, "Copy link", self._copy_url).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._button(row, "Show QR big", self._show_big_qr, "secondary").grid(row=0, column=1, sticky="ew")
        self._refresh_url()

    def _build_stats(self, parent):
        f = self.stats_frame = ctk.CTkFrame(parent, fg_color="transparent")
        f.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="stat")
        self._stat_viewers = self._stat(f, 0, "Viewers", ACCENT)
        self._stat_fps = self._stat(f, 1, "FPS", GREEN)
        self._stat_quality = self._stat(f, 2, "Quality", AMBER)
        self._stat_source = self._stat(f, 3, "Sharing", TEXT, small=True)

    def _stat(self, parent, col, caption, color, small=False) -> ctk.CTkLabel:
        tile = self._card(parent, corner_radius=12)
        tile.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 4, 0))
        value = ctk.CTkLabel(tile, text="—", font=self.f_small_bold if small else self.f_stat, text_color=color,
                             height=30)
        value.pack(pady=(8, 0), padx=4)
        self._label(tile, caption, self.f_small, MUTED, height=18).pack(pady=(0, 8))
        return value

    def _build_quick_toggles(self, parent):
        card = self.toggles_card = self._card(parent)
        self._audio_var = ctk.BooleanVar(value=self.c.audio.enabled)
        self._audio_switch = ctk.CTkSwitch(card, text="System audio", variable=self._audio_var, font=self.f_bold,
                                           progress_color=ACCENT, text_color=TEXT, command=self._toggle_audio)
        self._audio_switch.pack(anchor="w", padx=18, pady=(16, 0))
        self._audio_hint = self._label(card, "", self.f_small, MUTED)
        self._audio_hint.pack(anchor="w", padx=62)

        self._rc_var = ctk.BooleanVar(value=self.c.remote.enabled)
        ctk.CTkSwitch(card, text="Allow remote control", variable=self._rc_var, font=self.f_bold,
                      progress_color=RED, text_color=TEXT, command=self._toggle_remote).pack(anchor="w", padx=18, pady=(12, 0))
        self._label(card, "Viewers can move your mouse and type", self.f_small, MUTED).pack(anchor="w", padx=62, pady=(0, 16))

    def _open_uploads(self):
        """Show the shared-files folder (what devices upload) in Explorer / Files / Finder."""
        try:
            open_folder(self.c.settings.upload_dir)
        except OSError as exc:
            self.notify(f"Couldn't open the folder: {exc}", "error")

    # ── Tab: Source ───────────────────────────────────────────────────
    def _build_source_tab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 8))
        top.grid_columnconfigure(0, weight=1)
        self._label(top, "What do you want to share?", self.f_h2).grid(row=0, column=0, sticky="w")
        self._button(top, "Select region…", self._pick_region, "secondary", width=130).grid(row=0, column=1, padx=6)
        self._button(top, "Refresh", self._refresh_sources, "secondary", width=90).grid(row=0, column=2)
        self._source_list = ctk.CTkScrollableFrame(tab, fg_color=CARD)
        self._source_list.grid(row=1, column=0, sticky="nsew")
        self._source_list.grid_columnconfigure(0, weight=1)
        self._source_now = self._label(tab, "", self.f_small, MUTED)
        self._source_now.grid(row=2, column=0, sticky="w", padx=8, pady=(6, 2))

    def _refresh_sources(self):
        for w in self._source_list.winfo_children():
            w.destroy()
        self._sources, self._source_buttons = [], []
        row = 0

        def section(title):
            nonlocal row
            self._label(self._source_list, title.upper(), self.f_small, MUTED).grid(
                row=row, column=0, sticky="w", padx=4, pady=(10 if row else 0, 4))
            row += 1

        def item(text, data):
            nonlocal row
            btn = ctk.CTkButton(self._source_list, text=text, anchor="w", height=40, corner_radius=10, bg_color=CARD,
                                font=self.f_body, fg_color=CARD2, hover_color=HOVER, text_color=TEXT,
                                command=lambda d=data: self._select_source(d))
            btn.grid(row=row, column=0, sticky="ew", pady=3, padx=2)
            self._sources.append(data)
            self._source_buttons.append(btn)
            row += 1

        section("Screens")
        for m in list_monitors():
            item(f"🖥   Monitor {m['index']}      {m['width']} × {m['height']}",
                 {"type": "monitor", "index": m["index"], "name": f"Monitor {m['index']}"})
        windows = [w for w in list_windows() if "RetCore OnLAN — Control Panel" not in w["title"]]
        section(f"App windows ({len(windows)})")
        for w in windows:
            title = w["title"] if len(w["title"]) <= 64 else w["title"][:61] + "…"
            item(f"🗔   {title}      {w['width']} × {w['height']}",
                 {"type": "window", "hwnd": w["hwnd"], "title": w["title"], "name": title})
        if not windows:
            self._label(self._source_list, "No app windows found.", self.f_small, MUTED).grid(row=row, column=0, sticky="w", padx=4)
        self._highlight_source()

    def _select_source(self, data):
        cap = self.c.capture
        if data["type"] == "monitor":
            cap.set_source_monitor(data["index"])
        else:
            cap.set_source_window(data["hwnd"], data["title"])
        self._highlight_source()
        self.notify(f"Now sharing: {data['name']}", "ok")

    def _highlight_source(self):
        src = self.c.capture.get_source_info()
        for data, btn in zip(self._sources, self._source_buttons):
            active = (data["type"] == src["type"] == "monitor" and data["index"] == src["monitor_index"]) or \
                     (data["type"] == src["type"] == "window" and data.get("hwnd") == src["window_hwnd"])
            btn.configure(fg_color=ACCENT if active else CARD2, text_color="white" if active else TEXT,
                          hover_color=ACCENT_HOVER if active else HOVER)
        self._source_now.configure(text=f"Currently sharing: {self._source_name()}")

    def _source_name(self) -> str:
        src = self.c.capture.get_source_info()
        if src["type"] == "window":
            t = src["window_title"] or "Window"
            return t if len(t) <= 40 else t[:37] + "…"
        if src["type"] == "region" and src["region"]:
            r = src["region"]
            return f"Region {r['width']}×{r['height']}"
        return f"Monitor {src['monitor_index']}"

    def _pick_region(self):
        self.withdraw()
        self.after(250, lambda: RegionSelector(self._on_region_picked))

    def _on_region_picked(self, x, y, w, h):
        self.deiconify()
        if w > 20 and h > 20:
            self.c.capture.set_source_region(x, y, w, h)
            self._highlight_source()
            self.notify(f"Now sharing a {w}×{h} region", "ok")

    # ── Tab: Quality ──────────────────────────────────────────────────
    def _build_quality_tab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        box = ctk.CTkScrollableFrame(tab, fg_color=CARD)
        box.grid(row=0, column=0, sticky="nsew")
        tab.grid_rowconfigure(0, weight=1)
        box.grid_columnconfigure(0, weight=1)

        self._label(box, "Preset", self.f_h2).grid(row=0, column=0, sticky="w", padx=6, pady=(4, 6))
        self._preset = ctk.CTkSegmentedButton(box, values=list(PRESETS) + ["Custom"], command=self._apply_preset,
                                              font=self.f_bold, selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
                                              unselected_color=CARD2, unselected_hover_color=HOVER, fg_color=CARD2,
                                              text_color=TEXT, height=36)
        self._preset.grid(row=1, column=0, sticky="ew", padx=6)
        self._preset_desc = self._label(box, "", self.f_small, MUTED)
        self._preset_desc.grid(row=2, column=0, sticky="w", padx=8, pady=(6, 14))

        cap = self.c.capture
        self._fps_val = self._slider_row(box, 3, "Frame rate", 5, 60, 55, cap.target_fps, " fps", self._on_fps)
        self._q_val = self._slider_row(box, 5, "Image quality", 30, 100, 70, cap.jpeg_quality, "%", self._on_quality)

        self._label(box, "Resolution (width)", self.f_bold).grid(row=7, column=0, sticky="w", padx=8, pady=(14, 4))
        self._width = ctk.CTkSegmentedButton(box, values=WIDTHS, command=self._on_width, font=self.f_body,
                                             selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
                                             unselected_color=CARD2, unselected_hover_color=HOVER, fg_color=CARD2,
                                             text_color=TEXT, height=32)
        self._width.grid(row=8, column=0, sticky="ew", padx=6)
        self._width.set(str(cap.max_width) if cap.max_width else "Native")
        self._label(box, "Lower settings use less Wi-Fi and stay smooth on slow networks.", self.f_small, MUTED).grid(
            row=9, column=0, sticky="w", padx=8, pady=(14, 4))
        self._sync_preset()

    def _slider_row(self, parent, row, title, lo, hi, steps, value, unit, command):
        head = ctk.CTkFrame(parent, fg_color="transparent")
        head.grid(row=row, column=0, sticky="ew", padx=8)
        head.grid_columnconfigure(0, weight=1)
        self._label(head, title, self.f_bold).grid(row=0, column=0, sticky="w")
        val = self._label(head, f"{value}{unit}", self.f_bold, ACCENT)
        val.grid(row=0, column=1, sticky="e")
        val.unit = unit
        slider = ctk.CTkSlider(parent, from_=lo, to=hi, number_of_steps=steps, command=command,
                               progress_color=ACCENT, button_color=ACCENT, button_hover_color=ACCENT_HOVER)
        slider.set(value)
        slider.grid(row=row + 1, column=0, sticky="ew", padx=6, pady=(4, 12))
        val.slider = slider
        return val

    def _on_fps(self, v):
        self.c.capture.set_fps(int(v))
        self._fps_val.configure(text=f"{int(v)} fps")
        self._sync_preset()

    def _on_quality(self, v):
        self.c.capture.set_quality(int(v))
        self._q_val.configure(text=f"{int(v)}%")
        self._sync_preset()

    def _on_width(self, v):
        self.c.capture.set_max_width(0 if v == "Native" else int(v))
        self._sync_preset()

    def _apply_preset(self, name):
        if name not in PRESETS:
            self._preset_desc.configure(text="Your own combination of the settings below.")
            return
        fps, q, width, refine, _ = PRESETS[name]
        cap = self.c.capture
        cap.set_fps(fps), cap.set_quality(q), cap.set_max_width(width), cap.set_refine(refine)
        self._fps_val.slider.set(fps)
        self._fps_val.configure(text=f"{fps} fps")
        self._q_val.slider.set(q)
        self._q_val.configure(text=f"{q}%")
        self._width.set(str(width) if width else "Native")
        self._sync_preset()
        self.notify(f"Quality preset: {name}", "ok")

    def _sync_preset(self):
        cap = self.c.capture
        current = (cap.target_fps, cap.jpeg_quality, cap.max_width)
        name = next((n for n, p in PRESETS.items() if p[:3] == current), "Custom")
        if name in PRESETS:
            cap.set_refine(PRESETS[name][3])
        else:
            cap.set_refine(True)  # custom: keep the HD-when-still behaviour
        self._preset.set(name)
        self._preset_desc.configure(text=PRESETS[name][4] if name in PRESETS else
                                    "Your own combination of the settings below. HD when the screen is still.")

    # ── Tab: Internet (ngrok) ─────────────────────────────────────────
    def _build_internet_tab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        self._label(tab, "Share outside your Wi-Fi", self.f_h2).grid(row=0, column=0, sticky="w", padx=8, pady=(4, 2))
        self._label(tab, "Creates a secure public link with ngrok so people anywhere can connect.",
                    self.f_small, MUTED).grid(row=1, column=0, sticky="w", padx=8)

        self._label(tab, "ngrok authtoken", self.f_bold).grid(row=2, column=0, sticky="w", padx=8, pady=(16, 4))
        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.grid(row=3, column=0, sticky="ew", padx=6)
        row.grid_columnconfigure(0, weight=1)
        self._token = ctk.CTkEntry(row, show="•", height=36, corner_radius=10, font=self.f_body,
                                   placeholder_text="Paste your token here", fg_color=CARD2, border_color=BORDER,
                                   text_color=TEXT)
        self._token.grid(row=0, column=0, sticky="ew")
        saved = self.c.prefs.get("ngrok_token", "") or ""
        if saved:
            self._token.insert(0, saved)
        self._eye(row, self._token).grid(row=0, column=1, padx=(6, 0))
        self._button(tab, "Get a free token ↗", lambda: webbrowser.open(NGROK_TOKEN_URL), "ghost", width=150).grid(
            row=4, column=0, sticky="w", padx=2, pady=(4, 10))

        self._public_btn = self._button(tab, "Go public", self._toggle_public, height=40, width=180)
        self._public_btn.grid(row=5, column=0, sticky="w", padx=8)
        self._public_status = self._label(tab, "", self.f_bold)
        self._public_status.grid(row=6, column=0, sticky="w", padx=8, pady=(12, 0))
        self._pin_warning = self._label(tab, "⚠  No PIN is set — anyone with the public link can watch your screen.\n"
                                             "     Set one in the Security tab first.", self.f_small, AMBER, justify="left")
        self._pin_warning.grid(row=7, column=0, sticky="w", padx=8, pady=(10, 0))
        self._render_public()

    def _eye(self, parent, entry: ctk.CTkEntry) -> ctk.CTkButton:
        def toggle():
            hidden = entry.cget("show") == "•"
            entry.configure(show="" if hidden else "•")
            btn.configure(text="Hide" if hidden else "Show")
        btn = self._button(parent, "Show", toggle, "secondary", width=64, height=36)
        return btn

    def _toggle_public(self):
        if self._busy_public:
            return
        if self.c.tunnel.public_url:
            try:
                self.c.tunnel.stop()
                self.notify("Public link stopped — local network only", "ok")
            except Exception as exc:
                self.notify(f"Could not stop ngrok: {exc}", "error")
            self._render_public()
            return
        token = self._token.get().strip()
        if not token:
            self.notify("Paste your ngrok authtoken first", "warn")
            self._token.focus_set()
            return
        self._busy_public = True
        self._public_btn.configure(text="Connecting…", state="disabled")
        self._public_status.configure(text="Starting the tunnel…", text_color=AMBER)

        def work():
            try:
                url, err = self.c.tunnel.start(token), None
            except Exception as exc:
                url, err = None, str(exc)
            self.after(0, lambda: self._public_done(url, err, token))

        threading.Thread(target=work, daemon=True).start()

    def _public_done(self, url, err, token):
        self._busy_public = False
        self._public_btn.configure(state="normal")
        if url:
            self.c.prefs.set("ngrok_token", token)
            self.notify("You're public! Share the new link or QR code.", "ok")
        else:
            self.notify("ngrok failed — check the token and your internet connection", "error")
            messagebox.showerror("Couldn't go public", err or "Unknown error", parent=self)
        self._render_public()

    def _render_public(self):
        url = self.c.tunnel.public_url
        if url:
            self._public_btn.configure(text="Stop public link", fg_color=RED, hover_color=RED_HOVER)
            self._public_status.configure(text=f"🌍  Public at {url}", text_color=GREEN)
        elif not self._busy_public:
            self._public_btn.configure(text="Go public", fg_color=ACCENT, hover_color=ACCENT_HOVER)
            self._public_status.configure(text="Only devices on your network can connect.", text_color=MUTED)
        if self.c.pin.required:
            self._pin_warning.grid_remove()
        else:
            self._pin_warning.grid()

    # ── Tab: Security ─────────────────────────────────────────────────
    def _build_security_tab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        self._label(tab, "Access PIN", self.f_h2).grid(row=0, column=0, sticky="w", padx=8, pady=(4, 2))
        self._label(tab, "Visitors must enter this PIN before they can see or use anything.",
                    self.f_small, MUTED).grid(row=1, column=0, sticky="w", padx=8)
        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.grid(row=2, column=0, sticky="w", padx=6, pady=(14, 0))
        self._pin = ctk.CTkEntry(row, show="•", width=200, height=38, corner_radius=10, font=self.f_mono,
                                 placeholder_text="e.g. 4821", fg_color=CARD2, border_color=BORDER, text_color=TEXT)
        self._pin.grid(row=0, column=0)
        if self.c.pin.pin:
            self._pin.insert(0, self.c.pin.pin)
        self._pin.bind("<Return>", lambda _e: self._save_pin())
        self._eye(row, self._pin).grid(row=0, column=1, padx=6)
        self._button(row, "Save PIN", self._save_pin, width=110, height=38).grid(row=0, column=2, padx=(4, 6))
        self._button(row, "Remove", self._remove_pin, "secondary", width=90, height=38).grid(row=0, column=3)
        self._pin_status = self._label(tab, "", self.f_bold)
        self._pin_status.grid(row=3, column=0, sticky="w", padx=8, pady=(14, 2))
        self._label(tab, "• This PC never needs the PIN.\n• Changing the PIN signs every visitor out.\n"
                         "• After 5 wrong tries a visitor is locked out for a while.",
                    self.f_small, MUTED, justify="left").grid(row=4, column=0, sticky="w", padx=8, pady=(8, 0))
        self._render_pin()

    def _save_pin(self):
        pin = self._pin.get().strip()
        if pin and len(pin) < 4:
            self.notify("Use at least 4 characters for the PIN", "warn")
            return
        self.c.pin.set_pin(pin)
        self._render_pin()
        self._render_public()
        self.notify("PIN saved — visitors must unlock with it" if pin else "PIN removed — open access", "ok")

    def _remove_pin(self):
        self._pin.delete(0, "end")
        self._save_pin()

    def _render_pin(self):
        if self.c.pin.required:
            self._pin_status.configure(text="🔒  Protected — visitors need the PIN", text_color=GREEN)
        else:
            self._pin_status.configure(text="🔓  Open — anyone on your network can connect", text_color=AMBER)

    # ── Tab: Devices ──────────────────────────────────────────────────
    def _build_devices_tab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)
        self._devices_title = self._label(tab, "Devices", self.f_h2)
        self._devices_title.grid(row=0, column=0, sticky="w", padx=8, pady=(4, 8))
        self._device_list = ctk.CTkScrollableFrame(tab, fg_color=CARD)
        self._device_list.grid(row=1, column=0, sticky="nsew")
        self._device_list.grid_columnconfigure(0, weight=1)

    def _devices_loop(self):
        devices = self.c.devices.snapshot()
        sig = tuple((d["ip"], d["is_blocked"], d["is_active"], int(d["last_seen"]) // 15) for d in devices)
        if sig != self._devices_sig:
            self._devices_sig = sig
            self._render_devices(devices)
        self.after(3000, self._devices_loop)

    def _render_devices(self, devices):
        for w in self._device_list.winfo_children():
            w.destroy()
        active = sum(1 for d in devices if d["is_active"])
        self._devices_title.configure(text=f"Devices  ·  {active} active, {len(devices)} total")
        if not devices:
            self._label(self._device_list, "No devices yet — share the link or QR code to get started.",
                        self.f_body, MUTED).grid(row=0, column=0, pady=30)
            return
        local_ip = get_local_ip()
        for i, d in enumerate(devices):
            row = ctk.CTkFrame(self._device_list, fg_color=CARD2, bg_color=CARD, corner_radius=12)
            row.grid(row=i, column=0, sticky="ew", pady=4, padx=2)
            row.grid_columnconfigure(1, weight=1)
            is_host = d["ip"] in LOOPBACK or d["ip"] == local_ip
            self._label(row, d["device_type"].split()[0], ctk.CTkFont(FAMILY, 22)).grid(row=0, column=0, rowspan=2, padx=(14, 10), pady=10)
            name = "This PC" if is_host else d["ip"]
            self._label(row, name, self.f_mono).grid(row=0, column=1, sticky="sw", pady=(10, 0))
            ua = d["user_agent"] or "Unknown browser"
            self._label(row, ua if len(ua) <= 60 else ua[:57] + "…", self.f_small, MUTED).grid(row=1, column=1, sticky="nw", pady=(0, 10))
            if d["is_blocked"]:
                state, color = "⛔  Blocked", RED
            elif d["is_active"]:
                state, color = "●  Active", GREEN
            else:
                state, color = f"○  Seen {_ago(d['last_seen'])}", MUTED
            self._label(row, state, self.f_small, color).grid(row=0, column=2, rowspan=2, padx=10)
            if not is_host:
                if d["is_blocked"]:
                    btn = self._button(row, "Unblock", lambda ip=d["ip"]: self._set_blocked(ip, False), "secondary", width=90)
                else:
                    btn = self._button(row, "Block", lambda ip=d["ip"]: self._set_blocked(ip, True), "danger", width=90)
                btn.grid(row=0, column=3, rowspan=2, padx=(0, 12))

    def _set_blocked(self, ip, block):
        if block and not messagebox.askyesno("Block device?", f"Block {ip}?\nIt will be denied access immediately.",
                                             parent=self):
            return
        self.c.devices.set_blocked(ip, block)
        self._devices_sig = None
        self._render_devices(self.c.devices.snapshot())
        self.notify(f"{ip} {'blocked' if block else 'unblocked'}", "ok")

    # ── Status bar ────────────────────────────────────────────────────
    def _build_statusbar(self):
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=2, column=0, sticky="ew", padx=20, pady=(4, 10))
        bar.grid_columnconfigure(0, weight=1)
        self._status_msg = self._label(bar, "", self.f_small, MUTED)
        self._status_msg.grid(row=0, column=0, sticky="w")
        self._label(bar, "Theme", self.f_small, MUTED).grid(row=0, column=1, padx=(0, 6))
        theme = ctk.CTkOptionMenu(bar, values=["Dark", "Light", "System"], width=96, height=26, font=self.f_small,
                                  fg_color=CARD2, button_color=CARD2, button_hover_color=HOVER, text_color=TEXT,
                                  command=self._set_theme)
        theme.set(self.c.prefs.get("appearance", "Dark"))
        theme.grid(row=0, column=2)
        self._label(bar, f"   {self._version_text()}", self.f_small, MUTED).grid(row=0, column=3)
        check = self._label(bar, "  ·  Check for updates", self.f_small, ACCENT)
        check.configure(cursor="hand2")
        check.bind("<Button-1>", lambda _e: self._check_updates(manual=True))
        check.grid(row=0, column=4)

    def _set_theme(self, mode):
        ctk.set_appearance_mode(mode)
        self.c.prefs.set("appearance", mode)

    # ── Actions ───────────────────────────────────────────────────────
    def _refresh_url(self):
        url = self.c.tunnel.public_url or self._local_url()
        if url == self._shown_url:
            return
        self.server_url = self._shown_url = url
        qr = make_qr_image(url, size=160).convert("RGB")
        self._qr_img = ctk.CTkImage(qr, size=(160, 160))
        self._qr.configure(image=self._qr_img)
        self._url_label.configure(text=url)
        self._mode_chip.configure(text="🌍  Public internet link" if self.c.tunnel.public_url
                                  else "📶  Local network (same Wi-Fi)")

    def _copy_url(self):
        self.clipboard_clear()
        self.clipboard_append(self.server_url)
        self.notify("Link copied to clipboard ✓", "ok")

    def _show_big_qr(self):
        win = ctk.CTkToplevel(self, fg_color=CARD)
        self._set_icon(win)
        win.title("Scan to connect")
        win.transient(self)
        img = ctk.CTkImage(make_qr_image(self.server_url, size=420).convert("RGB"), size=(420, 420))
        ctk.CTkLabel(win, image=img, text="").pack(padx=30, pady=(30, 10))
        self._label(win, self.server_url, self.f_mono, ACCENT).pack(pady=(0, 6))
        self._button(win, "Close", win.destroy, "secondary", width=120).pack(pady=(4, 24))
        win._img = img
        win.after(100, win.grab_set)

    def _toggle_stream(self):
        cap = self.c.capture
        if cap.is_running:
            cap.stop()
            self.notify("Stream paused — viewers see the last frame", "warn")
        else:
            cap.start()
            self.notify("Stream resumed", "ok")
        self._tick(reschedule=False)

    def _toggle_audio(self):
        self.c.audio.enabled = bool(self._audio_var.get())
        self.notify("System audio on" if self.c.audio.enabled else "System audio muted for all viewers", "ok")

    def _toggle_remote(self):
        on = bool(self._rc_var.get())
        self.c.remote.enabled = on
        self.notify("Remote control ON — viewers can control this PC" if on else "Remote control off",
                    "warn" if on else "ok")

    # ── Live refresh ──────────────────────────────────────────────────
    def _tick(self, reschedule: bool = True):
        cap, audio, viewers = self.c.capture, self.c.audio, self.c.stream_hub.count
        self._stat_viewers.configure(text=str(viewers))
        self._stat_fps.configure(text=f"{cap.actual_fps:.0f}" if cap.is_running else "—")
        self._stat_quality.configure(text=f"{cap.jpeg_quality}%")
        name = self._source_name()
        self._stat_source.configure(text=name if len(name) <= 12 else name[:11] + "…")

        if not cap.is_running:
            self._pill.configure(text="  ⏸  Paused  ", text_color=AMBER)
            self._pause_btn.configure(text="Resume", fg_color=GREEN, hover_color="#059669", text_color="white")
        else:
            if viewers:
                self._pill.configure(text=f"  ●  Live · {viewers} watching  ", text_color=GREEN)
            else:
                self._pill.configure(text="  ●  Ready — waiting for viewers  ", text_color=MUTED)
            self._pause_btn.configure(text="Pause", fg_color=CARD2, hover_color=HOVER, text_color=TEXT)

        if audio.available:
            self._audio_switch.configure(state="normal")
            self._audio_hint.configure(text=audio.device_name[:40])
        else:
            self._audio_switch.configure(state="disabled")
            self._audio_hint.configure(text="Starting…" if audio.is_running else "No audio device found")

        self._refresh_url()  # picks up ngrok started/stopped from anywhere
        if reschedule:
            self._render_public()
            self.after(700, self._tick)

    def _on_close(self):
        viewers = self.c.stream_hub.count
        if viewers and not messagebox.askyesno(
                "Quit RetCore OnLAN?", f"{viewers} device(s) are watching right now.\nStop sharing and quit?", parent=self):
            return
        self.c.capture.stop()
        self.c.audio.stop()
        self.c.airplay.stop()
        self.c.clipboard.flush()  # os._exit skips atexit: save the last second of clipboard edits now
        self.destroy()
        os._exit(0)
