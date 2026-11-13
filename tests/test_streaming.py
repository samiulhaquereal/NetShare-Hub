import json
import threading
import time
import unittest

from netshare.domain.protocol import AUD, CFG, CLP, IDL, VID
from netshare.services.events import ChangeNotifier
from netshare.services.stream_hub import StreamClient, StreamHub
from tests.helpers import AppTestCase


class FakeWs:
    def __init__(self, delay: float = 0.0):
        self.sent: list[bytes] = []
        self.delay = delay
        self._busy = threading.Lock()
        self.overlaps = 0

    def send(self, data):
        if not self._busy.acquire(blocking=False):
            self.overlaps += 1  # two threads writing the same socket at once
            self._busy.acquire()
        try:
            time.sleep(self.delay)
            self.sent.append(bytes(data))
        finally:
            self._busy.release()


def _run(client):
    t = threading.Thread(target=client.run_sender, daemon=True)
    t.start()
    return t


class StreamHubTests(unittest.TestCase):
    def test_every_client_gets_all_audio(self):
        hub = StreamHub()
        clients = [StreamClient(FakeWs()) for _ in range(3)]
        for cl in clients:
            hub.add(cl)
            _run(cl)
        for i in range(20):
            hub.publish_audio(bytes([i]) * 4)
        time.sleep(0.3)
        for cl in clients:
            audio = b"".join(m[1:] for m in cl._ws.sent if m[:1] == AUD)
            self.assertEqual(audio, b"".join(bytes([i]) * 4 for i in range(20)))
            hub.remove(cl)

    def test_slow_client_gets_latest_frame_not_backlog(self):
        hub = StreamHub()
        cl = StreamClient(FakeWs(delay=0.05))
        hub.add(cl)
        _run(cl)
        for i in range(30):
            hub.publish_frame(b"frame%d" % i)
            time.sleep(0.002)
        time.sleep(0.3)
        frames = [m[1:] for m in cl._ws.sent if m[:1] == VID]
        self.assertLess(len(frames), 10)
        self.assertEqual(frames[-1], b"frame29")
        hub.remove(cl)

    def test_clipboard_and_video_never_interleave(self):
        hub = StreamHub()
        ws = FakeWs(delay=0.002)
        cl = StreamClient(ws)
        hub.add(cl)
        _run(cl)
        stop = time.time() + 0.5
        def frames():
            while time.time() < stop:
                hub.publish_frame(b"x" * 10)
        t = threading.Thread(target=frames)
        t.start()
        while time.time() < stop:
            hub.publish_clipboard("clip")
        t.join()
        hub.remove(cl)
        self.assertEqual(ws.overlaps, 0)

    def test_control_messages_reach_every_client_before_media(self):
        hub = StreamHub()
        ws = FakeWs()
        cl = StreamClient(ws)
        hub.add(cl)
        hub.publish_state(paused=True)
        hub.publish_idle()
        hub.publish_frame(b"jpeg")
        _run(cl)
        time.sleep(0.2)
        hub.remove(cl)
        self.assertEqual(ws.sent[0][:1], CFG)
        self.assertEqual(json.loads(ws.sent[0][1:]), {"paused": True})
        self.assertEqual(ws.sent[1], IDL)
        self.assertEqual(ws.sent[2], VID + b"jpeg")

    def test_notifier_wakes_waiters(self):
        n = ChangeNotifier()
        seen = n.snapshot()
        threading.Timer(0.05, lambda: n.bump("files")).start()
        t0 = time.time()
        self.assertEqual(n.wait(seen, timeout=2)["files"], 1)
        self.assertLess(time.time() - t0, 1)


