"""WSGI middleware."""


class FixNgrokWebSocketMiddleware:
    """ngrok's free tier rewrites the Connection header and drops "Upgrade",
    which makes WSGI WebSocket handshakes fail. Restore it."""

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        if environ.get("HTTP_UPGRADE", "").lower() == "websocket":
            conn = environ.get("HTTP_CONNECTION", "")
            if "upgrade" not in conn.lower():
                environ["HTTP_CONNECTION"] = f"{conn}, Upgrade" if conn else "Upgrade"
        return self.wsgi_app(environ, start_response)
