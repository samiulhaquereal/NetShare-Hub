"""HTTP server tuned for many simultaneous devices (a classroom of phones).

Werkzeug's threaded server accepts one connection per wake-up of its accept
loop, and that loop has to win the GIL against every busy request thread. With
hundreds of devices connecting at once the listen backlog (capped at ~200 on
Windows client editions) overflows and new connections are refused. Here each
wake-up drains *all* pending connections in one go.

For HTTPS the TLS handshake runs in the connection's own thread, not in the
accept loop: a phone on bad Wi-Fi taking seconds to finish its handshake must
not stop everybody else from connecting.
"""

from __future__ import annotations

import ssl
import sys

from werkzeug.serving import ThreadedWSGIServer

LISTEN_BACKLOG = 2048   # the OS caps it (Windows client editions: ~200)
MAX_ACCEPT_BATCH = 256
HANDSHAKE_TIMEOUT = 15


class BurstWSGIServer(ThreadedWSGIServer):
    request_queue_size = LISTEN_BACKLOG
    daemon_threads = True
    # On Windows SO_REUSEADDR lets another program bind the same port and silently
    # steal part of the traffic (semantics differ from POSIX).
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, host: str, port: int, app, tls: ssl.SSLContext | None = None):
        super().__init__(host, port, app)  # no ssl_context: werkzeug would handshake in accept()
        self.tls = tls
        if tls is not None:
            self.ssl_context = tls  # werkzeug reads it for wsgi.url_scheme = "https"

    def server_activate(self) -> None:
        super().server_activate()
        self.socket.setblocking(False)  # lets the accept loop stop when the queue is empty

    def get_request(self):
        conn, addr = self.socket.accept()
        conn.setblocking(True)  # accepted sockets inherit non-blocking mode on Windows
        return conn, addr

    def _handle_request_noblock(self) -> None:
        for _ in range(MAX_ACCEPT_BATCH):
            try:
                request, client_address = self.get_request()
            except OSError:  # BlockingIOError: nothing left to accept
                return
            if not self.verify_request(request, client_address):
                self.shutdown_request(request)
                continue
            try:
                self.process_request(request, client_address)
            except Exception:
                self.handle_error(request, client_address)
                self.shutdown_request(request)

    def process_request_thread(self, request, client_address) -> None:
        if self.tls is not None:
            try:
                request.settimeout(HANDSHAKE_TIMEOUT)
                request = self.tls.wrap_socket(request, server_side=True)
                request.settimeout(None)
            except (OSError, ssl.SSLError):  # plain http:// on the https port, cert rejected, timeout
                try:
                    request.close()
                except OSError:
                    pass
                return
        super().process_request_thread(request, client_address)


def make_wsgi_server(host: str, port: int, app, tls: ssl.SSLContext | None = None) -> BurstWSGIServer:
    return BurstWSGIServer(host, port, app, tls)
