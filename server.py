"""
server.py — NetShare Hub Main Application
Unified Server: Flask (HTTP/API) on :5000 + WebSocket (video) on :5001
Features: Screen Streaming + File Share + Clipboard Sync
Tkinter control panel with full source selection UI.

Run:  python server.py
"""

import datetime
import io
import json
import os
import socket
import sys
import threading
import time
import zipfile
from queue import Queue, Empty
from urllib.parse import unquote
import tkinter as tk
from tkinter import ttk, messagebox

import cv2
import mss
import numpy as np
import qrcode
from PIL import Image, ImageTk
from flask import Flask, Response, jsonify, render_template, request, send_file, make_response, send_from_directory
from flask_sock import Sock
from werkzeug.utils import secure_filename
import pyautogui
import sqlite3
import base64
import hashlib
import uuid
import re
from dotenv import load_dotenv

# Load configuration from .env file
load_dotenv()


# Disable PyAutoGUI fail-safe (not recommended for general use, but needed for remote access)
# However, we'll keep it on and tell the user about the corner-of-screen safety.
pyautogui.FAILSAFE = True

from capture import ScreenCapture, AudioCapture, list_monitors, list_windows

# ── Ngrok WebSocket WSGI Middleware ──────────────────────────────────────────
class FixNgrokWebSocketMiddleware:
    """
    Ngrok free tier modifies the HTTP_CONNECTION header and drops 'Upgrade',
    which breaks standard WSGI WebSocket checks. This middleware natively restores it.
    """
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        # Only modify if client is requesting a WebSocket upgrade
        if environ.get("HTTP_UPGRADE", "").lower() == "websocket":
            current_conn = environ.get("HTTP_CONNECTION", "").lower()
            if "upgrade" not in current_conn:
                # Add Upgrade to the existing connection header if possible, or set it
                if current_conn:
                    environ["HTTP_CONNECTION"] = f"{environ['HTTP_CONNECTION']}, Upgrade"
                else:
                    environ["HTTP_CONNECTION"] = "Upgrade"
        return self.wsgi_app(environ, start_response)

# ── Ports ─────────────────────────────────────────────────────────────

HTTP_PORT = 5000

