"""Self-signed HTTPS listener for the LAN (voice messages need a secure context)."""

import json
import socket
import ssl
import threading
import time

from cryptography import x509

from netshare.infrastructure import tls
from netshare.web.server import make_wsgi_server
from tests.helpers import AppTestCase


class CertificateTests(AppTestCase):
    def sans(self, path):
        cert = x509.load_pem_x509_certificate(path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return {str(i) for i in san.get_values_for_type(x509.IPAddress)} | set(san.get_values_for_type(x509.DNSName)), cert

    def test_created_once_reused_and_renewed_when_ip_changes(self):
        d = self.settings.cert_dir
        cert, key = tls.ensure_certificate(d, ["localhost", "127.0.0.1", "192.168.0.10"])
        names, c = self.sans(cert)
        self.assertTrue({"localhost", "127.0.0.1", "192.168.0.10"} <= names)
        self.assertLessEqual((c.not_valid_after_utc - c.not_valid_before_utc).days, 825, "Apple's limit")
        mtime = cert.stat().st_mtime_ns
        tls.ensure_certificate(d, ["localhost", "192.168.0.10"])
        self.assertEqual(cert.stat().st_mtime_ns, mtime, "still valid → reused")
        time.sleep(0.02)
        tls.ensure_certificate(d, ["localhost", "10.0.0.7"])  # laptop moved to another network
        self.assertIn("10.0.0.7", self.sans(cert)[0])
        ctx = tls.server_context(cert, key)
        self.assertIsInstance(ctx, ssl.SSLContext)

    def test_local_hosts_includes_loopback_and_extra(self):
        hosts = tls.local_hosts(["192.168.5.5"])
        self.assertTrue({"localhost", "127.0.0.1", "192.168.5.5"} <= set(hosts))


class HttpsServerTests(AppTestCase):
    def setUp(self):
        super().setUp()
        cert, key = tls.ensure_certificate(self.settings.cert_dir, ["localhost", "127.0.0.1"])
        self.server = make_wsgi_server("127.0.0.1", 0, self.app, tls=tls.server_context(cert, key))
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client_ctx = ssl.create_default_context()
        self.client_ctx.check_hostname = False
        self.client_ctx.verify_mode = ssl.CERT_NONE  # self-signed, like a phone after "Proceed"

    def tearDown(self):
        self.server.shutdown()
        super().tearDown()

    def get(self, path):
        import http.client
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=self.client_ctx, timeout=5)
        conn.request("GET", path)
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, body

    def test_pages_and_api_over_https(self):
        status, body = self.get("/messenger")
        self.assertEqual(status, 200)
        self.assertIn(b'"wsUrl": "wss://127.0.0.1:', body, "WebSockets switch to wss on the secure page")
        status, body = self.get("/api/clips")
        self.assertEqual(status, 200)

    def test_secure_websocket(self):
        import simple_websocket
        ws = simple_websocket.Client.connect(f"wss://127.0.0.1:{self.port}/ws/clipboard",
                                             ssl_context=self.client_ctx)
        try:
            self.assertEqual(json.loads(ws.receive(timeout=5))["type"], "state")
            ws.send(json.dumps({"type": "set", "text": "over wss", "seq": 1, "base": 0}))
            deadline = time.time() + 3
            while time.time() < deadline and self.c.clipboard.text != "over wss":
                time.sleep(0.05)
            self.assertEqual(self.c.clipboard.text, "over wss")
        finally:
            ws.close()

    def test_plain_http_or_stalled_handshake_does_not_block_others(self):
        idle = socket.create_connection(("127.0.0.1", self.port))         # never says hello
        plain = socket.create_connection(("127.0.0.1", self.port))
        plain.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")               # http:// on the https port
        try:
            t0 = time.time()
            status, _ = self.get("/api/clips")
            self.assertEqual(status, 200)
            self.assertLess(time.time() - t0, 3, "a stuck handshake must not hold up the accept loop")
        finally:
            idle.close()
            plain.close()


class HttpsPortExposedTests(AppTestCase):
    def test_pages_know_the_https_port(self):
        body = self.lan().get("/messenger").data
        self.assertIn(f'"httpsPort": {self.settings.https_port}'.encode(), body)
