"""HTML pages and PWA assets."""

from __future__ import annotations

from flask import Blueprint, make_response, render_template, send_from_directory

from ..context import base_urls, device_key, is_host, pin_token

_PAGES = {
    "/": "index.html",
    "/messenger": "messenger.html",
    "/screen": "screen.html",
    "/files-page": "files.html",
    "/clipboard-page": "clipboard.html",
    "/adb-hub": "adb.html",
}


def create_blueprint(c, _sock) -> Blueprint:
    bp = Blueprint("pages", __name__)
    static_dir = str(c.settings.static_dir)

    def _render(template: str):
        http_url, ws_url = base_urls(c)
        if template == "messenger.html":
            device_key(c)  # assign the chat identity cookie before the WebSocket connects
        return render_template(
            template, ws_url=ws_url, http_url=http_url, is_public=bool(c.tunnel.public_url),
            pin_required=c.pin.required, is_authenticated=is_host() or c.pin.is_authorized(pin_token()))

    for path, template in _PAGES.items():
        endpoint = template.removesuffix(".html").replace("-", "_")
        bp.add_url_rule(path, endpoint, (lambda t=template: _render(t)))

    @bp.route("/favicon.ico")
    def favicon():
        return send_from_directory(static_dir, "icon.png", mimetype="image/png")

    @bp.route("/manifest.json")
    def manifest():
        return send_from_directory(static_dir, "manifest.json")

    @bp.route("/sw.js")
    def service_worker():
        resp = make_response(send_from_directory(static_dir, "sw.js", mimetype="application/javascript"))
        resp.headers["Cache-Control"] = "no-cache"  # browsers must pick up SW updates
        return resp

    return bp
