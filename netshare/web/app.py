"""Flask application factory."""

from __future__ import annotations

import datetime

from flask import Flask, g, jsonify, render_template, request
from flask_sock import Sock

from ..container import Container
from ..infrastructure.file_store import UnsafePathError
from ..services.identity import COOKIE_MAX_AGE, COOKIE_NAME
from .assets import init_assets
from .context import client_info, is_host, pin_token
from .middleware import FixNgrokWebSocketMiddleware
from .routes import adb, admin, auth, chat, clipboard, files, pages, stream

# Reachable without a PIN: the unlock flow itself and static assets.
_PUBLIC_PREFIXES = ("/api/auth/", "/static/", "/favicon.ico", "/manifest.json", "/sw.js", "/qr.png")


def create_app(c: Container) -> Flask:
    s = c.settings
    app = Flask(__name__, template_folder=str(s.template_dir), static_folder=str(s.static_dir),
                static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = 1024 ** 4  # 1 TB — uploads stream to disk
    app.wsgi_app = FixNgrokWebSocketMiddleware(app.wsgi_app)
    app.extensions["netshare"] = c
    init_assets(app, s.static_dir)

    sock = Sock(app)
    for module in (pages, auth, admin, files, clipboard, stream, chat, adb):
        app.register_blueprint(module.create_blueprint(c, sock))

    @app.before_request
    def _guard():
        info = client_info()
        c.devices.track(info.ip, request.headers.get("User-Agent", ""))
        if is_host():
            return None  # the host machine is always trusted
        if c.devices.is_blocked(info.ip) and not request.path.startswith("/static"):
            if request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json":
                return jsonify({"error": "Access Denied: Your IP address has been blocked by host.",
                                "blocked": True}), 403
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return render_template("blocked.html", ip=info.ip, server_time=now), 403
        if not c.pin.required or request.path.startswith(_PUBLIC_PREFIXES):
            return None
        if c.pin.is_authorized(pin_token()):
            return None
        if request.endpoint and request.endpoint.startswith("pages."):
            return None  # page renders its own PIN lock overlay
        return jsonify({"error": "Access PIN required", "pin_required": True}), 401

    @app.after_request
    def _finish(response):
        response.headers["ngrok-skip-browser-warning"] = "any"
        ident = getattr(g, "_identity", None)
        if ident is not None and ident.is_new:
            response.set_cookie(COOKIE_NAME, c.identity.cookie_value(ident.key), max_age=COOKIE_MAX_AGE,
                                httponly=True, samesite="Lax")
        return response

    @app.errorhandler(UnsafePathError)
    def _unsafe_path(_exc):
        return jsonify({"error": "file not found"}), 404

    return app
