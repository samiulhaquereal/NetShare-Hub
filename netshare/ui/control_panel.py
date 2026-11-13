"""Host control panel (Tkinter). Pure presentation: every action delegates to a
service on the container."""

from __future__ import annotations

import os
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from PIL import ImageTk

from ..container import Container
from ..infrastructure.capture import list_monitors, list_windows
from ..infrastructure.network import get_local_ip, make_qr_image
from .region_selector import RegionSelector

C_BG, C_CARD, C_CARD2 = "#0F0F1A", "#1A1A2E", "#20203A"
C_ACCENT, C_ACCENT2 = "#7C3AED", "#A855F7"
C_TEXT, C_MUTED = "#E2E8F0", "#64748B"
C_GREEN, C_AMBER, C_RED, C_SEL = "#10B981", "#F59E0B", "#EF4444", "#2D2D55"
C_INPUT = "#1E1E38"
WIDTH, HEIGHT = 640, 920


def _button(parent, text, command, bg=C_CARD2, fg=C_TEXT, active=C_SEL, size=9, bold=True, padx=12, pady=5):
    return tk.Button(parent, text=text, command=command, font=("Segoe UI", size, "bold" if bold else "normal"),
                     bg=bg, fg=fg, activebackground=active, activeforeground="white",
                     bd=0, padx=padx, pady=pady, cursor="hand2")


