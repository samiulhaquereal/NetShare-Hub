"""WSGI middleware."""

_NO_DEFLATE_PATHS = frozenset({"/ws/stream"})


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
            if environ.get("PATH_INFO") in _NO_DEFLATE_PATHS:
                # Browsers always offer permessage-deflate and simple_websocket always
                # accepts it — re-compressing every JPEG/PCM message costs CPU on both
                # ends for no size gain. Withholding the offer disables it.
                environ.pop("HTTP_SEC_WEBSOCKET_EXTENSIONS", None)
        return self.wsgi_app(environ, start_response)
