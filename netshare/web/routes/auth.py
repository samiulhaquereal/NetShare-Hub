"""PIN unlock flow used by every page's lock overlay."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..context import PIN_COOKIE, client_info, is_host, pin_token


def create_blueprint(c, _sock) -> Blueprint:
    bp = Blueprint("auth", __name__, url_prefix="/api/auth")

    @bp.route("/status")
    def status():
        info = client_info()
        return jsonify({"pin_required": c.pin.required,
                        "is_authenticated": is_host() or c.pin.is_authorized(pin_token()),
                        "is_blocked": c.devices.is_blocked(info.ip)})

    @bp.route("/verify", methods=["POST"])
    def verify():
        data = request.get_json(silent=True) or {}
        ok, result, retry_after = c.pin.verify(client_info().ip, str(data.get("pin", "")))
        if not ok:
            resp = jsonify({"status": "error", "error": result})
            resp.status_code = 429 if retry_after else 401
            if retry_after:
                resp.headers["Retry-After"] = str(retry_after)
            return resp
        resp = jsonify({"status": "ok", "token": result})
        if result:
            resp.set_cookie(PIN_COOKIE, result, max_age=10 * 24 * 3600, samesite="Lax")
        return resp

    return bp