class ControlPanel(tk.Tk):
    def __init__(self, container: Container):
        super().__init__()
        self.c = container
        self.title("NetShare Hub — Control Panel")
        self.configure(bg=C_BG)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        icon = os.path.join(container.settings.static_dir, "icon.png")
        if os.path.exists(icon):
            try:
                self.iconphoto(True, tk.PhotoImage(file=icon))
            except tk.TclError:
                pass

        self.update_idletasks()
        sx, sy = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{WIDTH}x{HEIGHT}+{(sx - WIDTH) // 2}+{(sy - HEIGHT) // 2}")

        self.server_url = self._local_url()
        self._qr_photo = None
        self._src_data: list[dict] = []
        self._dev_data: list[dict] = []

        self._build_ui()
        self._refresh_sources()
        self._refresh_devices()
        self._stats_loop()

    def _local_url(self) -> str:
        return f"http://{get_local_ip()}:{self.c.settings.http_port}"

    # ── Layout ────────────────────────────────────────────────────────
    def _build_ui(self):
        canvas = tk.Canvas(self, bg=C_BG, highlightthickness=0, borderwidth=0)
        scrollbar = tk.Scrollbar(self, orient="vertical", command=canvas.yview, bg=C_CARD, troughcolor=C_BG)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)

        self.container = tk.Frame(canvas, bg=C_BG)
        canvas.create_window((0, 0), window=self.container, anchor="nw", width=WIDTH - 80)
        self.container.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        canvas.bind_all("<Button-4>", lambda _e: canvas.yview_scroll(-1, "units"))  # X11 wheel up
        canvas.bind_all("<Button-5>", lambda _e: canvas.yview_scroll(1, "units"))   # X11 wheel down

        hdr = tk.Frame(self.container, bg=C_BG)
        hdr.pack(fill="x", padx=24, pady=(22, 0))
        tk.Label(hdr, text="NetShare Hub", font=("Segoe UI", 22, "bold"), bg=C_BG, fg=C_TEXT).pack(side="left")
        self._badge = tk.Label(hdr, text="● LIVE", font=("Segoe UI", 10, "bold"), bg=C_GREEN, fg="white",
                               padx=10, pady=4)
        self._badge.pack(side="right")
        tk.Label(self.container, text="Screen Sharing  •  File Transfer  •  Clipboard Sync",
                 font=("Segoe UI", 10), bg=C_BG, fg=C_MUTED).pack(anchor="w", padx=24, pady=(3, 14))

        self._build_qr_card()
        self._build_stats_row()
        self._build_source_card()
        self._build_settings_card()
        self._build_public_card()
        self._build_security_card()
        self._build_controls()
        tk.Label(self.container, text="All devices must be on the same Wi-Fi network",
                 font=("Segoe UI", 9), bg=C_BG, fg=C_MUTED).pack(pady=(2, 24))

    def _card(self) -> tk.Frame:
        outer = tk.Frame(self.container, bg=C_CARD)
        outer.pack(fill="x", padx=24, pady=6)
        inner = tk.Frame(outer, bg=C_CARD, padx=18, pady=14)
        inner.pack(fill="x")
        return inner

    @staticmethod
    def _title(parent, text, subtitle=None):
        tk.Label(parent, text=text, font=("Segoe UI", 12, "bold"), bg=C_CARD, fg=C_TEXT).pack(
            anchor="w", pady=(0, 4 if subtitle else 10))
        if subtitle:
            tk.Label(parent, text=subtitle, font=("Segoe UI", 9), bg=C_CARD, fg=C_MUTED).pack(anchor="w", pady=(0, 10))

    def _build_qr_card(self):
        inner = self._card()
        self._qr_label = tk.Label(inner, bg=C_CARD, cursor="hand2")
        self._qr_label.pack(side="left", padx=(0, 18))
        self._qr_label.bind("<Button-1>", lambda _e: self._copy_url())

        right = tk.Frame(inner, bg=C_CARD)
        right.pack(side="left", fill="both", expand=True)
        tk.Label(right, text="Scan to Connect", font=("Segoe UI", 13, "bold"), bg=C_CARD, fg=C_TEXT).pack(anchor="w")
        tk.Label(right, text="or open this URL in any browser:", font=("Segoe UI", 9),
                 bg=C_CARD, fg=C_MUTED).pack(anchor="w", pady=(2, 8))
        url_f = tk.Frame(right, bg=C_INPUT, padx=10, pady=8)
        url_f.pack(fill="x")
        self._url_label = tk.Label(url_f, font=("Consolas", 13, "bold"), bg=C_INPUT, fg=C_ACCENT2, cursor="hand2")
        self._url_label.pack(anchor="w")
        self._url_label.bind("<Button-1>", lambda _e: self._copy_url())
        _button(right, "Copy URL", self._copy_url, bg=C_ACCENT, fg="white", active=C_ACCENT2,
                padx=14, pady=6).pack(anchor="w", pady=(8, 0))
        self._refresh_qr()

    def _build_stats_row(self):
        row = tk.Frame(self.container, bg=C_BG)
        row.pack(fill="x", padx=24, pady=6)
        self._lbl_fps = self._stat_chip(row, "FPS", "—", C_GREEN)
        self._lbl_clients = self._stat_chip(row, "Clients", "0", C_ACCENT2)
        self._lbl_quality = self._stat_chip(row, "Quality", f"{self.c.capture.jpeg_quality}%", C_AMBER)
        self._lbl_source = self._stat_chip(row, "Source", "Monitor 1", C_TEXT)

    @staticmethod
    def _stat_chip(parent, label, value, color) -> tk.Label:
        f = tk.Frame(parent, bg=C_CARD, padx=12, pady=10)
        f.pack(side="left", fill="both", expand=True, padx=(0, 5))
        v = tk.Label(f, text=value, font=("Segoe UI", 17, "bold"), bg=C_CARD, fg=color)
        v.pack()
        tk.Label(f, text=label, font=("Segoe UI", 8), bg=C_CARD, fg=C_MUTED).pack()
        return v

    def _build_source_card(self):
        inner = self._card()
        hrow = tk.Frame(inner, bg=C_CARD)
        hrow.pack(fill="x", pady=(0, 8))
        tk.Label(hrow, text="Share Source", font=("Segoe UI", 12, "bold"), bg=C_CARD, fg=C_TEXT).pack(side="left")
        _button(hrow, "Refresh", self._refresh_sources, fg=C_MUTED, size=8, padx=10, pady=4).pack(side="right")

        lb_frame = tk.Frame(inner, bg=C_CARD)
        lb_frame.pack(fill="x")
        sb = tk.Scrollbar(lb_frame, orient="vertical", bg=C_CARD2, troughcolor=C_CARD)
        self._src_listbox = tk.Listbox(lb_frame, bg=C_CARD2, fg=C_TEXT, selectbackground=C_ACCENT,
                                       selectforeground="white", font=("Segoe UI", 10), bd=0,
                                       highlightthickness=0, activestyle="none", height=6, yscrollcommand=sb.set)
        sb.config(command=self._src_listbox.yview)
        self._src_listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._src_listbox.bind("<<ListboxSelect>>", self._on_source_select)

        self._src_desc = tk.Label(inner, text="", font=("Segoe UI", 9), bg=C_CARD, fg=C_MUTED, anchor="w")
        self._src_desc.pack(fill="x", pady=(6, 0))
        _button(inner, "Select Custom Region", self._pick_region, active=C_ACCENT, bold=False,
                pady=7).pack(anchor="w", pady=(8, 0))

    def _build_settings_card(self):
        inner = self._card()
        self._title(inner, "Performance")
        cap = self.c.capture
        self._fps_var = tk.IntVar(value=cap.target_fps)
        self._quality_var = tk.IntVar(value=cap.jpeg_quality)
        self._width_var = tk.StringVar(value=str(cap.max_width) if cap.max_width else "Native")
        self._slider(inner, "Target FPS", self._fps_var, 5, 60, lambda v: cap.set_fps(int(float(v))))
        self._slider(inner, "JPEG Quality", self._quality_var, 30, 100, lambda v: cap.set_quality(int(float(v))))

        res_row = tk.Frame(inner, bg=C_CARD)
        res_row.pack(fill="x", pady=(8, 0))
        tk.Label(res_row, text="Stream Width", font=("Segoe UI", 10), bg=C_CARD, fg=C_TEXT,
                 width=14, anchor="w").pack(side="left")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=C_INPUT, background=C_CARD2, foreground=C_TEXT,
                        selectbackground=C_ACCENT)
        style.configure("Horizontal.TScale", background=C_CARD, troughcolor=C_INPUT, sliderthickness=18)
        combo = ttk.Combobox(res_row, textvariable=self._width_var, values=["Native", "1920", "1280", "960", "640"],
                             state="readonly", width=10, font=("Segoe UI", 10))
        combo.pack(side="left", padx=8)
        combo.bind("<<ComboboxSelected>>", self._on_width_change)

        audio_row = tk.Frame(inner, bg=C_CARD)
        audio_row.pack(fill="x", pady=(12, 0))
        self._audio_status_lbl = tk.Label(audio_row, font=("Segoe UI", 9), bg=C_CARD, anchor="w")
        self._audio_status_lbl.pack(side="left", fill="x", expand=True)
        self._audio_btn = _button(audio_row, "Mute Audio", self._toggle_audio)
        self._audio_btn.pack(side="right")

    @staticmethod
    def _slider(parent, label, var, lo, hi, cmd):
        row = tk.Frame(parent, bg=C_CARD)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, font=("Segoe UI", 10), bg=C_CARD, fg=C_TEXT, width=14, anchor="w").pack(side="left")
        ttk.Scale(row, from_=lo, to=hi, orient="horizontal", variable=var, command=cmd).pack(
            side="left", fill="x", expand=True, padx=8)
        tk.Label(row, textvariable=var, font=("Consolas", 10, "bold"), bg=C_CARD, fg=C_ACCENT2,
                 width=4).pack(side="right")

    def _build_public_card(self):
        inner = self._card()
        self._title(inner, "Public Access (Ngrok)", "Expose your hub to the internet so others can connect remotely.")
        self._token_var = tk.StringVar(value=self.c.prefs.get("ngrok_token", "") or "")
        row = tk.Frame(inner, bg=C_CARD)
        row.pack(fill="x", pady=4)
        tk.Label(row, text="Authtoken:", font=("Segoe UI", 10), bg=C_CARD, fg=C_TEXT, width=10,
                 anchor="w").pack(side="left")
        tk.Entry(row, textvariable=self._token_var, bg=C_INPUT, fg=C_TEXT, bd=0, insertbackground="white",
                 font=("Consolas", 10), show="*", highlightthickness=1,
                 highlightbackground=C_CARD2).pack(side="left", fill="x", expand=True, padx=8)
        self._public_btn = _button(inner, "Go Public", self._on_go_public, bg=C_ACCENT, fg="white",
                                   active=C_ACCENT2, size=10, padx=18, pady=8)
        self._public_btn.pack(anchor="w", pady=(10, 0))
        self._public_status_lbl = tk.Label(inner, text="Status: Local Network Only", font=("Segoe UI", 9, "bold"),
                                           bg=C_CARD, fg=C_MUTED)
        self._public_status_lbl.pack(anchor="w", pady=(8, 0))

    def _build_security_card(self):
        inner = self._card()
        self._title(inner, "Security & Device Management", "Set Access PIN to lock stream & manage connected devices.")
        pin_row = tk.Frame(inner, bg=C_CARD)
        pin_row.pack(fill="x", pady=4)
        tk.Label(pin_row, text="Access PIN:", font=("Segoe UI", 10), bg=C_CARD, fg=C_TEXT, width=10,
                 anchor="w").pack(side="left")
        self._pin_var = tk.StringVar(value=self.c.pin.pin)
        tk.Entry(pin_row, textvariable=self._pin_var, bg=C_INPUT, fg=C_TEXT, bd=0, insertbackground="white",
                 font=("Consolas", 11, "bold"), show="*", width=12, highlightthickness=1,
                 highlightbackground=C_CARD2).pack(side="left", padx=8)
        _button(pin_row, "Save PIN", self._save_pin, bg=C_ACCENT, fg="white", active=C_ACCENT2,
                pady=4).pack(side="left", padx=4)
        _button(pin_row, "Clear", self._remove_pin, fg=C_MUTED, bold=False, padx=10, pady=4).pack(side="left")
        self._pin_status_lbl = tk.Label(inner, font=("Segoe UI", 9, "bold"), bg=C_CARD)
        self._pin_status_lbl.pack(anchor="w", pady=(6, 12))
        self._render_pin_status()

        dev_hdr = tk.Frame(inner, bg=C_CARD)
        dev_hdr.pack(fill="x", pady=(8, 4))
        tk.Label(dev_hdr, text="Connected / Recent Devices", font=("Segoe UI", 10, "bold"), bg=C_CARD,
                 fg=C_TEXT).pack(side="left")
        _button(dev_hdr, "Refresh Devices", self._refresh_devices, fg=C_MUTED, size=8, padx=8,
                pady=3).pack(side="right")

        lb_frame = tk.Frame(inner, bg=C_CARD)
        lb_frame.pack(fill="x", pady=(2, 6))
        sb = tk.Scrollbar(lb_frame, orient="vertical", bg=C_CARD2, troughcolor=C_CARD)
        self._dev_listbox = tk.Listbox(lb_frame, bg=C_CARD2, fg=C_TEXT, selectbackground=C_ACCENT,
                                       selectforeground="white", font=("Consolas", 9), bd=0,
                                       highlightthickness=0, height=4, yscrollcommand=sb.set)
        sb.config(command=self._dev_listbox.yview)
        self._dev_listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        _button(inner, "Block / Unblock Selected IP", self._toggle_block_ip, bg=C_RED, fg="white",
                active="#DC2626").pack(anchor="w", pady=(4, 0))

    def _build_controls(self):
        row = tk.Frame(self.container, bg=C_BG)
        row.pack(fill="x", padx=24, pady=8)
        self._pause_btn = _button(row, "Pause Stream", self._toggle_stream, bg=C_AMBER, fg="white",
                                  active="#D97706", size=10, padx=18, pady=10)
        self._pause_btn.pack(side="left", padx=(0, 8))
        self._rc_var = tk.BooleanVar(value=self.c.remote.enabled)
        tk.Checkbutton(row, text="Allow Remote Control", variable=self._rc_var, font=("Segoe UI", 10),
                       bg=C_BG, fg=C_TEXT, selectcolor=C_INPUT, activebackground=C_BG, activeforeground=C_TEXT,
                       command=lambda: setattr(self.c.remote, "enabled", bool(self._rc_var.get()))
                       ).pack(side="left", padx=12)
        _button(row, "Open in Browser", lambda: webbrowser.open(self.server_url), bg=C_CARD, active="#252550",
                size=10, padx=18, pady=10).pack(side="left")
        _button(row, "Exit", self._on_close, bg=C_RED, fg="white", active="#DC2626", size=10,
                padx=18, pady=10).pack(side="right")

    # ── Sources ───────────────────────────────────────────────────────
    def _add_header(self, text: str):
        self._src_listbox.insert(tk.END, text)
        self._src_listbox.itemconfig(tk.END, fg=C_MUTED, selectbackground=C_CARD2, selectforeground=C_MUTED)
        self._src_data.append({"_header": True})

    def _refresh_sources(self):
        self._src_data.clear()
        self._src_listbox.delete(0, tk.END)
        self._add_header("  -- Monitors --")
        for m in list_monitors():
            self._src_listbox.insert(tk.END, f"    Monitor {m['index']}  ({m['width']}x{m['height']})")
            self._src_data.append({"type": "monitor", "index": m["index"], "name": f"Monitor {m['index']}"})
        self._add_header("  -- Application Windows --")
        windows = list_windows()
        for w in windows:
            self._src_listbox.insert(tk.END, f"    {w['title'][:46]}  ({w['width']}x{w['height']})")
            self._src_data.append({"type": "window", "hwnd": w["hwnd"], "title": w["title"]})
        if not windows:
            self._add_header("    (no windows found)")
        self._src_listbox.selection_set(1)

    def _on_source_select(self, _e):
        sel = self._src_listbox.curselection()
        if not sel or sel[0] >= len(self._src_data):
            return
        data = self._src_data[sel[0]]
        if data.get("_header"):
            return
        if data["type"] == "monitor":
            self.c.capture.set_source_monitor(data["index"])
            self._src_desc.config(text=f"Capturing: {data['name']}")
            self._lbl_source.config(text=data["name"])
        else:
            self.c.capture.set_source_window(data["hwnd"], data["title"])
            title = data["title"][:28]
            self._src_desc.config(text=f"Capturing window: {title}")
            self._lbl_source.config(text=title[:14])

    def _pick_region(self):
        self.withdraw()
        self.after(200, lambda: RegionSelector(self._on_region_picked))

    def _on_region_picked(self, x, y, w, h):
        self.deiconify()
        if w > 20 and h > 20:
            self.c.capture.set_source_region(x, y, w, h)
            self._src_desc.config(text=f"Capturing: Region  {w}x{h} at ({x},{y})")
            self._lbl_source.config(text=f"{w}x{h}")

    # ── Public access ─────────────────────────────────────────────────
    def _on_go_public(self):
        if self.c.tunnel.public_url:
            self._stop_public()
            return
        token = self._token_var.get().strip()
        if not token:
            messagebox.showwarning("Authtoken Required", "Please enter your Ngrok authtoken first.")
            return
        self._public_btn.config(text="Starting...", state="disabled")
        self._public_status_lbl.config(text="Status: Connecting to Ngrok...", fg=C_AMBER)

        def work():
            try:
                url, err = self.c.tunnel.start(token), None
            except Exception as exc:
                url, err = None, str(exc)
            self.after(0, lambda: self._finish_go_public(url, err, token))

        threading.Thread(target=work, daemon=True).start()

    def _finish_go_public(self, url, err, token):
        self._public_btn.config(state="normal")
        if url:
            self.c.prefs.set("ngrok_token", token)
            self.server_url = url
            self._refresh_qr()
            self._public_btn.config(text="Stop Public", bg=C_RED)
            self._public_status_lbl.config(text=f"Status: PUBLIC ({url})", fg=C_GREEN)
            messagebox.showinfo("Success", f"Server is now PUBLIC!\n\nURL: {url}")
        else:
            self._public_btn.config(text="Go Public", bg=C_ACCENT)
            self._public_status_lbl.config(text="Status: Connection Failed", fg=C_RED)
            messagebox.showerror("Ngrok Error", err or "Unknown error")

    def _stop_public(self):
        try:
            self.c.tunnel.stop()
        except Exception as exc:
            messagebox.showerror("Error", f"Failed to stop Ngrok: {exc}")
            return
        self._public_btn.config(text="Go Public", bg=C_ACCENT)
        self._public_status_lbl.config(text="Status: Local Network Only", fg=C_MUTED)
        self.server_url = self._local_url()
        self._refresh_qr()
        messagebox.showinfo("Public Access Stopped", "The server is now only accessible via your local network.")

    def _refresh_qr(self):
        self._qr_photo = ImageTk.PhotoImage(make_qr_image(self.server_url, size=150))
        self._qr_label.config(image=self._qr_photo)
        self._url_label.config(text=self.server_url)

    # ── Security ──────────────────────────────────────────────────────
    def _render_pin_status(self):
        pin = self.c.pin.pin
        if pin:
            self._pin_status_lbl.config(text=f"PIN Status: PROTECTED ({'•' * len(pin)})", fg=C_GREEN)
        else:
            self._pin_status_lbl.config(text="PIN Status: Open Access (No PIN)", fg=C_AMBER)

    def _save_pin(self):
        self.c.pin.set_pin(self._pin_var.get())
        self._render_pin_status()
        if self.c.pin.required:
            messagebox.showinfo("PIN Saved", "Access PIN saved. Clients must enter this PIN to connect.")
        else:
            messagebox.showinfo("PIN Removed", "Access PIN removed. Open access enabled.")

    def _remove_pin(self):
        self._pin_var.set("")
        self._save_pin()

    def _refresh_devices(self):
        self._dev_data = self.c.devices.snapshot()
        self._dev_listbox.delete(0, tk.END)
        for d in self._dev_data:
            blocked = " [BLOCKED]" if d["is_blocked"] else ""
            status = "Active" if d["is_active"] else "Idle"
            self._dev_listbox.insert(tk.END, f"{d['device_type']} | {d['ip']:<15} | {status:<6}{blocked}")
        if not self._dev_data:
            self._dev_listbox.insert(tk.END, "  No devices recorded yet.")

    def _toggle_block_ip(self):
        sel = self._dev_listbox.curselection()
        if not sel or sel[0] >= len(self._dev_data):
            messagebox.showwarning("No Device Selected", "Please select a device from the list.")
            return
        ip = self._dev_data[sel[0]]["ip"]
        if self.c.devices.is_blocked(ip):
            self.c.devices.set_blocked(ip, False)
            messagebox.showinfo("Unblocked", f"IP {ip} has been unblocked.")
        elif messagebox.askyesno("Confirm Block",
                                 f"Are you sure you want to block IP {ip}?\nThey will be denied access."):
            self.c.devices.set_blocked(ip, True)
            messagebox.showinfo("Blocked", f"IP {ip} has been blocked.")
        self._refresh_devices()

    # ── Misc actions ──────────────────────────────────────────────────
    def _copy_url(self):
        self.clipboard_clear()
        self.clipboard_append(self.server_url)
        self._badge.config(text="Copied!", bg="#0EA5E9")
        self.after(1800, self._render_badge)

    def _render_badge(self):
        if self.c.capture.is_running:
            self._badge.config(text="● LIVE", bg=C_GREEN)
        else:
            self._badge.config(text="PAUSED", bg=C_AMBER)

    def _toggle_stream(self):
        cap = self.c.capture
        if cap.is_running:
            cap.stop()
            self._pause_btn.config(text="Resume Stream", bg=C_GREEN)
        else:
            cap.start()
            self._pause_btn.config(text="Pause Stream", bg=C_AMBER)
        self._render_badge()

    def _on_width_change(self, _e):
        val = self._width_var.get()
        self.c.capture.set_max_width(0 if val == "Native" else int(val))

    def _toggle_audio(self):
        audio = self.c.audio
        audio.enabled = not audio.enabled
        self._audio_btn.config(text="Mute Audio" if audio.enabled else "Unmute Audio",
                               bg=C_CARD2 if audio.enabled else C_AMBER)

    def _stats_loop(self):
        cap, audio = self.c.capture, self.c.audio
        self._lbl_fps.config(text=f"{cap.actual_fps:.0f}")
        self._lbl_clients.config(text=str(self.c.stream_hub.count))
        self._lbl_quality.config(text=f"{cap.jpeg_quality}%")
        # Audio device is detected asynchronously after startup.
        self._audio_status_lbl.config(text=f"Audio: {audio.device_name[:32] if audio.available else 'Not found'}",
                                      fg=C_GREEN if audio.available else C_RED)
        self.after(500, self._stats_loop)

    def _on_close(self):
        self.c.capture.stop()
        self.c.audio.stop()
        self.c.airplay.stop()
        self.destroy()
        os._exit(0)