class LiveWebSocketTests(AppTestCase):
    """Runs the real WSGI server and talks to it over WebSockets."""

    def setUp(self):
        super().setUp()
        from werkzeug.serving import make_server
        self.server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.c.capture.stop()
        super().tearDown()

    def _ws(self, path):
        import simple_websocket
        return simple_websocket.Client.connect(f"ws://127.0.0.1:{self.port}{path}")

    def test_stream_handshake_frames_and_clipboard(self):
        try:
            import mss
            with mss.mss() as sct:
                sct.grab(sct.monitors[1])
        except Exception as exc:
            self.skipTest(f"no screen to capture: {exc}")
        self.c.capture.start()
        a, b = self._ws("/ws/stream"), self._ws("/ws/stream")
        try:
            first = a.receive(timeout=3)
            self.assertEqual(first[:1], CFG)
            self.assertIn("sample_rate", json.loads(first[1:]))
            self.assertEqual(a.receive(timeout=3)[:1], CLP)
            self.assertEqual(b.receive(timeout=3)[:1], CFG)
            self.assertEqual(b.receive(timeout=3)[:1], CLP)

            got_video = False
            deadline = time.time() + 5
            while time.time() < deadline and not got_video:
                msg = a.receive(timeout=2)
                got_video = msg is not None and msg[:1] == VID and msg[1:3] == b"\xff\xd8"  # JPEG SOI
            self.assertTrue(got_video, "no JPEG frame received")

            a.send(CLP + "from-a".encode())
            deadline = time.time() + 3
            got_clip = False
            while time.time() < deadline and not got_clip:
                msg = b.receive(timeout=1)
                got_clip = msg is not None and msg[:1] == CLP and msg[1:] == b"from-a"
            self.assertTrue(got_clip, "clipboard not relayed to other viewer")
            self.assertEqual(self.c.clipboard.text, "from-a")
        finally:
            a.close()
            b.close()

    def test_video_socket_skips_deflate_but_chat_keeps_it(self):
        import socket, base64, os
        def handshake(path):
            key = base64.b64encode(os.urandom(16)).decode()
            crlf = "\r\n"
            req = crlf.join([f"GET {path} HTTP/1.1", "Host: 127.0.0.1", "Upgrade: websocket",
                             "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13",
                             "Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits", "", ""])
            with socket.create_connection(("127.0.0.1", self.port), timeout=3) as s:
                s.sendall(req.encode())
                return s.recv(4096).decode(errors="replace").lower()
        stream, chat = handshake("/ws/stream"), handshake("/ws/chat")
        self.assertIn("101", stream.splitlines()[0])
        self.assertNotIn("permessage-deflate", stream)
        self.assertIn("permessage-deflate", chat)

    def test_capture_idles_without_viewers(self):
        self.c.capture.start()
        time.sleep(0.5)
        self.assertEqual(self.c.capture.frame_count, 0)

    def test_chat_socket_roundtrip(self):
        ws = self._ws("/ws/chat")
        try:
            self.assertEqual(json.loads(ws.receive(timeout=3))["action"], "update_users")
            ws.send(json.dumps({"type": "text", "to_target": "group", "text": "<img src=x onerror=alert(1)>"}))
            deadline = time.time() + 3
            msg = None
            while time.time() < deadline:
                data = json.loads(ws.receive(timeout=2))
                if data.get("type") == "update_msg":
                    msg = data["msg"]
                    break
            self.assertIsNotNone(msg)
            self.assertEqual(msg["content"], "<img src=x onerror=alert(1)>")  # plain text; client escapes
        finally:
            ws.close()


