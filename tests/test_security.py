from tests.helpers import LAN_IP, AppTestCase


class PinTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.c.pin.set_pin("4321")

    def test_api_requires_pin_for_visitors(self):
        r = self.lan().get("/api/users")
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.get_json()["pin_required"])

    def test_adb_and_info_are_no_longer_exempt(self):
        self.assertEqual(self.lan().get("/api/adb/devices").status_code, 401)
        self.assertEqual(self.lan().get("/info").status_code, 401)

    def test_pages_render_lock_overlay_instead_of_json(self):
        for page in ("/", "/screen", "/files-page", "/clipboard-page", "/messenger", "/adb-hub"):
            r = self.lan().get(page)
            self.assertEqual(r.status_code, 200, page)
            self.assertIn(b'id="pin-lock-modal" style="display: flex', r.data, page)

    def test_correct_pin_issues_working_token(self):
        r = self.lan().post("/api/auth/verify", json={"pin": "4321"})
        self.assertEqual(r.status_code, 200)
        token = r.get_json()["token"]
        self.assertTrue(token)
        ok = self.lan().get("/api/users", headers={"X-Server-PIN-Token": token})
        self.assertEqual(ok.status_code, 200)

    def test_token_invalidated_when_pin_changes(self):
        token = self.lan().post("/api/auth/verify", json={"pin": "4321"}).get_json()["token"]
        self.c.pin.set_pin("9999")
        self.assertEqual(self.lan().get("/api/users", headers={"X-Server-PIN-Token": token}).status_code, 401)

    def test_bruteforce_is_throttled(self):
        c = self.lan()
        codes = [c.post("/api/auth/verify", json={"pin": f"{i:04d}"}).status_code for i in range(6)]
        self.assertEqual(codes[:4], [401] * 4)
        self.assertEqual(codes[4], 429)
        # even the right PIN is refused while locked out
        self.assertEqual(c.post("/api/auth/verify", json={"pin": "4321"}).status_code, 429)
        # other clients are unaffected
        self.assertEqual(self.lan("192.168.1.77").post("/api/auth/verify", json={"pin": "4321"}).status_code, 200)

    def test_host_is_trusted(self):
        self.assertEqual(self.host().get("/api/users").status_code, 200)


class AdminTests(AppTestCase):
    def test_admin_endpoints_are_host_only(self):
        lan = self.lan()
        self.assertEqual(lan.get("/api/security/pin").status_code, 403)
        self.assertEqual(lan.post("/api/security/pin", json={"pin": "1"}).status_code, 403)
        self.assertEqual(lan.get("/api/security/devices").status_code, 403)
        self.assertEqual(lan.post("/api/security/device/block", json={"ip": "127.0.0.1"}).status_code, 403)
        self.assertEqual(lan.post("/settings/public", json={"action": "start", "token": "x"}).status_code, 403)
        self.assertFalse(self.c.pin.required)

    def test_tunnel_visitors_are_not_host(self):
        self.assertEqual(self.tunnel().get("/api/security/pin").status_code, 403)

    def test_host_can_set_pin(self):
        r = self.host().post("/api/security/pin", json={"pin": "1234"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.c.pin.required)

    def test_blocking(self):
        self.host().post("/api/security/device/block", json={"ip": LAN_IP, "block": True})
        self.assertEqual(self.lan().get("/api/users").status_code, 403)
        self.assertIn(b"blocked", self.lan().get("/").data.lower())
        self.assertEqual(self.lan("192.168.1.99").get("/api/users").status_code, 200)

    def test_tunnel_ip_comes_from_forwarded_header(self):
        self.host().post("/api/security/device/block", json={"ip": "203.0.113.9", "block": True})
        self.assertEqual(self.tunnel("203.0.113.9").get("/api/users").status_code, 403)
        self.assertEqual(self.tunnel("203.0.113.10").get("/api/users").status_code, 200)

    def test_lan_client_cannot_spoof_forwarded_for(self):
        self.host().post("/api/security/device/block", json={"ip": LAN_IP, "block": True})
        c = self.lan()
        r = c.get("/api/users", headers={"X-Forwarded-For": "10.9.9.9"})
        self.assertEqual(r.status_code, 403)
