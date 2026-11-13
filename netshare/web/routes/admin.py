"""Host-only administration: PIN, device blocking and public (ngrok) access.

These used to be callable by any connected device — e.g. a visitor could expose
the PC to the internet or lock everyone else out. They now answer only to
requests made on the host machine itself.
"""

from __future__ import annotations

from functools import wraps

from flask import Blueprint, jsonify, request

from ..context import is_host


def _host_only(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_host():
            return jsonify({"error": "This action is only available on the host PC."}), 403
        return fn(*args, **kwargs)
    return wrapper


def create_blueprint(c, _sock) -> Blueprint:
    bp = Blueprint("admin", __name__)

    @bp.route("/api/security/pin", methods=["GET", "POST"])
    @_host_only
    def security_pin():
        if request.method == "POST":
            c.pin.set_pin(str((request.get_json(silent=True) or {}).get("pin", "")))
        return jsonify({"status": "ok", "pin_required": c.pin.required, "pin": c.pin.pin})

    @bp.route("/api/security/devices")
    @_host_only
    def security_devices():
        return jsonify(c.devices.snapshot())

    @bp.route("/api/security/device/block", methods=["POST"])
    @_host_only
    def security_block():
        data = request.get_json(silent=True) or {}
        ip = data.get("ip")
        if not ip:
            return jsonify({"error": "Missing ip"}), 400
        block = bool(data.get("block", True))
        c.devices.set_blocked(ip, block)
        return jsonify({"status": "ok", "ip": ip, "blocked": block})

    @bp.route("/settings/ngrok_info")
    def ngrok_info():
        return jsonify({"is_public": bool(c.tunnel.public_url), "url": c.tunnel.public_url})

    @bp.route("/settings/public", methods=["POST"])
    @_host_only
    def settings_public():
        data = request.get_json(silent=True) or {}
        action = data.get("action")
        try:
            if action == "start":
                token = data.get("token") or c.prefs.get("ngrok_token")
                if not token:
                    return jsonify({"status": "error", "error": "Ngrok token is required"}), 400
                return jsonify({"status": "ok", "url": c.tunnel.start(token)})
            if action == "stop":
                c.tunnel.stop()
                return jsonify({"status": "ok"})
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 500
        return jsonify({"status": "error", "error": "Invalid action"}), 400

    return bp
