"""Screen streaming: /ws/stream socket, capture source/settings, thumbnails,
QR code and server info."""

from __future__ import annotations

import io
import json
import threading
import time

from flask import Blueprint, Response, jsonify, request

from ...domain.protocol import CFG, CLP, CTL
from ...infrastructure.capture import get_window_rect, grab_thumbnail, list_monitors, list_windows
from ...infrastructure.network import get_local_ip, make_qr_image
from ...services.stream_hub import StreamClient


def _int(data: dict, key: str, default: int) -> int:
    try:
        return int(data.get(key, default))
    except (TypeError, ValueError):
        return default


def _jpeg(data: bytes) -> Response:
    resp = Response(data, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


def create_blueprint(c, sock) -> Blueprint:
    bp = Blueprint("stream", __name__)
    cap, audio, hub = c.capture, c.audio, c.stream_hub

    @sock.route("/ws/stream", bp=bp)
    def ws_stream(ws):
        client = StreamClient(ws)
        cfg = {"sample_rate": audio.sample_rate, "channels": audio.channels,
               "audio_ok": audio.available, "target_fps": cap.target_fps, "paused": not cap.is_running}
        if not (client.send(CFG + json.dumps(cfg).encode()) and
                client.send(CLP + c.clipboard.text.encode("utf-8"))):
            return
        hub.add(client)
        latest = cap.get_latest_frame()
        if latest:
            client.offer_frame(latest)  # show something immediately
        cap.request_frame()
        sender = threading.Thread(target=client.run_sender, daemon=True, name="ws-stream-sender")
        sender.start()
        try:
            while not client.closed:
                msg = ws.receive()
                if msg is None:
                    break
                if isinstance(msg, (bytes, bytearray)):
                    if msg.startswith(CLP):
                        c.clipboard.update(bytes(msg[1:]).decode("utf-8", "replace"), origin=client)
                    elif msg.startswith(CTL):
                        c.remote.handle(bytes(msg[1:]))
        finally:
            hub.remove(client)

    @bp.route("/sources")
    def sources():
        return jsonify({"monitors": list_monitors(), "windows": list_windows(), "current": cap.get_source_info()})

    @bp.route("/source", methods=["POST"])
    def set_source():
        data = request.get_json(silent=True) or {}
        kind = data.get("type", "monitor")
        if kind == "monitor":
            cap.set_source_monitor(_int(data, "index", 1))
        elif kind == "region":
            cap.set_source_region(_int(data, "x", 0), _int(data, "y", 0), _int(data, "w", 800), _int(data, "h", 600))
        elif kind == "window":
            cap.set_source_window(_int(data, "hwnd", 0), str(data.get("title", "")))
        return jsonify({"ok": True, "source": cap.get_source_info()})

    @bp.route("/settings", methods=["POST"])
    def settings():
        data = request.get_json(silent=True) or {}
        if "fps" in data:
            cap.set_fps(_int(data, "fps", cap.target_fps))
        if "quality" in data:
            cap.set_quality(_int(data, "quality", cap.jpeg_quality))
        if "width" in data:
            cap.set_max_width(_int(data, "width", cap.max_width))
        return jsonify(cap.get_stats())

    @bp.route("/audio", methods=["POST"])
    def audio_toggle():
        data = request.get_json(silent=True) or {}
        if "enabled" in data:
            audio.enabled = bool(data["enabled"])
        return jsonify(audio.get_info())

    @bp.route("/thumbnail/monitor/<int:index>")
    def thumb_monitor(index):
        return _jpeg(grab_thumbnail(monitor_index=index))

    @bp.route("/thumbnail/window/<int:hwnd>")
    def thumb_window(hwnd):
        r = get_window_rect(hwnd)
        if not r:
            return _jpeg(b"")
        x, y, w, h = r
        return _jpeg(grab_thumbnail(region={"top": y, "left": x, "width": min(w, 1920), "height": min(h, 1080)}))

    @bp.route("/qr.png")
    def qr():
        url = c.tunnel.public_url or f"http://{get_local_ip()}:{c.settings.http_port}"
        buf = io.BytesIO()
        make_qr_image(url, size=300).save(buf, format="PNG")
        return Response(buf.getvalue(), mimetype="image/png")

    @bp.route("/info")
    def info():
        port = c.settings.http_port
        stats = cap.get_stats()
        stats.update({
            "clients": hub.count,
            "ws_url": (f"{c.tunnel.public_ws_url}/ws/stream" if c.tunnel.public_url
                       else f"ws://{get_local_ip()}:{port}/ws/stream"),
            "http_url": c.tunnel.public_url or f"http://{get_local_ip()}:{port}",
            "is_public": bool(c.tunnel.public_url),
            "audio": audio.get_info(),
            "remote_control": c.remote.enabled,
            "pin_required": c.pin.required,
            "server_time": time.time(),
        })
        return jsonify(stats)

    return bp