class LiveClipboardTests(LiveWebSocketTests):
    def _recv(self, ws, want_type, timeout=3):
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = ws.receive(timeout=1)
            if raw is None:
                continue
            msg = json.loads(raw)
            if msg["type"] == want_type:
                return msg
        self.fail(f"no {want_type!r} message")

    def test_ws_clipboard_syncs_instantly_and_in_order(self):
        a, b = self._ws("/ws/clipboard"), self._ws("/ws/clipboard")
        try:
            self.assertEqual(self._recv(a, "state")["text"], "")
            self.assertEqual(self._recv(b, "state")["text"], "")
            for i, text in enumerate(["h", "he", "hel", "hello"], 1):
                a.send(json.dumps({"type": "set", "text": text, "seq": i}))
            acks = [self._recv(a, "ack") for _ in range(4)]
            self.assertEqual([x["seq"] for x in acks], [1, 2, 3, 4])
            deadline, last = time.time() + 3, None
            while time.time() < deadline and (last is None or last["text"] != "hello"):
                last = self._recv(b, "text")
            self.assertEqual(last["text"], "hello", "other device sees the newest text")
            self.assertEqual(last["version"], acks[-1]["version"])
            self.assertEqual(self.c.clipboard.text, "hello")
            b.send(json.dumps({"type": "set", "text": "", "seq": 1}))  # Clear on another device
            self.assertEqual(self._recv(a, "text")["text"], "")
            self.assertEqual(self.c.clipboard.text, "")
            a.send('{"type":"ping"}')
            self._recv(a, "pong")
        finally:
            a.close(); b.close()

    def test_ws_clipboard_rejects_oversized_text(self):
        from netshare.services.clipboard import MAX_CLIPBOARD_BYTES
        a = self._ws("/ws/clipboard")
        try:
            self._recv(a, "state")
            a.send(json.dumps({"type": "set", "text": "x" * (MAX_CLIPBOARD_BYTES + 1), "seq": 7}))
            err = self._recv(a, "error")
            self.assertEqual(err["seq"], 7)
            self.assertEqual(self.c.clipboard.text, "")
        finally:
            a.close()

    def test_http_clipboard_api_limits_and_reaches_ws_peers(self):
        a = self._ws("/ws/clipboard")
        try:
            self._recv(a, "state")
            r = self.lan().post("/clipboard", json={"text": "from http"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(self._recv(a, "text")["text"], "from http")
            self.assertEqual(self.lan().post("/clipboard", json={"text": "x" * 1_000_001}).status_code, 413)
            self.assertEqual(self.lan().post("/clipboard", data={"text": "x"}).status_code, 415)
            self.assertEqual(self.lan().get("/clipboard").get_json()["text"], "from http")
        finally:
            a.close()


class ClipboardMergeTests(unittest.TestCase):
    def test_merge3_keeps_both_edits_in_different_places(self):
        from netshare.services.clipboard import merge3
        base = "TOP\n\nEND\n"
        self.assertEqual(merge3(base, "TOP\nmine\n\nEND\n", "TOP\n\nEND\ntheirs"), "TOP\nmine\n\nEND\ntheirs")
        self.assertEqual(merge3(base, "TOP\n\nEND\nmine", "TOP\ntheirs\n\nEND\n"), "TOP\ntheirs\n\nEND\nmine")
        self.assertEqual(merge3(base, base, "x"), "x")
        self.assertEqual(merge3(base, "x", base), "x")
        self.assertIsNone(merge3("abc", "aXc", "aYc"), "same spot → conflict")
        self.assertEqual(merge3("ছবি নোট", "ছবি নতুন নোট", "ছবি নোট!"), "ছবি নতুন নোট!")

    def test_service_merges_edit_based_on_older_version(self):
        from netshare.services.clipboard import ClipboardService
        svc = ClipboardService(StreamHub(), ChangeNotifier())
        v1 = svc.update("Shopping:\n\nNotes:\n")
        svc.edit("Shopping:\n\nNotes:\ncall mom", base=v1)          # B
        v, text = svc.edit("Shopping:\nmilk\n\nNotes:\n", base=v1)  # A, raced with B
        self.assertEqual(text, "Shopping:\nmilk\n\nNotes:\ncall mom")
        self.assertEqual(svc.text, text)
        _, text = svc.edit("Clear wins", base=v1)  # overlapping change → last writer wins
        self.assertEqual(text, "Clear wins")
        _, text = svc.edit("no base → replace")
        self.assertEqual(text, "no base → replace")
