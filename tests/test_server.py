import socket
import threading
import unittest
import urllib.request

from netshare.web.server import BurstWSGIServer, make_wsgi_server


def _app(environ, start_response):
    body = environ["PATH_INFO"].encode()
    start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))])
    return [body]


class BurstServerTests(unittest.TestCase):
    def setUp(self):
        self.server = make_wsgi_server("127.0.0.1", 0, _app)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_serves_requests_with_blocking_client_sockets(self):
        self.assertIsInstance(self.server, BurstWSGIServer)
        self.assertEqual(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/hello", timeout=5).read(), b"/hello")

    def test_burst_of_simultaneous_connections_is_drained(self):
        """300 clients connect at the same instant (> Windows' backlog cap of 200)."""
        socks = [socket.create_connection(("127.0.0.1", self.port), timeout=10) for _ in range(300)]
        try:
            for i, s in enumerate(socks):
                s.sendall(f"GET /c{i} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            for i, s in enumerate(socks):
                data = b""
                while not data.endswith(f"/c{i}".encode()):
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                self.assertTrue(data.endswith(f"/c{i}".encode()), i)
        finally:
            for s in socks:
                s.close()
