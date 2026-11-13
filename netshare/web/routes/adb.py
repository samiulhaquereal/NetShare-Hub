"""Android (ADB / scrcpy) and iOS (AirPlay via UxPlay) mirroring hub."""

from __future__ import annotations

import sys
import threading
import time

from flask import Blueprint, Response, jsonify, make_response, request, send_from_directory

from ...infrastructure.capture import get_window_rect, grab_thumbnail
from ..context import host_only, is_host


def _no_cache(resp: Response, mimetype: str) -> Response:
    resp.headers["Content-Type"] = mimetype
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


def create_blueprint(c, _sock) -> Blueprint:
    bp = Blueprint("adb", __name__)
    adb, screens = c.adb, c.adb_screens
    static_dir = str(c.settings.static_dir)

    # ── Android ───────────────────────────────────────────────────────
    @bp.route("/api/adb/devices")
    def devices():
        try:
            devs = adb.devices()
            return jsonify({"status": "ok", "adb_path": adb.path, "devices": devs, "count": len(devs)})
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc), "devices": []})

    @bp.route("/api/adb/pair", methods=["POST"])
    def pair():
        data = request.get_json(silent=True) or {}
        ip, code = str(data.get("ip", "")).strip(), str(data.get("code", "")).strip()
        if not ip or not code:
            return jsonify({"status": "error", "error": "Both Pairing IP:Port and 6-digit Code are required."}), 400
        try:
            ok, out = adb.pair(ip, code)
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 500
        if ok:
            return jsonify({"status": "ok", "result": out,
                            "message": f"Successfully paired with {ip}! Now connect using the Connection IP & Port."})
        return jsonify({"status": "error", "error": out or
                        "Pairing failed. Make sure device and PC are on the same Wi-Fi and code is fresh."}), 400

    @bp.route("/api/adb/connect-wifi", methods=["POST"])
    def connect_wifi():
        ip = str((request.get_json(silent=True) or {}).get("ip", "")).strip()
        if not ip:
            return jsonify({"status": "error", "error": "Missing IP/Port"}), 400
        try:
            ok, out = adb.connect(ip)
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 500
        if ok:
            return jsonify({"status": "ok", "message": f"Connected to {ip}", "result": out})
        return jsonify({"status": "error", "error": f"Failed to connect: {out}\n\nTip: On Android 11+, make sure to "
                        "PAIR first using the 'Pairing Code' & 'Pairing Port', then enter the main "
                        "'Wireless Debugging' IP:Port here."}), 400

    @bp.route("/api/adb/screencap")
    def screencap():
        frame = screens.frame(request.args.get("serial", "").strip())
        if not frame:
            return jsonify({"error": "Failed to capture screen"}), 500
        return _no_cache(make_response(frame), "image/png")

    @bp.route("/api/adb/stream/<path:serial>")
    def mjpeg_stream(serial):
        """multipart stream sharing the same cached worker as /screencap."""
        def generate():
            last = None
            while True:
                frame = screens.frame(serial)
                if frame and frame is not last:
                    last = frame
                    yield b"--frame\r\nContent-Type: image/png\r\n\r\n" + frame + b"\r\n"
                else:
                    time.sleep(0.05)
        return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")

    @bp.route("/api/adb/launch-scrcpy", methods=["POST"])
    def launch_scrcpy():
        serial = str((request.get_json(silent=True) or {}).get("serial", "")).strip()
        try:
            c.scrcpy.launch(serial or None)
        except FileNotFoundError:
            hint = ("Failed to auto-download scrcpy on PC. Please check internet connection or manually "
                    "extract scrcpy.exe to C:\\scrcpy\\" if sys.platform == "win32" else
                    "scrcpy is not installed on the host. Install it with: sudo apt install scrcpy")
            return jsonify({"status": "error", "error": hint}), 500
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 500
        return jsonify({"status": "ok", "message": f"Launched Native 60-120 FPS scrcpy Mirror for {serial}!"})

    # ── iOS AirPlay ───────────────────────────────────────────────────
    @bp.route("/api/airplay/status")
    def airplay_status():
        win = c.airplay.mirror_window()
        return jsonify({**c.airplay.status(), "streaming": win is not None, "can_manage": is_host(),
                        "title": win["title"] if win else "NetShare Hub (iOS AirPlay)",
                        "hwnd": win["hwnd"] if win else None})

    @bp.route("/api/airplay/install", methods=["POST"])
    @host_only
    def airplay_install():
        """Fetch the pinned receiver build in the background, then (re)start it."""
        if not c.airplay.status()["can_download"]:
            return jsonify({"status": "error", "error": "Install UxPlay with: sudo apt install uxplay avahi-daemon"}), 400
        if c.airplay.state == "downloading":
            return jsonify({"status": "ok", "message": "Download already in progress"})

        def run():
            try:
                c.airplay.install()
            except Exception as exc:
                print(f"⚠️ [AirPlay] {exc}")
                return
            c.airplay.restart_async()
        threading.Thread(target=run, daemon=True, name="AirPlayInstall").start()
        return jsonify({"status": "ok", "message": "Downloading AirPlay receiver…"})

    @bp.route("/api/airplay/setup-bonjour", methods=["POST"])
    @host_only
    def airplay_setup_bonjour():
        if c.airplay.setup_bonjour():
            c.airplay.restart_async()
            return jsonify({"status": "ok", "message": "Bonjour service is running"})
        return jsonify({"status": "error", "error": "Bonjour setup was cancelled or failed (admin approval is needed once)."}), 400

    @bp.route("/api/airplay/screencap")
    def airplay_screencap():
        win = c.airplay.mirror_window()
        r = get_window_rect(win["hwnd"]) if win else None
        if r:
            x, y, w, h = r
            data = grab_thumbnail(region={"top": y, "left": x, "width": min(w, 1920), "height": min(h, 1080)})
            if data:
                return _no_cache(make_response(data), "image/jpeg")
        return send_from_directory(static_dir, "icons/icon-192.png")

    @bp.route("/api/airplay/watch", methods=["POST"])
    def airplay_watch():
        win = c.airplay.mirror_window()
        if not win:
            return jsonify({"status": "error", "error": "No active AirPlay stream window found"}), 404
        c.capture.set_source_window(win["hwnd"], win["title"])
        return jsonify({"status": "ok", "message": f"Streaming AirPlay window '{win['title']}'"})

    return bp