# ── File Share Setup ─────────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
CHAT_UPLOAD_FOLDER = os.path.join(BASE_DIR, "chat uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(CHAT_UPLOAD_FOLDER, exist_ok=True)

CIPHER_KEY = os.getenv("CHAT_CIPHER_KEY", "DefaultSecret123")

# ── Clipboard state ───────────────────────────────────────────────────
clipboard_data    = ""
clipboard_version = 0

# ── Global URLs for Public Access ─────────────────────────────────────
PUBLIC_HTTP_URL = None
PUBLIC_WS_URL   = None

# ── Global capture engine ─────────────────────────────────────────────
capturer       = ScreenCapture(target_fps=30, jpeg_quality=70, max_width=1280)
audio_capturer = AudioCapture()

# ── WebSocket frame-type prefixes (1-byte protocol header) ────────────
VID = b'\x01'   # video: JPEG bytes follow
AUD = b'\x02'   # audio: Int16 LE PCM bytes follow
CFG = b'\x00'   # config: UTF-8 JSON follows
CLP = b'\x03'   # clipboard: UTF-8 text follows (bidirectional)
CTL = b'\x04'   # control: UTF-8 JSON events (client -> server)

# ── Remote Control State ──────────────────────────────────────────────
remote_control_on = False

# ── Security & Device Management State ────────────────────────────────
SERVER_PIN = os.getenv("SERVER_PIN", "").strip()
_blocked_ips = set()
_connected_devices = {}
_device_lock = threading.Lock()

def _get_valid_pin_token(pin_str: str) -> str:
    if not pin_str: return ""
    return hashlib.sha256(f"{pin_str}:{CIPHER_KEY}".encode()).hexdigest()[:16]

def track_device(ip: str, user_agent: str = ""):
    with _device_lock:
        now = time.time()
        if ip not in _connected_devices:
            ua_lower = user_agent.lower()
            device_type = "📱 Mobile" if any(x in ua_lower for x in ["mobile", "android", "iphone", "ipad"]) else "💻 Desktop"
            _connected_devices[ip] = {
                "ip": ip,
                "user_agent": user_agent[:60],
                "device_type": device_type,
                "first_seen": now,
                "last_seen": now,
                "is_blocked": ip in _blocked_ips
            }
        else:
            _connected_devices[ip]["last_seen"] = now
            _connected_devices[ip]["is_blocked"] = ip in _blocked_ips

# Global lock for thumbnail mss calls (mss is not thread-safe across instances)
_thumb_lock = threading.Lock()

# ── Connected WebSocket client tracking ───────────────────────────────
# ── Connected WebSocket client tracking ───────────────────────────────
_ws_clients: dict[int, dict] = {} # Store {id: {"ws": websocket, "q": queue}}
_ws_lock = threading.Lock()

# ── Frame delivery bridge ──
def _on_frame_ready_bridge():
    """Called from CaptureThread to distribute frames to connected clients."""
    frame = capturer.get_latest_frame()
    if not frame:
        return
        
    with _ws_lock:
        for data in _ws_clients.values():
            q = data["q"]
            # If queue is full, drop the oldest frame to maintain "live" feel
            if q.full():
                try:
                    q.get_nowait()
                except Empty:
                    pass
            
            # Push newest frame
            try:
                q.put_nowait(frame)
            except Exception:
                pass



capturer.on_frame_ready = _on_frame_ready_bridge

# ── Helpers ───────────────────────────────────────────────────────────

def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def make_qr_image(url: str, size: int = 200) -> Image.Image:
    qr = qrcode.QRCode(version=1,
                       error_correction=qrcode.constants.ERROR_CORRECT_L,
                       box_size=6, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#7C3AED", back_color="white")
    return img.resize((size, size), Image.NEAREST)


def make_thumb(region: dict | None = None, monitor_index: int = 1,
               size: tuple = (256, 144)) -> bytes:
    """Grab a tiny thumbnail. Thread-safe via lock. Returns JPEG bytes."""
    if not _thumb_lock.acquire(blocking=True, timeout=0.5):
        return b""   # couldn't get lock in time — skip to avoid blocking thread
    try:
        with mss.mss() as sct:
            if region:
                raw = sct.grab(region)
            else:
                monitors = sct.monitors
                idx = monitor_index if monitor_index < len(monitors) else 1
                raw = sct.grab(monitors[idx])
            img = np.array(raw)[:, :, :3]
            h, w = img.shape[:2]
            scale = min(size[0] / w, size[1] / h)
            nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
            _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
            return buf.tobytes()
    except Exception as e:
        print(f"[Thumb] {e}")
        return b""
    finally:
        _thumb_lock.release()


# ── Chat Support Logic ────────────────────────────────────────────────
# Identity & Security Helpers
_id_to_mac = {} 

def xor_cipher(text: str, key: str) -> str:
    return "".join(chr(ord(c) ^ ord(key[i % len(key)])) for i, c in enumerate(text))

def encrypt_data(text: str) -> str:
    if not text: return ""
    xor_ed = xor_cipher(text, CIPHER_KEY)
    return base64.b64encode(xor_ed.encode()).decode()

def decrypt_data(encoded: str) -> str:
    if not encoded: return ""
    try:
        decoded = base64.b64decode(encoded.encode()).decode()
        return xor_cipher(decoded, CIPHER_KEY)
    except Exception:
        return encoded

def obfuscate_mac(mac: str) -> str:
    if not mac or mac == "group" or mac.startswith("cg_"): return mac
    return "u_" + hashlib.md5(mac.encode()).hexdigest()[:8]

def get_db():
    conn = sqlite3.connect(os.path.join(BASE_DIR, "chat.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_chat_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (mac TEXT PRIMARY KEY, username TEXT, avatar TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_mac TEXT,
        to_target TEXT, 
        msg_type TEXT, 
        content TEXT,
        filename TEXT,
        file_url TEXT,
        filetype TEXT,
        status TEXT DEFAULT 'sent',
        is_edited INTEGER DEFAULT 0,
        is_deleted INTEGER DEFAULT 0,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS custom_groups (
        id TEXT PRIMARY KEY,
        name TEXT,
        created_by TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS group_members (
        group_id TEXT,
        mac TEXT,
        PRIMARY KEY (group_id, mac)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS message_reactions (
        msg_id INTEGER,
        mac TEXT,
        emoji TEXT,
        PRIMARY KEY (msg_id, mac, emoji)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS pinned_chats (
        mac TEXT,
        target_id TEXT,
        PRIMARY KEY (mac, target_id)
    )''')
    conn.commit()
    conn.close()



def migrate_to_encryption():
    conn = get_db()
    cursor = conn.cursor()
    rows = cursor.execute("SELECT id, content FROM messages WHERE msg_type='text' AND is_deleted=0").fetchall()
    migrated_count = 0
    for row in rows:
        msg_id, content = row["id"], row["content"]
        # Basic check: if it's already base64-like and we can decrypt it to valid text, maybe skip?
        # Simpler: If it doesn't look like our base64 signature, encrypt it.
        try:
            # If decrypting it yields valid-ish text and Re-encrypting it yields the same, maybe it's already encrypted?
            # Actually, let's keep it simple: If it isn't encrypted, encrypt it. 
            # We add a prefix to know if it's encrypted.
            if not content.startswith("ENC:"):
                encrypted = "ENC:" + encrypt_data(content)
                cursor.execute("UPDATE messages SET content=? WHERE id=?", (encrypted, msg_id))
                migrated_count += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    if migrated_count > 0:
        print(f"[*] Migrated {migrated_count} messages to Encryption.")

init_chat_db()
migrate_to_encryption()

# Chat WebSocket tracking
_mac_to_ws_chat = {} # { "mac": [ws1, ws2] }
_ws_chat_lock = threading.Lock()
_id_to_mac = {}

def get_chat_db():
    return get_db()

def get_real_mac(obf_id: str) -> str:
    if not obf_id or obf_id == "group" or obf_id.startswith("cg_"): return obf_id
    if obf_id.startswith("u_"):
        return _id_to_mac.get(obf_id, obf_id)
    return obf_id

def log_mac_id(mac: str):
    obf = obfuscate_mac(mac)
    if obf.startswith("u_"):
        _id_to_mac[obf] = mac

def get_mac_address(ip: str) -> str:
    if ip in ("127.0.0.1", "localhost", "::1"):
        return "localhost-mac-id"
    try:
        import subprocess, re
        output = subprocess.check_output(f"arp -a {ip}", shell=True).decode("utf-8", errors="ignore")
        match = re.search(r'([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})', output)
        if match: return match.group(0).replace("-", ":").upper()
    except Exception: pass
    return ip


# ── Flask app ─────────────────────────────────────────────────────────

if getattr(sys, 'frozen', False):
    template_dir = os.path.join(sys._MEIPASS, "templates")
else:
    template_dir = os.path.join(BASE_DIR, "templates")

app = Flask(__name__, template_folder=template_dir)
sock = Sock(app)
app.wsgi_app = FixNgrokWebSocketMiddleware(app.wsgi_app)
# Enable virtually unlimited uploads (1 Terabyte)
app.config['MAX_CONTENT_LENGTH'] = 1000 * 1024 * 1024 * 1024


@app.after_request
def add_ngrok_skip_header(response):
    """Automatically bypass the Ngrok 'Browser Warning' page."""
    response.headers["ngrok-skip-browser-warning"] = "any"
    return response

@app.before_request
def security_middleware():
    ip = request.remote_addr
    ua = request.headers.get("User-Agent", "")
    track_device(ip, ua)

    # 1. IP Block check
    if ip in _blocked_ips and not request.path.startswith("/static"):
        if request.path.startswith("/api/") or request.headers.get("Accept") == "application/json":
            return jsonify({"error": "Access Denied: Your IP address has been blocked by host.", "blocked": True}), 403
        s_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return render_template("blocked.html", ip=ip, server_time=s_time), 403

    # 2. PIN Protection check
    if SERVER_PIN:
        allowed_prefixes = ["/api/auth/", "/api/adb/", "/info", "/favicon.ico", "/static/", "/manifest.json", "/sw.js", "/qr.png"]
        if any(request.path.startswith(p) for p in allowed_prefixes):
            return None
            
        token = request.headers.get("X-Server-PIN-Token") or request.args.get("pin_token") or request.cookies.get("pin_token")
        if token != _get_valid_pin_token(SERVER_PIN):
            if request.path in ["/", "/messenger"]:
                return None  # Let page load and render PIN lock overlay in HTML
            return jsonify({"error": "Access PIN required", "pin_required": True}), 401

@app.route("/api/auth/status")
def auth_status():
    ip = request.remote_addr
    track_device(ip, request.headers.get("User-Agent", ""))
    token = request.headers.get("X-Server-PIN-Token") or request.args.get("pin_token") or request.cookies.get("pin_token")
    is_authed = not bool(SERVER_PIN) or (token == _get_valid_pin_token(SERVER_PIN))
    return jsonify({
        "pin_required": bool(SERVER_PIN),
        "is_authenticated": is_authed,
        "is_blocked": ip in _blocked_ips
    })

@app.route("/api/auth/verify", methods=["POST"])
def auth_verify():
    data = request.get_json(force=True) or {}
    pin = str(data.get("pin", "")).strip()
    if not SERVER_PIN:
        return jsonify({"status": "ok", "token": ""})
    if pin == SERVER_PIN:
        token = _get_valid_pin_token(SERVER_PIN)
        return jsonify({"status": "ok", "token": token})
    return jsonify({"status": "error", "error": "Invalid Access PIN"}), 401

@app.route("/api/security/pin", methods=["GET", "POST"])
def security_pin():
    global SERVER_PIN
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        new_pin = str(data.get("pin", "")).strip()
        SERVER_PIN = new_pin
        print(f"[Security] Server PIN set to: '{SERVER_PIN}'")
        return jsonify({"status": "ok", "pin_required": bool(SERVER_PIN), "pin": SERVER_PIN})
    return jsonify({"pin_required": bool(SERVER_PIN), "pin": SERVER_PIN})

# ── ADB Integration Helpers ─────────────────────────────────────────
def _get_adb_cmd():
    import shutil
    # 1. System PATH
    sys_adb = shutil.which("adb.exe" if os.name == 'nt' else "adb")
    if sys_adb:
        return sys_adb

    adb_bin = "adb.exe" if os.name == 'nt' else "adb"
    base = sys._MEIPASS if getattr(sys, 'frozen', False) else BASE_DIR

    # Common search paths
    candidates = [
        os.path.join(base, adb_bin),
        os.path.join(base, "bin", adb_bin),
        r"F:\Install\Android\Sdk\platform-tools\adb.exe",
        os.path.expanduser(r"~\AppData\Local\Android\Sdk\platform-tools\adb.exe"),
        r"C:\platform-tools\adb.exe",
        r"C:\Program Files\Android\platform-tools\adb.exe",
        r"C:\Android\sdk\platform-tools\adb.exe",
        r"D:\Android\Sdk\platform-tools\adb.exe"
    ]

    for path in candidates:
        if os.path.exists(path):
            return path
    return "adb"

@app.route("/api/adb/devices")
def api_adb_devices():
    try:
        import subprocess
        adb_cmd = _get_adb_cmd()
        proc = subprocess.run([adb_cmd, "devices", "-l"], capture_output=True, text=True, timeout=10)
        output = proc.stdout or proc.stderr or ""
        lines = [line.strip() for line in output.splitlines() if line.strip() and not line.startswith("*")]
        devices = []
        for line in lines[1:]: # Skip "List of devices attached"
            parts = line.split()
            if len(parts) >= 2:
                serial = parts[0]
                status = parts[1]
                model = "Android Device"
                for p in parts[2:]:
                    if p.startswith("model:"):
                        model = p.split(":")[1].replace("_", " ")
                
                # Fetch model name from shell if default
                if model == "Android Device" and status == "device":
                    try:
                        m_proc = subprocess.run([adb_cmd, "-s", serial, "shell", "getprop", "ro.product.model"], capture_output=True, text=True, timeout=3)
                        if m_proc.stdout and m_proc.stdout.strip():
                            model = m_proc.stdout.strip()
                    except Exception:
                        pass
                
                conn_type = "USB Cable"
                if ":" in serial or "_adb-tls" in serial:
                    conn_type = "Wireless Wi-Fi"

                devices.append({
                    "serial": serial,
                    "status": status,
                    "model": model,
                    "connection": conn_type
                })
        return jsonify({"status": "ok", "adb_path": adb_cmd, "devices": devices, "count": len(devices)})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e), "devices": []})

@app.route("/api/adb/pair", methods=["POST"])
def api_adb_pair():
    data = request.get_json(force=True) or {}
    ip = data.get("ip", "").strip()
    code = data.get("code", "").strip()
    if not ip or not code:
        return jsonify({"status": "error", "error": "Both Pairing IP:Port and 6-digit Code are required."}), 400
    try:
        import subprocess
        adb_cmd = _get_adb_cmd()
        proc = subprocess.run([adb_cmd, "pair", ip, code], capture_output=True, text=True, timeout=15)
        out = (proc.stdout + "\n" + proc.stderr).strip()
        if "Successfully paired" in out or proc.returncode == 0:
            return jsonify({"status": "ok", "message": f"Successfully paired with {ip}! Now connect using the Connection IP & Port.", "result": out})
        else:
            return jsonify({"status": "error", "error": out or "Pairing failed. Make sure device and PC are on the same Wi-Fi and code is fresh."}), 400
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/api/adb/connect-wifi", methods=["POST"])
def api_adb_connect_wifi():
    data = request.get_json(force=True) or {}
    ip = data.get("ip", "").strip()
    if not ip:
        return jsonify({"status": "error", "error": "Missing IP/Port"}), 400
    try:
        import subprocess
        adb_cmd = _get_adb_cmd()
        proc = subprocess.run([adb_cmd, "connect", ip], capture_output=True, text=True, timeout=15)
        out = (proc.stdout + "\n" + proc.stderr).strip()
        if "connected to" in out.lower() or (proc.returncode == 0 and "cannot connect" not in out.lower() and "failed" not in out.lower()):
            return jsonify({"status": "ok", "message": f"Connected to {ip}", "result": out})
        else:
            return jsonify({
                "status": "error",
                "error": f"Failed to connect: {out}\n\nTip: On Android 11+, make sure to PAIR first using the 'Pairing Code' & 'Pairing Port', then enter the main 'Wireless Debugging' IP:Port here."
            }), 400
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/api/security/devices")
def security_devices():
    with _device_lock:
        now = time.time()
        devs = []
        for ip, info in _connected_devices.items():
            d = info.copy()
            d["is_active"] = (now - d["last_seen"]) < 60
            devs.append(d)
        devs.sort(key=lambda x: x["last_seen"], reverse=True)
        return jsonify(devs)

@app.route("/api/security/device/block", methods=["POST"])
def security_device_block():
    data = request.get_json(force=True) or {}
    target_ip = data.get("ip")
    block = bool(data.get("block", True))
    if target_ip:
        if block:
            _blocked_ips.add(target_ip)
        else:
            _blocked_ips.discard(target_ip)
        with _device_lock:
            if target_ip in _connected_devices:
                _connected_devices[target_ip]["is_blocked"] = block
        print(f"[Security] IP {target_ip} blocked={block}")
        return jsonify({"status": "ok", "ip": target_ip, "blocked": block})
    return jsonify({"error": "Missing ip"}), 400

def clean_filename(filename: str) -> str:

    """
    Relaxed filename sanitizer that keeps spaces, brackets, and most characters
    but prevents path traversal and dangerous OS symbols.
    """
    if not filename:
        return f"upload_{int(time.time())}"
    # Remove path components
    filename = os.path.basename(filename)
    # Strip dangerous characters that could be used for path traversal or shell injection
    # We keep spaces, dots, underscores, dashes, brackets [], and parentheses ()
    # and common alphanumeric chars.
    import re
    # Keep alphanumeric, spaces, dots, hyphens, underscores, brackets, parens
    filename = re.sub(r'[^\w\s\.\-\(\)\[\]]', '', filename)
    # Remove leading dots to prevent hidden files or traversal
    filename = filename.lstrip('.')
    return filename or f"upload_{int(time.time())}"


@app.route("/")
def index():
    local_ip = get_local_ip()
    h_url = PUBLIC_HTTP_URL or f"http://{local_ip}:{HTTP_PORT}"
    w_url = PUBLIC_WS_URL   or f"ws://{local_ip}:{HTTP_PORT}/ws/stream"
    token = request.headers.get("X-Server-PIN-Token") or request.args.get("pin_token") or request.cookies.get("pin_token")
    is_authed = not bool(SERVER_PIN) or (token == _get_valid_pin_token(SERVER_PIN))
    return render_template("index.html", ws_url=w_url, http_url=h_url, 
                           is_public=bool(PUBLIC_HTTP_URL), chat_cipher_key=CIPHER_KEY,
                           pin_required=bool(SERVER_PIN), is_authenticated=is_authed)


@app.route("/messenger")
def messenger():
    """Renders the dedicated messenger page."""
    local_ip = get_local_ip()
    h_url = PUBLIC_HTTP_URL or f"http://{local_ip}:{HTTP_PORT}"
    w_url = PUBLIC_WS_URL   or f"ws://{local_ip}:{HTTP_PORT}/ws/stream"
    token = request.headers.get("X-Server-PIN-Token") or request.args.get("pin_token") or request.cookies.get("pin_token")
    is_authed = not bool(SERVER_PIN) or (token == _get_valid_pin_token(SERVER_PIN))
    return render_template("messenger.html", ws_url=w_url, http_url=h_url, 
                           is_public=bool(PUBLIC_HTTP_URL), chat_cipher_key=CIPHER_KEY,
                           pin_required=bool(SERVER_PIN), is_authenticated=is_authed)


@app.route("/info")
def info():
    """Returns comprehensive server stats and configuration."""
    stats = capturer.get_stats()
    stats["clients"]     = len(_ws_clients)
    stats["ws_url"]      = PUBLIC_WS_URL or f"ws://{get_local_ip()}:{HTTP_PORT}/ws/stream"
    stats["http_url"]    = PUBLIC_HTTP_URL or f"http://{get_local_ip()}:{HTTP_PORT}"
    stats["is_public"]   = bool(PUBLIC_HTTP_URL)
    stats["server_time"] = time.time()
    stats["audio"]       = audio_capturer.get_info()
    stats["target_fps"]  = capturer.target_fps
    stats["quality"]     = capturer.jpeg_quality
    stats["remote_control"] = remote_control_on
    stats["pin_required"] = bool(SERVER_PIN)
    return jsonify(stats)




@app.route("/settings/ngrok_info")
def ngrok_info():
    return jsonify({
        "is_public": bool(PUBLIC_HTTP_URL),
        "url": PUBLIC_HTTP_URL
    })


@app.route("/settings/public", methods=["POST"])
def settings_public():
    """Start or stop public access via Ngrok."""
    data   = request.get_json(force=True) or {}
    action = data.get("action")
    token  = data.get("token")

    if action == "start":
        if not token:
            return jsonify({"status": "error", "error": "Ngrok token is required"}), 400
        success, result = start_public_access(token)
        if success:
            return jsonify({"status": "ok", "url": result})
        else:
            return jsonify({"status": "error", "error": result}), 500
    elif action == "stop":
        try:
            from pyngrok import ngrok
            ngrok.kill()
            global PUBLIC_HTTP_URL, PUBLIC_WS_URL
            PUBLIC_HTTP_URL = None
            PUBLIC_WS_URL   = None
            return jsonify({"status": "ok"})
        except Exception as e:
            return jsonify({"status": "error", "error": str(e)}), 500

    return jsonify({"status": "error", "error": "Invalid action"}), 400



def start_public_access(token):
    global PUBLIC_HTTP_URL, PUBLIC_WS_URL
    try:
        import subprocess
        # ── Monkey patch subprocess.Popen to hide the Ngrok window on Windows ──
        if os.name == 'nt':
            original_popen = subprocess.Popen
            def hidden_popen(*args, **kwargs):
                if getattr(sys, 'frozen', False):
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startupinfo.wShowWindow = subprocess.SW_HIDE
                    kwargs['startupinfo'] = startupinfo
                    kwargs['creationflags'] = 0x08000000 # CREATE_NO_WINDOW
                return original_popen(*args, **kwargs)
            subprocess.Popen = hidden_popen

        from pyngrok import ngrok, conf
        
        # Configure PyNgrok to use bundled ngrok instead of downloading
        ngrok_bin = "ngrok.exe" if os.name == 'nt' else "ngrok"
        ngrok_exe_path = os.path.join(sys._MEIPASS if getattr(sys, 'frozen', False) else BASE_DIR, ngrok_bin)
        if os.path.exists(ngrok_exe_path):
            conf.get_default().ngrok_path = ngrok_exe_path

        # Kill any existing ngrok processes to resolve ERR_NGROK_334 (Endpoint Already Online)
        if os.name == 'nt':
            try:
                os.system("taskkill /f /im ngrok.exe >nul 2>&1")
                time.sleep(1.5)  # Allow system to release sockets
            except:
                pass
        else:
            try:
                os.system("killall ngrok >/dev/null 2>&1")
                time.sleep(1.5)
            except:
                pass

        if token:
            ngrok.set_auth_token(token)
        
        # Kill existing tunnels if any
        try:
            for t in ngrok.get_tunnels():
                ngrok.disconnect(t.public_url)
        except:
            pass
            
        print("[Ngrok] Starting single tunnel for port 5000...")
        # Single tunnel for BOTH Flask and WebSockets (on port 5000)
        h_tunnel = ngrok.connect(HTTP_PORT, bind_tls=True)
        PUBLIC_HTTP_URL = h_tunnel.public_url
        
        # Single hostname handles both now via paths /ws/stream and /ws/chat
        PUBLIC_WS_URL = PUBLIC_HTTP_URL.replace("https://", "wss://").replace("http://", "ws://")
        
        print("="*60)
        print(f" PUBLIC ACCESS ENABLED (Single Port Mode)")
        print(f" URL: {PUBLIC_HTTP_URL}")
        print("="*60)
        return True, PUBLIC_HTTP_URL
    except Exception as e:
        print(f"[Ngrok Error] {e}")
        return False, str(e)






@app.route("/favicon.ico")
def favicon():
    return send_from_directory('static', 'icon.png', mimetype='image/png')

@app.route("/manifest.json")
def manifest():
    return send_from_directory('static', 'manifest.json')

@app.route("/sw.js")
def service_worker():
    response = make_response(send_from_directory('static', 'sw.js'))
    response.headers['Content-Type'] = 'application/javascript'
    return response
    

# ── Chat API Routes ───────────────────────────────────────────────────

# ── Chat Support Utilities ────────────────────────────────────────────

def broadcast_chat_to_all(msg_str, skip_mac=None):
    with _ws_chat_lock:
        for hw_mac, ws_list in _mac_to_ws_chat.items():
            if skip_mac and hw_mac == skip_mac: continue
            for websocket in ws_list:
                try: websocket.send(msg_str)
                except Exception: pass

def send_chat_to_mac(target_mac, msg_str):
    with _ws_chat_lock:
        if target_mac in _mac_to_ws_chat:
            for websocket in _mac_to_ws_chat[target_mac]:
                try: websocket.send(msg_str)
                except Exception: pass

def broadcast_user_list():
    msg = json.dumps({"type": "system", "action": "update_users"})
    broadcast_chat_to_all(msg)

def broadcast_update(msg_id):
    conn = get_chat_db()
    row = conn.execute("SELECT m.*, u.username as sender_name FROM messages m LEFT JOIN users u ON m.from_mac = u.mac WHERE m.id=?", (msg_id,)).fetchone()
    if not row: return
    d = dict(row)
    d["from_mac"] = obfuscate_mac(d["from_mac"])
    d["to_target"] = obfuscate_mac(d["to_target"])
    recs = conn.execute("SELECT emoji, mac FROM message_reactions WHERE msg_id=?", (msg_id,)).fetchall()
    d["reactions"] = {}
    for r in recs:
        emo = r["emoji"]
        if emo not in d["reactions"]: d["reactions"][emo] = []
        d["reactions"][emo].append(obfuscate_mac(r["mac"]))
    
    update_msg = json.dumps({"type": "update_msg", "msg": d})
    to_target = row["to_target"]
    if to_target == "group": 
        broadcast_chat_to_all(update_msg)
    elif to_target.startswith("cg_"):
        members = [r["mac"] for r in get_chat_db().execute("SELECT mac FROM group_members WHERE group_id=?", (to_target,)).fetchall()]
        for m in members: send_chat_to_mac(m, update_msg)
    else:
        send_chat_to_mac(row["from_mac"], update_msg)
        send_chat_to_mac(to_target, update_msg)

@app.route("/api/user", methods=["GET", "POST"])
def api_user():
    ip = request.remote_addr
    mac = get_mac_address(ip)
    log_mac_id(mac)
    conn = get_chat_db()
    if request.method == "POST":
        data = request.get_json() or {}
        name = data.get("username")
        if name:
            conn.execute("INSERT OR REPLACE INTO users (mac, username, avatar) VALUES (?, ?, (SELECT avatar FROM users WHERE mac=?))", (mac, name, mac))
            conn.commit()
            broadcast_user_list()
        return jsonify({"status": "ok", "mac": obfuscate_mac(mac), "username": name})
        
    row = conn.execute("SELECT username, avatar FROM users WHERE mac = ?", (mac,)).fetchone()
    if row: return jsonify({"username": row["username"], "mac": obfuscate_mac(mac), "avatar": row["avatar"]})
    return jsonify({"username": None, "mac": obfuscate_mac(mac), "avatar": None})

@app.route("/api/users")
def api_users_list():
    conn = get_chat_db()
    rows = conn.execute("SELECT mac, username, avatar FROM users").fetchall()
    users = []
    for r in rows:
        u_mac = r["mac"]
        log_mac_id(u_mac)
        users.append({
            "mac": obfuscate_mac(u_mac),
            "username": r["username"],
            "avatar": r["avatar"],
            "is_active": bool(_mac_to_ws_chat.get(u_mac))
        })
    return jsonify(users)

@app.route("/api/groups", methods=["GET", "POST"])
def api_groups():
    my_mac = get_mac_address(request.remote_addr)
    conn = get_chat_db()
    if request.method == "POST":
        data = request.get_json() or {}
        name = data.get("name", "Unnamed Group")
        members_obf = data.get("members", [])
        members = [get_real_mac(m) for m in members_obf]
        if my_mac not in members: members.append(my_mac)
        group_id = f"cg_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO custom_groups (id, name, created_by) VALUES (?, ?, ?)", (group_id, name, my_mac))
        for m_mac in members:
            conn.execute("INSERT INTO group_members (group_id, mac) VALUES (?, ?)", (group_id, m_mac))
        conn.commit()
        msg = json.dumps({"type": "system", "action": "update_groups"})
        for hw_mac in members:
            send_chat_to_mac(hw_mac, msg)
        return jsonify({"status": "ok", "id": group_id})
    rows = conn.execute("SELECT cg.* FROM custom_groups cg JOIN group_members gm ON cg.id = gm.group_id WHERE gm.mac = ?", (my_mac,)).fetchall()
    res = []
    for r in rows:
        d = dict(r)
        d["created_by"] = obfuscate_mac(d.get("created_by"))
        res.append(d)
    return jsonify(res)

@app.route("/api/groups/<group_id>", methods=["PUT"])
def update_group(group_id):
    my_mac = get_mac_address(request.remote_addr)
    conn = get_chat_db()
    group = conn.execute("SELECT * FROM custom_groups WHERE id=?", (group_id,)).fetchone()
    if not group or group["created_by"] != my_mac: return jsonify({"error": "Unauthorized"}), 403
    data = request.get_json() or {}
    new_name = data.get("name")
    members_obf = data.get("members", [])
    members = [get_real_mac(m) for m in members_obf]
    if my_mac not in members: members.append(my_mac)
    if new_name: conn.execute("UPDATE custom_groups SET name=? WHERE id=?", (new_name, group_id))
    old_members = set([r["mac"] for r in conn.execute("SELECT mac FROM group_members WHERE group_id=?", (group_id,)).fetchall()])
    conn.execute("DELETE FROM group_members WHERE group_id=?", (group_id,))
    for m_mac in members: conn.execute("INSERT INTO group_members (group_id, mac) VALUES (?, ?)", (group_id, m_mac))
    conn.commit()
    all_affected = old_members.union(set(members))
    msg = json.dumps({"type": "system", "action": "update_groups"})
    for hw_mac in all_affected: send_chat_to_mac(hw_mac, msg)
    return jsonify({"status": "ok"})

@app.route("/api/groups/<group_id>/members", methods=["GET"])
def get_group_members(group_id):
    my_mac = get_mac_address(request.remote_addr)
    conn = get_chat_db()
    chk = conn.execute("SELECT 1 FROM group_members WHERE group_id=? AND mac=?", (group_id, my_mac)).fetchone()
    if not chk: return jsonify({"error": "Unauthorized"}), 403
    rows = conn.execute("SELECT mac FROM group_members WHERE group_id=?", (group_id,)).fetchall()
    return jsonify([obfuscate_mac(r["mac"]) for r in rows])

@app.route("/api/sidebar")
def api_sidebar():
    my_mac = get_mac_address(request.remote_addr)
    log_mac_id(my_mac)
    page = int(request.args.get("page", 0))
    limit = int(request.args.get("limit", 20))
    offset = page * limit
    conn = get_chat_db()
    previews = {}
    r = conn.execute("SELECT id, content, msg_type, is_deleted FROM messages WHERE to_target='group' ORDER BY id DESC LIMIT 1").fetchone()
    if r: previews["group"] = {"text": "🚫 Deleted" if r["is_deleted"] else (r["content"] if r["msg_type"]=="text" else "📎 [File]"), "time_id": r["id"]}
    else: previews["group"] = {"text": "No messages yet", "time_id": -1}
    cgroups = conn.execute("SELECT g.id, g.name, g.created_by FROM custom_groups g JOIN group_members gm ON g.id = gm.group_id WHERE gm.mac=?", (my_mac,)).fetchall()
    for cg in cgroups:
        cg_id = cg["id"]
        r = conn.execute("SELECT id, content, msg_type, is_deleted FROM messages WHERE to_target=? ORDER BY id DESC LIMIT 1", (cg_id,)).fetchone()
        previews[cg_id] = {"text": "🚫 Deleted" if r["is_deleted"] else (r["content"] if r["msg_type"]=="text" else "📎 [File]"), "time_id": r["id"]} if r else {"text": "No messages yet", "time_id": -1}
    rows = conn.execute("SELECT id, from_mac, to_target, content, msg_type, is_deleted FROM messages WHERE (from_mac=? AND to_target != 'group' AND to_target NOT LIKE 'cg_%') OR (to_target=?) ORDER BY id DESC", (my_mac, my_mac)).fetchall()
    temp_previews = {}
    for row in rows:
        other_mac = row["from_mac"] if row["to_target"] == my_mac else row["to_target"]
        if other_mac not in temp_previews:
            txt = "🚫 Deleted" if row["is_deleted"] else (row["content"] if row["msg_type"]=="text" else "📎 [File]")
            temp_previews[other_mac] = {"text": txt, "time_id": row["id"]}
    previews.update(temp_previews)
    pinned_rows = conn.execute("SELECT target_id FROM pinned_chats WHERE mac=?", (my_mac,)).fetchall()
    pinned_ids = set(r["target_id"] for r in pinned_rows)
    list_items = []
    def add_item(tid, title, type, created_by=None, is_active=False):
        list_items.append({"id": obfuscate_mac(tid), "title": title, "type": type, "time_id": previews.get(tid, {"time_id": -1})["time_id"], "created_by": obfuscate_mac(created_by) if created_by else None, "is_active": is_active, "is_pinned": tid in pinned_ids})
    add_item("group", "Global Chat", "global")
    for cg in cgroups: add_item(cg["id"], cg["name"], "cgroup", cg["created_by"])
    users = conn.execute("SELECT mac, username FROM users").fetchall()
    active_macs = set(_mac_to_ws_chat.keys())
    for u in users:
        u_mac = u["mac"]
        if u_mac == my_mac: continue
        add_item(u_mac, u["username"], "user", is_active=(u_mac in active_macs))
    obf_previews = {obfuscate_mac(k): v for k, v in previews.items()}
    list_items.sort(key=lambda x: (x["is_pinned"], x["time_id"], x.get("is_active", 0)), reverse=True)
    paged_items = list_items[offset : offset + limit]
    return jsonify({"items": paged_items, "previews": obf_previews, "has_more": len(list_items) > (offset + limit)})

@app.route("/api/messages/<int:msg_id>/react", methods=["POST", "DELETE"])
def api_react(msg_id):
    my_mac = get_mac_address(request.remote_addr)
    conn = get_chat_db()
    if request.method == "POST":
        data = request.get_json() or {}
        emoji = data.get("emoji")
        if emoji:
            conn.execute("INSERT OR REPLACE INTO message_reactions (msg_id, mac, emoji) VALUES (?, ?, ?)", (msg_id, my_mac, emoji))
            conn.commit()
    else:
        conn.execute("DELETE FROM message_reactions WHERE msg_id=? AND mac=?", (msg_id, my_mac))
        conn.commit()
    broadcast_update(msg_id)
    return jsonify({"status": "ok"})

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "")
    if not q: return jsonify([])
    my_mac = get_mac_address(request.remote_addr)
    conn = get_chat_db()
    
    # Simple keyword search across all accessible messages
    # Accessible = Global + Groups I am in + Private chats where I am a participant
    accessible_groups = [r["group_id"] for r in conn.execute("SELECT group_id FROM group_members WHERE mac=?", (my_mac,)).fetchall()]
    
    query = "SELECT m.*, u.username as sender_name FROM messages m LEFT JOIN users u ON m.from_mac = u.mac WHERE ("
    query += "m.to_target='group' OR m.to_target IN ({}) OR m.from_mac=? OR m.to_target=?".format(",".join(["'"+g+"'" for g in accessible_groups]) if accessible_groups else "''")
    query += ") AND m.content LIKE ? AND m.is_deleted=0 ORDER BY m.id DESC LIMIT 50"
    
    params = [my_mac, my_mac, f"%{encrypt_data(q)}%"]
    
    rows = conn.execute(query, params).fetchall()
    res = []
    for r in rows:
        d = dict(r)
        d["from_mac"] = obfuscate_mac(d["from_mac"])
        # We don't obfuscate to_target here as it helps the frontend know which chat context to switch to
        res.append(d)
    return jsonify(res)
@app.route("/api/messages")
def api_messages():
    target_param = request.args.get("target", "group")
    target = get_real_mac(target_param)
    before_id = request.args.get("before_id", type=int)
    limit = request.args.get("limit", 20, type=int)
    my_mac = get_mac_address(request.remote_addr)
    conn = get_chat_db()
    query = "SELECT m.*, u.username as sender_name FROM messages m LEFT JOIN users u ON m.from_mac = u.mac WHERE "
    params = []
    if target == "group": query += "m.to_target='group'"
    elif target.startswith("cg_"):
        query += "m.to_target=?"
        params.append(target)
    else:
        query += "((m.from_mac=? AND m.to_target=?) OR (m.from_mac=? AND m.to_target=?))"
        params.extend([my_mac, target, target, my_mac])
    if before_id:
        query += " AND m.id < ?"
        params.append(before_id)
    query += " ORDER BY m.id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    messages = []
    for row in rows:
        d = dict(row)
        d["from_mac"] = obfuscate_mac(d["from_mac"])
        d["to_target"] = obfuscate_mac(d["to_target"])
        recs = conn.execute("SELECT emoji, mac FROM message_reactions WHERE msg_id=?", (d["id"],)).fetchall()
        d["reactions"] = {}
        for r in recs:
            emo = r["emoji"]
            if emo not in d["reactions"]: d["reactions"][emo] = []
            d["reactions"][emo].append(obfuscate_mac(r["mac"]))
        messages.append(d)
    messages.reverse()
    return jsonify(messages)


@app.route("/api/pins", methods=["POST", "DELETE"])
def api_pins():
    my_mac = get_mac_address(request.remote_addr)
    data = request.get_json() or {}
    target_obf = data.get("target")
    if not target_obf: return jsonify({"error": "No target"}), 400
    target = get_real_mac(target_obf)
    
    conn = get_db()
    if request.method == "POST":
        conn.execute("INSERT OR REPLACE INTO pinned_chats (mac, target_id) VALUES (?, ?)", (my_mac, target))
    else:
        conn.execute("DELETE FROM pinned_chats WHERE mac=? AND target_id=?", (my_mac, target))
    conn.commit()
    return jsonify({"status": "ok"})

@app.route("/chat/upload", methods=["POST"])
def chat_upload():
    try:
        file = request.files['file']
        if not file or file.filename == '': return jsonify({"error": "No file"}), 400
        filename = clean_filename(file.filename)
        name, ext = os.path.splitext(filename)
        filepath = os.path.join(CHAT_UPLOAD_FOLDER, filename)
        counter = 1
        while os.path.exists(filepath):
            filename = f"{name}({counter}){ext}"
            filepath = os.path.join(CHAT_UPLOAD_FOLDER, filename)
            counter += 1
        file.save(filepath)
        file_url = f"/chat/download/{filename}"
        is_voice = request.form.get("msg_type") == "voice"
        return jsonify({
            "status": "ok", 
            "filename": filename, 
            "url": file_url,
            "is_voice": is_voice
        })
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/chat/download/<path:filename>")
def chat_download(filename):
    return send_from_directory(CHAT_UPLOAD_FOLDER, unquote(filename))

@app.route("/api/avatar", methods=["POST"])
def upload_avatar():
    try:
        file = request.files['file']
        if not file: return jsonify({"error": "No file"}), 400
        my_mac = get_mac_address(request.remote_addr)
        filename = f"avatar_{obfuscate_mac(my_mac)}_{int(time.time())}.jpg"
        filepath = os.path.join(CHAT_UPLOAD_FOLDER, filename)
        file.save(filepath)
        avatar_url = f"/chat/download/{filename}"
        conn = get_db()
        conn.execute("UPDATE users SET avatar = ? WHERE mac = ?", (avatar_url, my_mac))
        conn.commit()
        broadcast_user_list()
        return jsonify({"avatar": avatar_url})
    except Exception as e: return jsonify({"error": str(e)}), 500


# =========================================================================

# File Share Routes
# =========================================================================

# 📤 Upload
@app.route("/upload", methods=["POST"])
def upload():
    try:
        if 'file' not in request.files:
            return {"error": "Missing file field"}, 400
        file = request.files['file']
        if not file or file.filename == '':
            return {"error": "No file selected"}, 400

        filename = clean_filename(file.filename)
        if not filename:
            # Fallback if secure_filename removes everything (e.g. only unicode or dots)
            filename = f"upload_{int(time.time())}"

        name, ext = os.path.splitext(filename)
        filepath  = os.path.join(UPLOAD_FOLDER, filename)
        counter   = 1
        while os.path.exists(filepath):
            filename = f"{name}({counter}){ext}"
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            counter += 1

        file.save(filepath)
        print(f"[Upload] Saved: {filename}")
        return {"status": "ok", "filename": filename, "url": f"/download/{filename}"}
    except Exception as e:
        print(f"[Upload Error] {e}")
        return {"status": "error", "message": str(e)}, 500


# 📥 Download
@app.route("/download/<path:filename>")
def download(filename):
    filename  = unquote(filename)
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    return "File not found", 404


# 🔍 Inline File Preview / Thumbnail
@app.route("/file-preview/<path:filename>")
def file_preview(filename):
    filename  = unquote(filename)
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(file_path):
        return "File not found", 404
    
    ext = os.path.splitext(filename)[1].lower()
    is_thumb = request.args.get("thumb") == "1"
    
    # Image thumbnail generation
    if ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp']:
        if is_thumb:
            try:
                with Image.open(file_path) as img:
                    img.thumbnail((120, 120), Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    fmt = "PNG" if ext in ['.png', '.webp', '.gif'] else "JPEG"
                    if fmt == "JPEG" and img.mode in ("RGBA", "P"):
                        img = img.convert("RGB")
                    img.save(buf, format=fmt, quality=85)
                    buf.seek(0)
                    mimetype = f"image/{fmt.lower()}"
                    return Response(buf.read(), mimetype=mimetype)
            except Exception as e:
                print(f"[Thumb Error] {e}")
                pass
        return send_file(file_path, as_attachment=False)
        
    return send_file(file_path, as_attachment=False)


# 📦 Download multiple as ZIP
@app.route("/download-multiple", methods=["POST"])
def download_multiple():
    files       = request.json.get('files', [])
    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w') as zf:
        for file_name in files:
            file_path = os.path.join(UPLOAD_FOLDER, file_name)
            if os.path.exists(file_path):
                zf.write(file_path, arcname=file_name)
    memory_file.seek(0)
    return send_file(memory_file, download_name="files.zip", as_attachment=True)


# 🗑 Delete
@app.route("/delete/<path:filename>", methods=["DELETE"])
def delete_file(filename):
    filename = unquote(filename)
    path     = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(path):
        os.remove(path)
        return {"status": "deleted"}
    return {"error": "file not found"}, 404


# 📂 File list fragment (HTML)
@app.route("/files")
def files_list():
    import html
    files = os.listdir(UPLOAD_FOLDER)
    if not files:
        return "<p style='color:var(--muted);text-align:center;padding:40px 0'>No files uploaded yet</p>"
    files.sort(key=lambda x: os.path.getmtime(os.path.join(UPLOAD_FOLDER, x)), reverse=True)
    rows = ""
    for f in files:
        path     = os.path.join(UPLOAD_FOLDER, f)
        size     = os.path.getsize(path)
        size_mb  = size / (1024 * 1024)
        size_str = f"{size_mb/1024:.2f} GB" if size_mb > 1024 else f"{size_mb:.2f} MB"
        mtime    = os.path.getmtime(path)
        date_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        
        ext = os.path.splitext(f)[1].lower()
        is_image = ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp']
        is_video = ext in ['.mp4', '.webm', '.mov', '.mkv', '.avi']
        is_audio = ext in ['.mp3', '.wav', '.flac', '.ogg', '.m4a']
        
        ftype = 'image' if is_image else ('video' if is_video else ('audio' if is_audio else 'other'))
        safe_attr_f = html.escape(f, quote=True)
        
        if is_image:
            thumb_html = f'''<div class="file-thumb-wrap" data-filename="{safe_attr_f}" data-ftype="{ftype}" onclick="handlePreviewClick(this)" title="Click to preview thumbnail">
                <img src="/file-preview/{safe_attr_f}?thumb=1" class="file-thumb-img" alt="{safe_attr_f}" loading="lazy" onerror="this.onerror=null;this.parentElement.innerHTML=\'<span class=\\&quot;file-type-badge\\&quot;>{_file_icon(f)}</span>\';"/>
            </div>'''
        elif is_video:
            thumb_html = f'''<div class="file-thumb-wrap video-thumb" data-filename="{safe_attr_f}" data-ftype="{ftype}" onclick="handlePreviewClick(this)" title="Click to play video">
                <span class="file-type-badge">🎬</span><span class="play-overlay">▶</span>
            </div>'''
        elif is_audio:
            thumb_html = f'''<div class="file-thumb-wrap audio-thumb" data-filename="{safe_attr_f}" data-ftype="{ftype}" onclick="handlePreviewClick(this)" title="Click to play audio">
                <span class="file-type-badge">🎵</span>
            </div>'''
        else:
            thumb_html = f'''<div class="file-thumb-wrap" data-filename="{safe_attr_f}" data-ftype="{ftype}" onclick="handlePreviewClick(this)" title="Click for file details">
                <span class="file-type-badge">{_file_icon(f)}</span>
            </div>'''

        rows += f"""
        <tr class="file-row">
            <td><input type="checkbox" class="file-checkbox" value="{safe_attr_f}" onchange="updateBulkActions()"></td>
            <td class="file-name-cell">
                {thumb_html}
                <div class="file-name-info">
                    <span class="file-name-text" data-filename="{safe_attr_f}" data-ftype="{ftype}" onclick="handlePreviewClick(this)" title="{safe_attr_f}">{html.escape(f)}</span>
                    <span class="file-ext-tag">{ext.replace('.','').upper() or 'FILE'}</span>
                </div>
            </td>
            <td class="file-date">{date_str}</td>
            <td class="file-size">{size_str}</td>
            <td>
                <div class="file-actions">
                    <button type="button" class="action-btn prev-btn" data-filename="{safe_attr_f}" data-ftype="{ftype}" onclick="handlePreviewClick(this)" title="Preview">👁</button>
                    <a href='/download/{safe_attr_f}' class="action-btn dl-btn" title="Download">⬇</a>
                    <button type="button" class="action-btn del-btn" data-filename="{safe_attr_f}" onclick="handleDeleteClick(this)" title="Delete">🗑</button>
                </div>
            </td>
        </tr>
        """
    return f"""
    <table class="file-table">
        <thead>
            <tr>
                <th><input type="checkbox" onclick="toggleAll(this)"></th>
                <th>File Name</th>
                <th>Date</th>
                <th>Size</th>
                <th>Actions</th>
            </tr>
        </thead>
        <tbody>{rows}</tbody>
    </table>
    """


def _file_icon(filename: str) -> str:
    """Return an emoji icon based on file extension."""
    ext = os.path.splitext(filename)[1].lower()
    icon_map = {
        '.pdf': '📄', '.doc': '📝', '.docx': '📝', '.txt': '📃',
        '.xls': '📊', '.xlsx': '📊', '.csv': '📊',
        '.ppt': '📊', '.pptx': '📊',
        '.zip': '📦', '.rar': '📦', '.7z': '📦', '.tar': '📦', '.gz': '📦',
        '.jpg': '🖼', '.jpeg': '🖼', '.png': '🖼', '.gif': '🖼', '.svg': '🖼', '.webp': '🖼',
        '.mp4': '🎬', '.mov': '🎬', '.avi': '🎬', '.mkv': '🎬', '.webm': '🎬',
        '.mp3': '🎵', '.wav': '🎵', '.flac': '🎵', '.aac': '🎵',
        '.py': '🐍', '.js': '📜', '.html': '🌐', '.css': '🎨',
        '.exe': '⚙', '.msi': '⚙', '.bat': '⚙',
        '.apk': '📱', '.ipa': '📱',
    }
    return icon_map.get(ext, '📎')


# 📋 Clipboard
@app.route("/clipboard", methods=["GET", "POST"])
def clipboard():
    global clipboard_data, clipboard_version
    if request.method == 'POST':
        data     = request.get_json() or {}
        new_text = data.get("text", "")
        if new_text != clipboard_data:
            clipboard_data    = new_text
            clipboard_version += 1
            # Broadcast to all WS clients
            _trigger_ws_clipboard_broadcast(new_text)
        return {"status": "ok"}
    return {"text": clipboard_data, "version": clipboard_version}


def _trigger_ws_clipboard_broadcast(text: str, exclude_cid: int | None = None):
    """Helper to broadcast clipboard text via the WS thread's event loop."""
    _broadcast_clipboard(text, exclude_cid)



# ⚡ SSE — real-time file + clipboard updates
@app.route("/events")
def events():
    def stream():
        last_files             = set()
        last_clipboard_version = 0
        while True:
            current_files = set(os.listdir(UPLOAD_FOLDER))
            if current_files != last_files:
                yield "data: update\n\n"
                last_files = current_files
            if clipboard_version != last_clipboard_version:
                yield "data: clipboard\n\n"
                last_clipboard_version = clipboard_version
            time.sleep(1)
    return Response(stream(), mimetype='text/event-stream')

# ── Source and Settings ──────────────────────────────────────────────



@app.route("/sources")
def sources():
    monitors = list_monitors()
    windows  = list_windows()
    current  = capturer.get_source_info()
    return jsonify({"monitors": monitors, "windows": windows, "current": current})


@app.route("/source", methods=["POST"])
def set_source():
    data = request.get_json(force=True) or {}
    src  = data.get("type", "monitor")

    if src == "monitor":
        idx = int(data.get("index", 1))
        capturer.set_source_monitor(idx)
    elif src == "region":
        capturer.set_source_region(
            int(data.get("x", 0)), int(data.get("y", 0)),
            int(data.get("w", 800)), int(data.get("h", 600))
        )
    elif src == "window":
        capturer.set_source_window(
            int(data.get("hwnd", 0)), data.get("title", "")
        )
    return jsonify({"ok": True, "source": capturer.get_source_info()})


@app.route("/settings", methods=["POST"])
def settings():
    data = request.get_json(force=True) or {}
    if "fps"     in data: capturer.set_fps(int(data["fps"]))
    if "quality" in data: capturer.set_quality(int(data["quality"]))
    if "width"   in data: capturer.set_max_width(int(data["width"]))
    return jsonify(capturer.get_stats())


@app.route("/audio", methods=["POST"])
def audio_toggle():
    """Toggle audio streaming on/off."""
    data = request.get_json(force=True) or {}
    if "enabled" in data:
        audio_capturer.enabled = bool(data["enabled"])
    return jsonify(audio_capturer.get_info())


@app.route("/thumbnail/monitor/<int:index>")
def thumb_monitor(index):
    data = make_thumb(monitor_index=index)
    return Response(data, mimetype="image/jpeg")


@app.route("/thumbnail/window/<int:hwnd>")
def thumb_window(hwnd):
    from capture import get_window_rect
    r = get_window_rect(hwnd)
    if r:
        x, y, w, h = r
        data = make_thumb(region={"top": y, "left": x,
                                   "width": min(w, 1920),
                                   "height": min(h, 1080)})
    else:
        data = b""
    return Response(data, mimetype="image/jpeg")


@app.route("/qr.png")
def qr_endpoint():
    # Use public URL for QR code if available
    url = PUBLIC_HTTP_URL or f"http://{get_local_ip()}:{HTTP_PORT}"
    img = make_qr_image(url, size=300)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(buf.read(), mimetype="image/png")


# ── Remote Control Logic ──────────────────────────────────────────────
def process_control_event(payload: bytes):
    """Processes normalized input events from client and simulates them on host."""
    if not remote_control_on:
        return

    try:
        data = json.loads(payload.decode("utf-8"))
        etype = data.get("type")
        
        # Get active capture region for mapping

        sx, sy, sw, sh = capturer.get_active_rect()

        if etype in ["mm", "md", "mu", "clk"]:  # Mouse: move, down, up, click
            rx, ry = data.get("x", 0), data.get("y", 0)
            btn    = data.get("btn", "left")
            
            # Map normalized (0-1) to absolute pixels
            abs_x = int(sx + (rx * sw))
            abs_y = int(sy + (ry * sh))
            
            if etype == "mm": # mouse move
                pyautogui.moveTo(abs_x, abs_y, _pause=False)
            elif etype == "md": # mouse down
                pyautogui.mouseDown(abs_x, abs_y, button=btn)
            elif etype == "mu": # mouse up
                pyautogui.mouseUp(abs_x, abs_y, button=btn)
            elif etype == "clk": # click
                pyautogui.click(abs_x, abs_y, button=btn)

        elif etype == "sc": # scroll
            # pyautogui.scroll(clicks) — positive is up, negative is down
            # Note: browsers use different wheelDelta logic, we'll normalize on client
            v = data.get("v", 0)
            if v != 0:
                pyautogui.scroll(v)

        elif etype in ["kd", "ku", "kp"]: # Key: down, up, press
            key = data.get("key")
            if not key: return
            
            # Basic mapping for special keys if needed
            # PYAUTOGUI keys: ['accept', 'add', 'alt', 'altleft', 'altright', 'apps', 'backspace', ...]
            key = key.lower()
            if key == "control": key = "ctrl"
            
            try:
                if etype == "kd": pyautogui.keyDown(key)
                elif etype == "ku": pyautogui.keyUp(key)
                elif etype == "kp": pyautogui.press(key)
            except Exception:
                pass # ignore invalid keys

    except Exception as e:
        print(f"[Control Error] {e}")


# ── WebSocket server ──────────────────────────────────────────────────

# ── Chat WebSocket Server ─────────────────────────────────────────────

@sock.route("/ws/chat")
def ws_chat_handler(ws):
    ip = request.remote_addr
    mac = get_mac_address(ip)
    with _ws_chat_lock:
        if mac not in _mac_to_ws_chat: _mac_to_ws_chat[mac] = []
        _mac_to_ws_chat[mac].append(ws)
    broadcast_user_list()
    try:
        while True:
            msg = ws.receive()
            if not msg: break
            data = json.loads(msg)
            etype = data.get("type")
            
            if etype in ["edit", "delete"]:
                msg_id = data.get("msg_id")
                new_text = data.get("text", "")
                is_delete = etype == "delete"
                conn = get_chat_db()
                row = conn.execute("SELECT * FROM messages WHERE id=? AND from_mac=?", (msg_id, mac)).fetchone()
                if row:
                    final_text = "🚫 _This message was deleted_" if is_delete else new_text
                    if not is_delete: final_text = "ENC:" + encrypt_data(final_text)
                    conn.execute("UPDATE messages SET content=?, is_edited=?, is_deleted=?, file_url=NULL, filename=NULL WHERE id=?", (final_text, 1 if not is_delete else 0, 1 if is_delete else 0, msg_id))
                    conn.commit()
                    broadcast_update(msg_id)
                continue

            if etype == "typing":
                tid = data.get("target")
                real_target = get_real_mac(tid)
                status_msg = json.dumps({"type": "typing", "from": obfuscate_mac(mac), "target": tid, "is_typing": data.get("is_typing", False)})
                if real_target == "group": broadcast_chat_to_all(status_msg, skip_mac=mac)
                elif real_target.startswith("cg_"):
                    members = [r["mac"] for r in get_chat_db().execute("SELECT mac FROM group_members WHERE group_id=?", (real_target,)).fetchall()]
                    for m in members:
                        if m != mac: send_chat_to_mac(m, status_msg)
                else: send_chat_to_mac(real_target, status_msg)
                continue

            if etype == "read":
                target_id = data.get("to_target")
                target = get_real_mac(target_id)
                if target and target != "group" and not target.startswith("cg_"):
                    conn = get_chat_db()
                    conn.execute("UPDATE messages SET status='read' WHERE from_mac=? AND to_target=? AND status='sent'", (target, mac))
                    conn.commit()
                    receipt_str = json.dumps({"type": "read_receipt", "reader": obfuscate_mac(mac)})
                    send_chat_to_mac(target, receipt_str)
                continue

            to_target_obf = data.get("to_target", "group")
            to_target = get_real_mac(to_target_obf)
            conn = get_chat_db()
            if to_target.startswith("cg_"):
                member_chk = conn.execute("SELECT 1 FROM group_members WHERE group_id=? AND mac=?", (to_target, mac)).fetchone()
                if not member_chk: continue 
            
            c = conn.cursor()
            content = data.get("text", "")
            if etype == "text": content = "ENC:" + encrypt_data(content)
            c.execute("INSERT INTO messages (from_mac, to_target, msg_type, content, filename, file_url, filetype) VALUES (?, ?, ?, ?, ?, ?, ?)", (mac, to_target, etype, content, data.get("filename"), data.get("url"), data.get("filetype")))
            conn.commit()
            broadcast_update(c.lastrowid)
    except Exception: pass
    finally:
        with _ws_chat_lock:
            if mac in _mac_to_ws_chat and ws in _mac_to_ws_chat[mac]:
                _mac_to_ws_chat[mac].remove(ws)
                if not _mac_to_ws_chat[mac]: del _mac_to_ws_chat[mac]
        broadcast_user_list()


# ── Streaming WebSocket server ────────────────────────────────────────

@sock.route("/ws/stream")
def stream_ws_handler(ws):
    """Handles a single WebSocket client via Flask-Sock (threaded)."""
    global clipboard_data, clipboard_version
    cid = id(ws)
    q = Queue(maxsize=3)
    ws_send_lock = threading.Lock()
    def safe_send(data):
        with ws_send_lock:
            try: ws.send(data)
            except Exception: pass

    with _ws_lock:
        _ws_clients[cid] = {"ws": ws, "q": q}

    cfg = {
        "sample_rate": audio_capturer.sample_rate,
        "channels":    audio_capturer.channels,
        "audio_ok":    audio_capturer.available,
        "target_fps":  capturer.target_fps,
    }
    try:
        safe_send(CFG + json.dumps(cfg).encode())
        safe_send(CLP + clipboard_data.encode("utf-8"))
    except Exception:
        with _ws_lock:
            _ws_clients.pop(cid, None)
        return

    # ── VIDEO SENDER Task  ──
    def _video_sender():
        while cid in _ws_clients:
            try:
                frame = q.get(timeout=1.0)
                safe_send(VID + frame)
            except Empty:
                continue
            except Exception:
                break

    # ── AUDIO SENDER Task ──
    def _audio_sender():
        while cid in _ws_clients:
            try:
                chunks = []
                for _ in range(12):
                    c = audio_capturer.get_chunk()
                    if not c: break
                    chunks.append(c)
                if chunks:
                    safe_send(AUD + b"".join(chunks))
                time.sleep(0.02)
            except Exception:
                break

    vt = threading.Thread(target=_video_sender, daemon=True)
    at = threading.Thread(target=_audio_sender, daemon=True)
    vt.start()
    at.start()

    # ── RECEIVER Task (Main Thread for this WS) ──
    try:
        while True:
            msg = ws.receive()
            if not msg:
                break
            if isinstance(msg, bytes) and msg.startswith(CLP):
                text = msg[1:].decode("utf-8")
                if text != clipboard_data:
                    clipboard_data = text
                    clipboard_version += 1
                    _broadcast_clipboard(text, exclude_cid=cid)
            elif isinstance(msg, bytes) and msg.startswith(CTL):
                process_control_event(msg[1:])
    except Exception:
        pass
    finally:
        with _ws_lock:
            _ws_clients.pop(cid, None)

def _broadcast_clipboard(text: str, exclude_cid: int | None = None):
    """Internal WS helper to push a clipboard update to all connected clients."""
    msg = CLP + text.encode("utf-8")
    with _ws_lock:
        targets = [data["ws"] for tid, data in _ws_clients.items() if tid != exclude_cid]
    for target in targets:
        try:
            target.send(msg)
        except Exception:
            pass



# ── Tkinter Control Panel ─────────────────────────────────────────────

class ControlPanel(tk.Tk):
    C_BG      = "#0F0F1A"
    C_CARD    = "#1A1A2E"
    C_CARD2   = "#20203A"
    C_ACCENT  = "#7C3AED"
    C_ACCENT2 = "#A855F7"
    C_TEXT    = "#E2E8F0"
    C_MUTED   = "#64748B"
    C_GREEN   = "#10B981"
    C_AMBER   = "#F59E0B"
    C_RED     = "#EF4444"
    C_SEL     = "#2D2D55"

    def __init__(self):
        super().__init__()
        self.title("NetShare Hub — Control Panel")
        self.geometry("640x920")
        self.configure(bg=self.C_BG)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Set Window Icon
        icon_path = os.path.join(BASE_DIR, "static", "icon.png")
        if os.path.exists(icon_path):
            try:
                img = tk.PhotoImage(file=icon_path)
                self.iconphoto(True, img)
            except Exception:
                pass

        # Center on screen
        self.update_idletasks()
        sx, sy = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"640x920+{(sx-640)//2}+{(sy-920)//2}")

        self.server_url = f"http://{get_local_ip()}:{HTTP_PORT}"
        self._qr_photo = None
        self._region_sel = None

        self._build_ui()

        self._refresh_sources()
        self._stats_loop()

    # ------------------------------------------------------------------
    # UI build
    # ------------------------------------------------------------------

    def _build_ui(self):
        # ── Scrollable Wrapper ──
        self._canvas = tk.Canvas(self, bg=self.C_BG, highlightthickness=0, borderwidth=0)
        self._scrollbar = tk.Scrollbar(self, orient="vertical", command=self._canvas.yview, bg=self.C_CARD, troughcolor=self.C_BG)
        self._scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self._canvas.configure(yscrollcommand=self._scrollbar.set)

        self.container = tk.Frame(self._canvas, bg=self.C_BG)
        self._canvas_frame = self._canvas.create_window((0, 0), window=self.container, anchor="nw", width=560)
        
        # Update scrollregion when content changes
        self.container.bind("<Configure>", lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        
        # Mousewheel support
        def _on_mousewheel(event):
            self._canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        self._canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # ── Header ──
        hdr = tk.Frame(self.container, bg=self.C_BG)
        hdr.pack(fill="x", padx=24, pady=(22, 0))

        tk.Label(hdr, text="NetShare Hub",
                 font=("Segoe UI", 22, "bold"),
                 bg=self.C_BG, fg=self.C_TEXT).pack(side="left")

        self._badge = tk.Label(hdr, text="● LIVE",
                               font=("Segoe UI", 10, "bold"),
                               bg=self.C_GREEN, fg="white",
                               padx=10, pady=4)
        self._badge.pack(side="right")

        tk.Label(self.container, text="Screen Sharing  •  File Transfer  •  Clipboard Sync",
                 font=("Segoe UI", 10), bg=self.C_BG,
                 fg=self.C_MUTED).pack(anchor="w", padx=24, pady=(3, 14))

        # ── QR / URL card ──
        self._build_qr_card()

        # ── Stats row ──
        self._build_stats_row()

        # ── Source selector ──
        self._build_source_card()

        # ── Settings ──
        self._build_settings_card()

        # ── Public Access ──
        self._build_public_access_card()

        # ── Security & Connected Devices ──
        self._build_security_card()

        # ── Buttons ──
        self._build_controls()

        tk.Label(self.container,
                 text="All devices must be on the same Wi-Fi network",
                 font=("Segoe UI", 9), bg=self.C_BG,
                 fg=self.C_MUTED).pack(pady=(2, 24))


    def _card(self, h_pad=24, v_pad=6):
        f = tk.Frame(self.container, bg=self.C_CARD)
        f.pack(fill="x", padx=h_pad, pady=v_pad)
        return f


    # ── QR card ──
    def _build_qr_card(self):
        card  = self._card()
        inner = tk.Frame(card, bg=self.C_CARD, padx=18, pady=18)
        inner.pack(fill="x")

        qr_pil = make_qr_image(self.server_url, size=150)
        self._qr_photo = ImageTk.PhotoImage(qr_pil)
        self._qr_label = tk.Label(inner, image=self._qr_photo,
                                 bg=self.C_CARD, cursor="hand2")
        self._qr_label.pack(side="left", padx=(0, 18))
        self._qr_label.bind("<Button-1>", lambda _: self._copy_url())

        right = tk.Frame(inner, bg=self.C_CARD)
        right.pack(side="left", fill="both", expand=True)

        tk.Label(right, text="Scan to Connect",
                 font=("Segoe UI", 13, "bold"),
                 bg=self.C_CARD, fg=self.C_TEXT).pack(anchor="w")
        tk.Label(right, text="or open this URL in any browser:",
                 font=("Segoe UI", 9), bg=self.C_CARD,
                 fg=self.C_MUTED).pack(anchor="w", pady=(2, 8))

        url_f = tk.Frame(right, bg="#1E1E38", padx=10, pady=8)
        url_f.pack(fill="x")
        self._url_label = tk.Label(url_f, text=self.server_url,
                                  font=("Consolas", 13, "bold"),
                                  bg="#1E1E38", fg=self.C_ACCENT2, cursor="hand2")
        self._url_label.pack(anchor="w")
        self._url_label.bind("<Button-1>", lambda _: self._copy_url())

        tk.Button(right, text="Copy URL",
                  font=("Segoe UI", 9, "bold"),
                  bg=self.C_ACCENT, fg="white",
                  activebackground=self.C_ACCENT2, activeforeground="white",
                  bd=0, padx=14, pady=6, cursor="hand2",
                  command=self._copy_url).pack(anchor="w", pady=(8, 0))

    # ── Stats row ──
    def _build_stats_row(self):
        row = tk.Frame(self.container, bg=self.C_BG)
        row.pack(fill="x", padx=24, pady=6)
        self._lbl_fps     = self._stat_chip(row, "FPS",     "—",    self.C_GREEN)
        self._lbl_clients = self._stat_chip(row, "Clients", "0",    self.C_ACCENT2)
        self._lbl_quality = self._stat_chip(row, "Quality", "82%",  self.C_AMBER)
        self._lbl_source  = self._stat_chip(row, "Source",  "Monitor 1", self.C_TEXT)


    def _stat_chip(self, parent, label, value, color):
        c = tk.Frame(parent, bg=self.C_CARD, padx=12, pady=10)
        c.pack(side="left", fill="both", expand=True, padx=(0, 5))
        v = tk.Label(c, text=value, font=("Segoe UI", 17, "bold"),
                     bg=self.C_CARD, fg=color)
        v.pack()
        tk.Label(c, text=label, font=("Segoe UI", 8),
                 bg=self.C_CARD, fg=self.C_MUTED).pack()
        return v

    # ── Source selector ──
    def _build_source_card(self):
        card  = self._card()
        inner = tk.Frame(card, bg=self.C_CARD, padx=18, pady=14)
        inner.pack(fill="x")

        # Header row
        hrow = tk.Frame(inner, bg=self.C_CARD)
        hrow.pack(fill="x", pady=(0, 8))
        tk.Label(hrow, text="Share Source", font=("Segoe UI", 12, "bold"),
                 bg=self.C_CARD, fg=self.C_TEXT).pack(side="left")
        tk.Button(hrow, text="Refresh",
                  font=("Segoe UI", 8, "bold"),
                  bg=self.C_CARD2, fg=self.C_MUTED,
                  activebackground=self.C_SEL, activeforeground=self.C_TEXT,
                  bd=0, padx=10, pady=4, cursor="hand2",
                  command=self._refresh_sources).pack(side="right")

        # Listbox with scrollbar
        lb_frame = tk.Frame(inner, bg=self.C_CARD)
        lb_frame.pack(fill="x")

        scrollbar = tk.Scrollbar(lb_frame, orient="vertical",
                                 bg=self.C_CARD2, troughcolor=self.C_CARD)
        self._src_listbox = tk.Listbox(
            lb_frame,
            bg=self.C_CARD2, fg=self.C_TEXT,
            selectbackground=self.C_ACCENT, selectforeground="white",
            font=("Segoe UI", 10),
            bd=0, highlightthickness=0,
            activestyle="none",
            height=6,
            yscrollcommand=scrollbar.set,
        )
        scrollbar.config(command=self._src_listbox.yview)
        self._src_listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._src_listbox.bind("<<ListboxSelect>>", self._on_source_select)

        # Selected source label
        self._src_desc = tk.Label(inner, text="",
                                  font=("Segoe UI", 9), bg=self.C_CARD,
                                  fg=self.C_MUTED, anchor="w")
        self._src_desc.pack(fill="x", pady=(6, 0))

        # Region picker button
        tk.Button(inner, text="Select Custom Region",
                  font=("Segoe UI", 9),
                  bg=self.C_CARD2, fg=self.C_TEXT,
                  activebackground=self.C_ACCENT, activeforeground="white",
                  bd=0, padx=12, pady=7, cursor="hand2",
                  command=self._pick_region).pack(anchor="w", pady=(8, 0))

        # Store source data for listbox items
        self._src_data: list[dict] = []

    # ── Settings ──
    def _build_settings_card(self):
        card  = self._card()
        inner = tk.Frame(card, bg=self.C_CARD, padx=18, pady=14)
        inner.pack(fill="x")

        tk.Label(inner, text="Performance",
                 font=("Segoe UI", 12, "bold"),
                 bg=self.C_CARD, fg=self.C_TEXT).pack(anchor="w", pady=(0, 10))

        self._fps_var     = tk.IntVar(value=30)
        self._quality_var = tk.IntVar(value=70)
        self._width_var   = tk.StringVar(value="1280")

        self._slider(inner, "Target FPS",    self._fps_var,     5, 60,
                     lambda v: capturer.set_fps(int(float(v))))
        self._slider(inner, "JPEG Quality",  self._quality_var, 30, 100,
                     lambda v: capturer.set_quality(int(float(v))))

        # Resolution row
        res_row = tk.Frame(inner, bg=self.C_CARD)
        res_row.pack(fill="x", pady=(8, 0))
        tk.Label(res_row, text="Stream Width",
                 font=("Segoe UI", 10),
                 bg=self.C_CARD, fg=self.C_TEXT,
                 width=14, anchor="w").pack(side="left")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox",
                        fieldbackground="#1E1E38",
                        background=self.C_CARD2,
                        foreground=self.C_TEXT,
                        selectbackground=self.C_ACCENT)

        combo = ttk.Combobox(res_row, textvariable=self._width_var,
                             values=["Native", "1920", "1280", "960", "640"],
                             state="readonly", width=10,
                             font=("Segoe UI", 10))
        combo.pack(side="left", padx=8)
        combo.bind("<<ComboboxSelected>>", self._on_width_change)

        # ── Audio row ──
        audio_row = tk.Frame(inner, bg=self.C_CARD)
        audio_row.pack(fill="x", pady=(12, 0))

        info = audio_capturer.get_info()
        audio_status = audio_capturer.device_name[:32] if audio_capturer.available else "Not found"
        self._audio_status_lbl = tk.Label(
            audio_row,
            text=f"Audio: {audio_status}",
            font=("Segoe UI", 9), bg=self.C_CARD,
            fg=self.C_GREEN if audio_capturer.available else self.C_RED,
            anchor="w"
        )
        self._audio_status_lbl.pack(side="left", fill="x", expand=True)

        self._audio_btn = tk.Button(
            audio_row, text="Mute Audio",
            font=("Segoe UI", 9, "bold"),
            bg=self.C_CARD2, fg=self.C_TEXT,
            activebackground=self.C_SEL, activeforeground=self.C_TEXT,
            bd=0, padx=12, pady=5, cursor="hand2",
            command=self._toggle_audio
        )
        self._audio_btn.pack(side="right")
 
    # ── Public Access ──
    def _build_public_access_card(self):
        card  = self._card()
        inner = tk.Frame(card, bg=self.C_CARD, padx=18, pady=14)
        inner.pack(fill="x")
 
        tk.Label(inner, text="Public Access (Ngrok)",
                 font=("Segoe UI", 12, "bold"),
                 bg=self.C_CARD, fg=self.C_TEXT).pack(anchor="w", pady=(0, 4))
 
        tk.Label(inner, text="Expose your hub to the internet so others can connect remotely.",
                 font=("Segoe UI", 9), bg=self.C_CARD,
                 fg=self.C_MUTED).pack(anchor="w", pady=(0, 10))
 
        self._token_var = tk.StringVar()
        
        # Load saved token
        try:
            conf_path = os.path.join(BASE_DIR, "netshare_config.json")
            if os.path.exists(conf_path):
                with open(conf_path, "r") as f:
                    data = __import__("json").load(f)
                    if "ngrok_token" in data:
                        self._token_var.set(data["ngrok_token"])
        except Exception:
            pass
        
        entry_row = tk.Frame(inner, bg=self.C_CARD)
        entry_row.pack(fill="x", pady=4)
        
        tk.Label(entry_row, text="Authtoken:", font=("Segoe UI", 10),
                 bg=self.C_CARD, fg=self.C_TEXT, width=10, anchor="w").pack(side="left")
                 
        self._token_entry = tk.Entry(entry_row, textvariable=self._token_var,
                                     bg="#1E1E38", fg=self.C_TEXT, bd=0,
                                     insertbackground="white", font=("Consolas", 10),
                                     show="*", highlightthickness=1, highlightbackground=self.C_CARD2)
        self._token_entry.pack(side="left", fill="x", expand=True, padx=8)
        
        self._public_btn = tk.Button(inner, text="Go Public",
                                    font=("Segoe UI", 10, "bold"),
                                    bg=self.C_ACCENT, fg="white",
                                    activebackground=self.C_ACCENT2, activeforeground="white",
                                    bd=0, padx=18, pady=8, cursor="hand2",
                                    command=self._on_go_public)
        self._public_btn.pack(anchor="w", pady=(10, 0))
        
        self._public_status_lbl = tk.Label(inner, text="Status: Local Network Only",
                                         font=("Segoe UI", 9, "bold"), bg=self.C_CARD,
                                         fg=self.C_MUTED)
        self._public_status_lbl.pack(anchor="w", pady=(8, 0))
 
    def _on_go_public(self):
        global PUBLIC_HTTP_URL
        if PUBLIC_HTTP_URL:
            self._stop_public()
            return
            
        token = self._token_var.get().strip()
        if not token:
            messagebox.showwarning("Authtoken Required", "Please enter your Ngrok authtoken first.")
            return
            
        self._public_btn.config(text="Starting...", state="disabled")
        self._public_status_lbl.config(text="Status: Connecting to Ngrok...", fg=self.C_AMBER)
        
        def run_thread():
            success, result = start_public_access(token)
            # Schedule UI update on main thread
            self.after(0, lambda: self._finish_go_public(success, result))
            
        threading.Thread(target=run_thread, daemon=True).start()

    def _finish_go_public(self, success, result):
        self._public_btn.config(state="normal", text="Stop Public" if success else "Go Public")
        if success:
            self.server_url = result
            self._refresh_qr()
            self._public_btn.config(bg=self.C_RED)
            self._public_status_lbl.config(text=f"Status: PUBLIC ({result})", fg=self.C_GREEN)
            
            # Save token securely when successful
            try:
                conf_path = os.path.join(BASE_DIR, "netshare_config.json")
                conf_data = {}
                if os.path.exists(conf_path):
                    with open(conf_path, "r") as f:
                        conf_data = __import__("json").load(f)
                conf_data["ngrok_token"] = self._token_var.get().strip()
                with open(conf_path, "w") as f:
                    __import__("json").dump(conf_data, f)
            except Exception:
                pass
            
            messagebox.showinfo("Success", f"Server is now PUBLIC!\n\nURL: {result}")
        else:
            messagebox.showerror("Ngrok Error", result)
            self._public_btn.config(text="Go Public", bg=self.C_ACCENT)
            self._public_status_lbl.config(text="Status: Connection Failed", fg=self.C_RED)


    def _stop_public(self):
        global PUBLIC_HTTP_URL, PUBLIC_WS_URL
        try:
            from pyngrok import ngrok
            ngrok.kill()  # Complete shutdown of ngrok
            PUBLIC_HTTP_URL = None
            PUBLIC_WS_URL = None
            self._public_btn.config(text="Go Public", bg=self.C_ACCENT)
            self._public_status_lbl.config(text="Status: Local Network Only", fg=self.C_MUTED)
            self.server_url = f"http://{get_local_ip()}:{HTTP_PORT}"
            self._refresh_qr()
            show_info = messagebox.showinfo if hasattr(messagebox, "showinfo") else print
            show_info("Public Access Stopped", "The server is now only accessible via your local network.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to stop Ngrok: {e}")
 
    def _refresh_qr(self):
        # Update the QR code displayed in the UI
        qr_pil = make_qr_image(self.server_url, size=150)
        self._qr_photo = ImageTk.PhotoImage(qr_pil)
        if hasattr(self, '_qr_label'):
            self._qr_label.config(image=self._qr_photo)
        
        # Also update the URL text in the card
        if hasattr(self, '_url_label'):
            self._url_label.config(text=self.server_url)
        
    # ── Security & Device Management ──
    def _build_security_card(self):
        card  = self._card()
        inner = tk.Frame(card, bg=self.C_CARD, padx=18, pady=14)
        inner.pack(fill="x")

        tk.Label(inner, text="Security & Device Management",
                 font=("Segoe UI", 12, "bold"),
                 bg=self.C_CARD, fg=self.C_TEXT).pack(anchor="w", pady=(0, 4))

        tk.Label(inner, text="Set Access PIN to lock stream & manage connected devices.",
                 font=("Segoe UI", 9), bg=self.C_CARD,
                 fg=self.C_MUTED).pack(anchor="w", pady=(0, 10))

        # PIN Row
        pin_row = tk.Frame(inner, bg=self.C_CARD)
        pin_row.pack(fill="x", pady=4)

        tk.Label(pin_row, text="Access PIN:", font=("Segoe UI", 10),
                 bg=self.C_CARD, fg=self.C_TEXT, width=10, anchor="w").pack(side="left")

        self._pin_var = tk.StringVar(value=SERVER_PIN)
        self._pin_entry = tk.Entry(pin_row, textvariable=self._pin_var,
                                   bg="#1E1E38", fg=self.C_TEXT, bd=0,
                                   insertbackground="white", font=("Consolas", 11, "bold"),
                                   show="*", width=12, highlightthickness=1, highlightbackground=self.C_CARD2)
        self._pin_entry.pack(side="left", padx=8)

        tk.Button(pin_row, text="Save PIN",
                  font=("Segoe UI", 9, "bold"), bg=self.C_ACCENT, fg="white",
                  activebackground=self.C_ACCENT2, activeforeground="white",
                  bd=0, padx=12, pady=4, cursor="hand2",
                  command=self._save_pin).pack(side="left", padx=4)

        tk.Button(pin_row, text="Clear",
                  font=("Segoe UI", 9), bg=self.C_CARD2, fg=self.C_MUTED,
                  activebackground=self.C_SEL, activeforeground=self.C_TEXT,
                  bd=0, padx=10, pady=4, cursor="hand2",
                  command=self._remove_pin).pack(side="left")

        self._pin_status_lbl = tk.Label(inner,
                                         text=f"PIN Status: {'PROTECTED (' + SERVER_PIN + ')' if SERVER_PIN else 'Open Access (No PIN)'}",
                                         font=("Segoe UI", 9, "bold"), bg=self.C_CARD,
                                         fg=self.C_GREEN if SERVER_PIN else self.C_AMBER)
        self._pin_status_lbl.pack(anchor="w", pady=(6, 12))

        # Connected Devices Header
        dev_hdr = tk.Frame(inner, bg=self.C_CARD)
        dev_hdr.pack(fill="x", pady=(8, 4))
        tk.Label(dev_hdr, text="Connected / Recent Devices", font=("Segoe UI", 10, "bold"),
                 bg=self.C_CARD, fg=self.C_TEXT).pack(side="left")
        tk.Button(dev_hdr, text="Refresh Devices",
                  font=("Segoe UI", 8, "bold"), bg=self.C_CARD2, fg=self.C_MUTED,
                  activebackground=self.C_SEL, activeforeground=self.C_TEXT,
                  bd=0, padx=8, pady=3, cursor="hand2",
                  command=self._refresh_connected_devices).pack(side="right")

        # Devices listbox
        dev_lb_frame = tk.Frame(inner, bg=self.C_CARD)
        dev_lb_frame.pack(fill="x", pady=(2, 6))

        dev_scrollbar = tk.Scrollbar(dev_lb_frame, orient="vertical", bg=self.C_CARD2, troughcolor=self.C_CARD)
        self._dev_listbox = tk.Listbox(
            dev_lb_frame, bg=self.C_CARD2, fg=self.C_TEXT,
            selectbackground=self.C_ACCENT, selectforeground="white",
            font=("Consolas", 9), bd=0, highlightthickness=0, height=4,
            yscrollcommand=dev_scrollbar.set
        )
        dev_scrollbar.config(command=self._dev_listbox.yview)
        self._dev_listbox.pack(side="left", fill="both", expand=True)
        dev_scrollbar.pack(side="right", fill="y")

        # Action row for devices
        dev_act_row = tk.Frame(inner, bg=self.C_CARD)
        dev_act_row.pack(fill="x", pady=(4, 0))

        tk.Button(dev_act_row, text="Block / Unblock Selected IP",
                  font=("Segoe UI", 9, "bold"), bg=self.C_RED, fg="white",
                  activebackground="#DC2626", activeforeground="white",
                  bd=0, padx=12, pady=5, cursor="hand2",
                  command=self._toggle_block_ip).pack(side="left")

        self._dev_data = []
        self._refresh_connected_devices()

    def _save_pin(self):
        global SERVER_PIN
        new_pin = self._pin_var.get().strip()
        SERVER_PIN = new_pin
        if new_pin:
            self._pin_status_lbl.config(text=f"PIN Status: PROTECTED ({new_pin})", fg=self.C_GREEN)
            messagebox.showinfo("PIN Saved", f"Access PIN set to '{new_pin}'. Clients must enter this PIN to connect.")
        else:
            self._pin_status_lbl.config(text="PIN Status: Open Access (No PIN)", fg=self.C_AMBER)
            messagebox.showinfo("PIN Removed", "Access PIN removed. Open access enabled.")

    def _remove_pin(self):
        self._pin_var.set("")
        self._save_pin()

    def _refresh_connected_devices(self):
        self._dev_data.clear()
        self._dev_listbox.delete(0, tk.END)
        with _device_lock:
            now = time.time()
            devs = list(_connected_devices.values())
            devs.sort(key=lambda x: x["last_seen"], reverse=True)
            for d in devs:
                ip = d["ip"]
                dt = d["device_type"]
                blocked = " [BLOCKED]" if d.get("is_blocked") else ""
                status = "Active" if (now - d["last_seen"]) < 60 else "Idle"
                lbl = f"{dt} | {ip:<15} | {status:<6}{blocked}"
                self._dev_listbox.insert(tk.END, lbl)
                self._dev_data.append(d)
        if not self._dev_data:
            self._dev_listbox.insert(tk.END, "  No devices recorded yet.")

    def _toggle_block_ip(self):
        sel = self._dev_listbox.curselection()
        if not sel or not self._dev_data:
            messagebox.showwarning("No Device Selected", "Please select a device from the list.")
            return
        idx = sel[0]
        if idx >= len(self._dev_data):
            return
        dev = self._dev_data[idx]
        ip = dev["ip"]
        is_blocked = ip in _blocked_ips
        if is_blocked:
            _blocked_ips.discard(ip)
            with _device_lock:
                if ip in _connected_devices:
                    _connected_devices[ip]["is_blocked"] = False
            messagebox.showinfo("Unblocked", f"IP {ip} has been unblocked.")
        else:
            if messagebox.askyesno("Confirm Block", f"Are you sure you want to block IP {ip}?\nThey will be disconnected and denied access."):
                _blocked_ips.add(ip)
                with _device_lock:
                    if ip in _connected_devices:
                        _connected_devices[ip]["is_blocked"] = True
                messagebox.showinfo("Blocked", f"IP {ip} has been blocked.")
        self._refresh_connected_devices()

    def _slider(self, parent, label, var, lo, hi, cmd):
        row = tk.Frame(parent, bg=self.C_CARD)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, font=("Segoe UI", 10),
                 bg=self.C_CARD, fg=self.C_TEXT,
                 width=14, anchor="w").pack(side="left")
        style = ttk.Style()
        style.configure("Horizontal.TScale",
                        background=self.C_CARD,
                        troughcolor="#1E1E38",
                        sliderthickness=18)
        ttk.Scale(row, from_=lo, to=hi, orient="horizontal",
                  variable=var, command=cmd
                  ).pack(side="left", fill="x", expand=True, padx=8)
        tk.Label(row, textvariable=var,
                 font=("Consolas", 10, "bold"),
                 bg=self.C_CARD, fg=self.C_ACCENT2, width=4
                 ).pack(side="right")

    # ── Control buttons ──
    def _build_controls(self):
        row = tk.Frame(self.container, bg=self.C_BG)
        row.pack(fill="x", padx=24, pady=8)


        self._pause_btn = tk.Button(
            row, text="Pause Stream",
            font=("Segoe UI", 10, "bold"),
            bg=self.C_AMBER, fg="white",
            activebackground="#D97706", activeforeground="white",
            bd=0, padx=18, pady=10, cursor="hand2",
            command=self._toggle_stream)
        self._pause_btn.pack(side="left", padx=(0, 8))

        # Remote Control Toggle Checkbox
        self._rc_var = tk.BooleanVar(value=remote_control_on)
        self._rc_chk = tk.Checkbutton(
            row, text="Allow Remote Control",
            variable=self._rc_var,
            font=("Segoe UI", 10),
            bg=self.C_BG, fg=self.C_TEXT,
            selectcolor="#1E1E38", activebackground=self.C_BG,
            activeforeground=self.C_TEXT,
            command=self._on_toggle_rc)
        self._rc_chk.pack(side="left", padx=12)


        tk.Button(row, text="Open in Browser",
                  font=("Segoe UI", 10, "bold"),
                  bg=self.C_CARD, fg=self.C_TEXT,
                  activebackground="#252550", activeforeground="white",
                  bd=0, padx=18, pady=10, cursor="hand2",
                  command=lambda: __import__("webbrowser").open(self.server_url)
                  ).pack(side="left")

        tk.Button(row, text="Exit",
                  font=("Segoe UI", 10, "bold"),
                  bg=self.C_RED, fg="white",
                  activebackground="#DC2626", activeforeground="white",
                  bd=0, padx=18, pady=10, cursor="hand2",
                  command=self._on_close).pack(side="right")

    # ------------------------------------------------------------------
    # Source management
    # ------------------------------------------------------------------

    def _refresh_sources(self):
        monitors = list_monitors()
        windows  = list_windows()

        self._src_data.clear()
        self._src_listbox.delete(0, tk.END)

        # ── Monitors ──
        self._src_listbox.insert(tk.END, "  -- Monitors --")
        self._src_listbox.itemconfig(tk.END, fg=self.C_MUTED,
                                     selectbackground=self.C_CARD2,
                                     selectforeground=self.C_MUTED)
        self._src_data.append({"_header": True})

        for m in monitors:
            label = f"    Monitor {m['index']}  ({m['width']}x{m['height']})"
            self._src_listbox.insert(tk.END, label)
            self._src_data.append({"type": "monitor", "index": m["index"],
                                    "name": f"Monitor {m['index']}"})

        # ── Windows ──
        self._src_listbox.insert(tk.END, "  -- Application Windows --")
        self._src_listbox.itemconfig(tk.END, fg=self.C_MUTED,
                                     selectbackground=self.C_CARD2,
                                     selectforeground=self.C_MUTED)
        self._src_data.append({"_header": True})

        for w in windows:
            label = f"    {w['title'][:46]}  ({w['width']}x{w['height']})"
            self._src_listbox.insert(tk.END, label)
            self._src_data.append({"type": "window", "hwnd": w["hwnd"],
                                    "title": w["title"]})

        if not windows:
            self._src_listbox.insert(tk.END, "    (no windows found)")
            self._src_data.append({"_header": True})

        # Select current source
        self._src_listbox.selection_set(1)  # default: Monitor 1

    def _on_source_select(self, _):
        sel = self._src_listbox.curselection()
        if not sel:
            return
        idx  = sel[0]
        data = self._src_data[idx] if idx < len(self._src_data) else {}
        if data.get("_header"):
            return

        if data["type"] == "monitor":
            capturer.set_source_monitor(data["index"])
            self._src_desc.config(text=f"Capturing: {data['name']}")
            self._lbl_source.config(text=data["name"])
        elif data["type"] == "window":
            capturer.set_source_window(data["hwnd"], data["title"])
            title = data["title"][:28]
            self._src_desc.config(text=f"Capturing window: {title}")
            self._lbl_source.config(text=title[:14])

    def _pick_region(self):
        self.withdraw()
        self.after(200, lambda: RegionSelector(self._on_region_picked))

    def _on_region_picked(self, x, y, w, h):
        self.deiconify()
        if w > 20 and h > 20:
            capturer.set_source_region(x, y, w, h)
            desc = f"Region  {w}x{h} at ({x},{y})"
            self._src_desc.config(text=f"Capturing: {desc}")
            self._lbl_source.config(text=f"{w}x{h}")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _copy_url(self):
        self.clipboard_clear()
        self.clipboard_append(self.server_url)
        self._badge.config(text="Copied!", bg="#0EA5E9")
        self.after(1800, lambda: self._badge.config(text="● LIVE", bg=self.C_GREEN))

    def _toggle_stream(self):
        if capturer.is_running:
            capturer.stop()
            self._pause_btn.config(text="Resume Stream", bg=self.C_GREEN)
            self._badge.config(text="PAUSED", bg=self.C_AMBER)
        else:
            capturer.start()
            self._pause_btn.config(text="Pause Stream", bg=self.C_AMBER)
            self._badge.config(text="● LIVE", bg=self.C_GREEN)

    def _on_toggle_rc(self):
        global remote_control_on
        remote_control_on = self._rc_var.get()

    def _on_close(self):

        capturer.stop()
        audio_capturer.stop()
        self.destroy()
        os._exit(0)

    def _on_width_change(self, _):
        val = self._width_var.get()
        capturer.set_max_width(0 if val == "Native" else int(val))

    def _toggle_audio(self):
        audio_capturer.enabled = not audio_capturer.enabled
        if audio_capturer.enabled:
            self._audio_btn.config(text="Mute Audio",   bg=self.C_CARD2)
        else:
            self._audio_btn.config(text="Unmute Audio", bg=self.C_AMBER)

    # ------------------------------------------------------------------
    # Stats polling loop
    # ------------------------------------------------------------------

    def _stats_loop(self):
        stats = capturer.get_stats()
        self._lbl_fps.config(text=f"{stats['fps']:.0f}")
        self._lbl_clients.config(text=str(len(_ws_clients)))
        self._lbl_quality.config(text=f"{stats['quality']}%")
        self.after(400, self._stats_loop)


# ── Region Selector overlay ───────────────────────────────────────────

class RegionSelector(tk.Toplevel):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.sx = self.sy = 0
        self.rect = None

        self.attributes("-fullscreen", True)
        self.attributes("-alpha", 0.28)
        self.configure(bg="#000011")
        self.attributes("-topmost", True)

        self.cv = tk.Canvas(self, cursor="crosshair",
                            bg="#000011", highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        self.cv.create_text(
            self.winfo_screenwidth() // 2, 44,
            text="Click and drag to select region  |  Esc to cancel",
            fill="white", font=("Segoe UI", 15, "bold"),
        )
        self.cv.bind("<ButtonPress-1>",   self._press)
        self.cv.bind("<B1-Motion>",       self._drag)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.bind("<Escape>", lambda _: self._cancel())

    def _press(self, e):
        self.sx, self.sy = e.x, e.y
        if self.rect: self.cv.delete(self.rect)

    def _drag(self, e):
        if self.rect: self.cv.delete(self.rect)
        self.rect = self.cv.create_rectangle(
            self.sx, self.sy, e.x, e.y,
            outline="#7C3AED", width=3, fill="#3B1F6E"
        )

    def _release(self, e):
        x = min(self.sx, e.x);  y = min(self.sy, e.y)
        w = abs(e.x - self.sx); h = abs(e.y - self.sy)
        self.destroy()
        self.callback(x, y, w, h)

    def _cancel(self):
        self.destroy()
        self.callback(0, 0, 0, 0)


# ── Entry point ───────────────────────────────────────────────────────

def is_port_free(port):
    """Check if a port is available on localhost."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False

def main():

    # Fix Windows console encoding
    if sys.stdout and hasattr(sys.stdout, "buffer"):
        try:
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace"
            )
        except Exception:
            pass

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--public", action="store_true", help="Start with public access immediately")
    parser.add_argument("--token", help="Ngrok authtoken")
    args = parser.parse_args()

    if args.public and args.token:
        start_public_access(args.token)

    local_ip = get_local_ip()
    print("=" * 54)
    print("   NetShare Hub — Screen + Files + Clipboard over Wi-Fi")
    print("=" * 54)
    print(f"  Stream URL : http://{local_ip}:{HTTP_PORT}")
    print(f"  WebSockets : ws://{local_ip}:{HTTP_PORT}/ws/stream & /ws/chat")
    print(f"  Localhost  : http://localhost:{HTTP_PORT}")
    print("=" * 54)

    # Pre-flight port check
    if not is_port_free(HTTP_PORT):
        print(f"❌ ERROR: Port {HTTP_PORT} is already in use!")
        print(f"   Please close any other application using port {HTTP_PORT} and try again.")
        sys.exit(1)


    # Start capture
    capturer.start()

    # Start audio capture
    audio_capturer.start()

    # Start Flask HTTP server with integrated WebSockets via Werkzeug
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR) # Suppress normal HTTP requests from flooding terminal
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True, use_reloader=False),
        daemon=True, name="FlaskThread"
    ).start()


    time.sleep(0.6)

    # Launch control panel on main thread
    ControlPanel().mainloop()


if __name__ == "__main__":
    main()

