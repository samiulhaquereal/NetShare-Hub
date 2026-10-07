import os
import sys
import subprocess
import threading

# ── Global Subprocess Window Hiding on Windows ─────────────────────
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0

if sys.platform == "win32":
    _original_Popen = subprocess.Popen
    class SilentPopen(_original_Popen):
        def __init__(self, *args, **kwargs):
            if "creationflags" not in kwargs:
                kwargs["creationflags"] = CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)
    subprocess.Popen = SilentPopen

import time
import requests
import webbrowser
import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox
import socket

flask_thread = None
ngrok_process = None
server_running = False

# 🔥 Paths
if getattr(sys, 'frozen', False):
    base_dir = os.path.dirname(sys.executable)
    temp_dir = sys._MEIPASS
else:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    temp_dir = base_dir

ngrok_path = os.path.join(temp_dir, "ngrok.exe")
TOKEN_FILE = os.path.join(base_dir, "ngrok_token.txt")

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # Google DNS
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"

public_url = ""
local_ip = get_local_ip()
local_url = f"http://{local_ip}:5000"

# 🔐 Token
def get_token():
    if os.path.exists(TOKEN_FILE):
        return open(TOKEN_FILE).read().strip()
    return ""

def save_token(token):
    with open(TOKEN_FILE, "w") as f:
        f.write(token)

# 🚀 Start Server
def start_server():
    global flask_thread, server_running

    if server_running:
        return

    token = token_entry.get().strip()

    # if not token:
    #     messagebox.showerror("Error", "Enter ngrok token")
    #     return

    save_token(token)

    status_label.config(text="🟡 Starting...", fg="orange")

    # disable start button
    start_btn.config(state="disabled")
    stop_btn.config(state="normal")

    # Start Flask
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # 🏠 Show local immediately
    local_label.config(text=local_url)

    # 🌍 If token exists → start ngrok
    if token:
        save_token(token)

        subprocess.run(
            [ngrok_path, "config", "add-authtoken", token],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW
        )

        threading.Thread(target=start_ngrok, daemon=True).start()

    else:
        # 🏠 Local only mode
        status_label.config(text="🟢 Running (Local Only)", fg="green")
        public_label.config(text="Not enabled")
        server_running = True
        webbrowser.open(local_url)

# Flask (launch the unified server)
def run_flask():
    from server import app
    app.run(host="0.0.0.0", port=5000)

# Ngrok
def start_ngrok():
    global public_url, ngrok_process, server_running

    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    ngrok_process = subprocess.Popen(
        [ngrok_path, "http", "5000"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        startupinfo=startupinfo,
        creationflags=CREATE_NO_WINDOW
    )

    for _ in range(10):
        try:
            res = requests.get("http://127.0.0.1:4040/api/tunnels")
            data = res.json()

            for t in data["tunnels"]:
                if t["proto"] == "https":
                    public_url = t["public_url"]

                    public_label.config(text=public_url)
                    local_label.config(text=local_url)

                    status_label.config(text="🟢 Server Running", fg="green")
                    server_running = True
                    webbrowser.open(public_url)
                    return
        except:
            pass

        time.sleep(1)

    status_label.config(text="🔴 Server Off", fg="red")
    messagebox.showerror("Error", "Could not get ngrok URL")

def stop_server():
    global ngrok_process, server_running

    try:
        if ngrok_process:
            ngrok_process.terminate()
            ngrok_process = None
    except:
        pass

    # kill flask (simple way)
    subprocess.call("taskkill /f /im python.exe", shell=True)

    server_running = False

    status_label.config(text="🔴 Server Off", fg="red")

    start_btn.config(state="normal")
    stop_btn.config(state="disabled")

# 📋 Copy functions
def copy_public():
    if public_url:
        root.clipboard_clear()
        root.clipboard_append(public_url)

def copy_local():
    root.clipboard_clear()
    root.clipboard_append(local_url)

# 🎨 UI
root = tk.Tk()
root.title("NetShare Hub")
root.geometry("420x310")

icon_path = os.path.join(base_dir, "static", "icon.png")
if os.path.exists(icon_path):
    try:
        img = tk.PhotoImage(file=icon_path)
        root.iconphoto(True, img)
    except Exception:
        pass

tk.Label(root, text="🌐 NetShare Hub — Screen + Files + Clipboard",
         font=("Segoe UI", 11, "bold"), fg="#7C3AED").pack(pady=(10,2))
tk.Label(root, text="Enter ngrok token (optional) for remote access:",
         font=("Segoe UI", 9), fg="gray").pack()

token_entry = tk.Entry(root, width=45, show="*")
token_entry.pack()
token_entry.insert(0, get_token())

btn_frame = tk.Frame(root)
btn_frame.pack(pady=10)

start_btn = tk.Button(btn_frame, text="🚀 Start Server", command=start_server)
start_btn.pack(side="left", padx=5)

stop_btn = tk.Button(btn_frame, text="🛑 Stop Server", command=stop_server, state="disabled")
stop_btn.pack(side="left", padx=5)

# 🌐 Public URL
tk.Label(root, text="🌐 Public URL:").pack()
public_label = tk.Label(root, text="Not available", fg="blue", wraplength=380)
public_label.pack()

tk.Button(root, text="📋 Copy Public URL", command=copy_public).pack(pady=5)

# 🏠 Local URL
tk.Label(root, text="🏠 Local URL:").pack()
local_label = tk.Label(root, text="Not available", fg="blue")
local_label.pack()

tk.Button(root, text="📋 Copy Local URL", command=copy_local).pack(pady=5)

dev_label = tk.Label(
    root,
    text="Developer: B.M.Samiul Haque Real",
    fg="gray"
)
dev_label.place(relx=0.0, rely=1.0, anchor="sw", x=10, y=-10)

# 🔴 Status (bottom-right)
status_label = tk.Label(root, text="🔴 Server Off", fg="red")
status_label.place(relx=1.0, rely=1.0, anchor="se", x=-10, y=-10)

root.mainloop()