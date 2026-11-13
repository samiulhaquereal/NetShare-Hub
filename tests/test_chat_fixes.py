"""Messenger bug fixes: permissions, typing, unread events, attachments, avatars,
presence, catch-up after reconnect, slow sockets and safe serving of uploads."""

import io
import json
import threading
import time
from unittest import mock

from PIL import Image

from netshare.domain.chat import public_id
from netshare.services.chat_service import ChatConnection
from tests.helpers import AppTestCase


class RecordingSocket:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)

    def close(self):
        pass

    def events(self, kind=None):
        out = [json.loads(m) for m in self.sent]
        return [e for e in out if kind is None or e.get("type") == kind]


class ChatFixTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.chat = self.c.chat
        self.alice, self.bob, self.eve = "AA:00:00:00:00:01", "BB:00:00:00:00:02", "EE:00:00:00:00:03"
        for k, n in ((self.alice, "Alice"), (self.bob, "Bob"), (self.eve, "Eve")):
            self.chat.set_username(k, n)
        self.sock = {}
        for k in (self.alice, self.bob, self.eve):
            self.sock[k] = RecordingSocket()
            self.chat.connect(k, ChatConnection(self.sock[k]))
        self.drain()
        for s in self.sock.values():
            s.sent.clear()

    def drain(self):
        for conns in list(self.chat.hub._by_key.values()):
            for conn in conns:
                conn.drain()

    def post(self, me, target, text):
        self.chat.handle_event(me, {"type": "text", "to_target": target, "text": text})
        return self.chat.messages(me, target, None, 1)[-1]["id"]

    def test_outsider_cannot_react_to_a_private_message(self):
        dm = self.post(self.alice, public_id(self.bob), "secret")
        self.assertFalse(self.chat.react(self.eve, dm, "👍", remove=False))
        self.assertFalse(self.chat.react(self.eve, 999999, "👍", remove=False), "missing message")
        self.assertTrue(self.chat.react(self.bob, dm, "👍", remove=False))
        gid = self.chat.create_group(self.alice, "team", [public_id(self.bob)])
        gm = self.post(self.alice, gid, "in group")
        self.assertFalse(self.chat.react(self.eve, gm, "👍", remove=False), "non-member")
        r = self.lan().post(f"/api/messages/{dm}/react", json={"emoji": "👍"})
        self.assertEqual(r.status_code, 404)

    def test_typing_in_a_dm_names_the_typer_as_the_conversation(self):
        self.chat.handle_event(self.alice, {"type": "typing", "target": public_id(self.bob), "is_typing": True})
        self.drain()
        ev = self.sock[self.bob].events("typing")[0]
        self.assertEqual(ev["target"], public_id(self.alice), "Bob's sidebar row for this chat is Alice")
        self.assertEqual(self.sock[self.eve].events("typing"), [])

    def test_non_member_typing_in_a_group_is_ignored(self):
        gid = self.chat.create_group(self.alice, "team", [public_id(self.bob)])
        self.drain()
        self.chat.handle_event(self.eve, {"type": "typing", "target": gid, "is_typing": True})
        self.drain()
        self.assertEqual(self.sock[self.bob].events("typing"), [])

    def test_new_messages_and_updates_are_different_events(self):
        mid = self.post(self.alice, "group", "hello")
        self.chat.handle_event(self.alice, {"type": "edit", "msg_id": mid, "text": "hello!"})
        self.chat.react(self.bob, mid, "🔥", remove=False)
        self.drain()
        kinds = [e["type"] for e in self.sock[self.eve].events() if e["type"] in ("new_msg", "update_msg")]
        self.assertEqual(kinds, ["new_msg", "update_msg", "update_msg"])

    def test_removed_member_is_told_why_their_message_vanished(self):
        gid = self.chat.create_group(self.alice, "team", [public_id(self.bob)])
        self.chat.update_group(self.alice, gid, "team", [])
        self.drain()
        self.sock[self.bob].sent.clear()
        self.chat.handle_event(self.bob, {"type": "text", "to_target": gid, "text": "hello?"})
        self.drain()
        err = self.sock[self.bob].events("error")
        self.assertEqual(err[0]["code"], "not_member")

    def test_deleting_a_file_message_deletes_the_file(self):
        name = self.c.chat_files.save_upload("photo.jpg", io.BytesIO(b"\xff\xd8 private"))
        url = f"/chat/download/{name}"
        self.chat.handle_event(self.alice, {"type": "file", "to_target": "group", "url": url,
                                            "filename": "photo.jpg", "filetype": "image/jpeg"})
        mid = self.chat.messages(self.alice, "group", None, 1)[-1]["id"]
        self.assertEqual(self.lan().get(url).status_code, 200)
        self.chat.handle_event(self.alice, {"type": "delete", "msg_id": mid})
        self.assertEqual(self.lan().get(url).status_code, 404)

    def test_file_shared_by_two_messages_survives_one_delete(self):
        name = self.c.chat_files.save_upload("doc.pdf", io.BytesIO(b"%PDF"))
        url = f"/chat/download/{name}"
        for _ in range(2):
            self.chat.handle_event(self.alice, {"type": "file", "to_target": "group", "url": url, "filename": "d"})
        mid = self.chat.messages(self.alice, "group", None, 1)[-1]["id"]
        self.chat.handle_event(self.alice, {"type": "delete", "msg_id": mid})
        self.assertTrue((self.settings.chat_upload_dir / name).exists())

    def test_presence_only_on_real_online_changes(self):
        second_tab = ChatConnection(RecordingSocket())
        self.chat.connect(self.alice, second_tab)       # Alice opens another tab: still just "online"
        self.chat.disconnect(self.alice, second_tab)    # closes one tab: still online
        self.drain()
        self.assertEqual(self.sock[self.bob].events("presence"), [])
        self.assertEqual(self.sock[self.bob].events("system"), [], "no refetch-everything storm")
        conn = next(iter(self.chat.hub._by_key[self.alice]))
        self.chat.disconnect(self.alice, conn)
        self.drain()
        self.assertEqual(self.sock[self.bob].events("presence"),
                         [{"type": "presence", "user": public_id(self.alice), "online": False}])

    def test_catch_up_after_reconnect(self):
        first = self.post(self.alice, "group", "one")
        self.post(self.bob, "group", "two")
        self.post(self.bob, "group", "three")
        missed = self.chat.messages(self.alice, "group", None, 50, after_id=first)
        self.assertEqual([m["content"] for m in missed], ["two", "three"])
        r = self.lan().get(f"/api/messages?target=group&after_id={first}")
        self.assertEqual([m["content"] for m in r.get_json()], ["two", "three"])

    def test_slow_socket_does_not_block_others(self):
        gate = threading.Event()

        class Stuck(RecordingSocket):
            def send(self, msg):
                gate.wait(10)  # phone stopped reading

        stuck = ChatConnection(Stuck())
        self.chat.connect("SS:00:00:00:00:09", stuck)
        t0 = time.monotonic()
        self.post(self.alice, "group", "first")
        self.post(self.alice, "group", "second")
        self.assertLess(time.monotonic() - t0, 1.0, "sender isn't held up")
        self.drain()
        self.assertEqual(len(self.sock[self.bob].events("new_msg")), 2, "others got both")
        gate.set()

    def test_a_socket_that_never_reads_is_dropped(self):
        gate = threading.Event()

        class Stuck(RecordingSocket):
            def send(self, msg):
                gate.wait(10)

        conn = ChatConnection(Stuck())
        with mock.patch.object(ChatConnection, "MAX_QUEUE", 5):
            for i in range(20):
                conn.send(str(i))
        self.assertTrue(conn.closed)
        gate.set()


class AvatarTests(AppTestCase):
    def png(self, size=(900, 600)):
        buf = io.BytesIO()
        Image.new("RGB", size, (200, 30, 30)).save(buf, "PNG")
        return buf.getvalue()

    def upload(self, client, data, name="me.png"):
        return client.post("/api/avatar", data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")

    def test_avatar_is_validated_resized_and_replaced(self):
        c = self.lan()
        self.assertEqual(self.upload(c, self.png()).status_code, 400, "name first")
        c.post("/api/user", json={"username": "Phone"})
        self.assertEqual(self.upload(c, b"MZ not an image", "x.exe").status_code, 400)
        first = self.upload(c, self.png()).get_json()["avatar"]
        r = c.get(first)
        with Image.open(io.BytesIO(r.data)) as im:
            self.assertEqual((im.format, im.size), ("JPEG", (256, 256)))
        r.close()
        second = self.upload(c, self.png((300, 300))).get_json()["avatar"]
        self.assertNotEqual(first, second)
        self.assertEqual(c.get(first).status_code, 404, "old picture removed")


class SafeUploadServingTests(AppTestCase):
    def test_html_and_svg_cannot_run_script(self):
        store = self.c.chat_files
        store.save_upload("page.html", io.BytesIO(b"<script>alert(1)</script>"))
        r = self.lan().get("/chat/download/page.html")
        self.assertTrue(r.content_type.startswith("text/plain"))
        self.assertIn("sandbox", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        r.close()
        self.c.files.save_upload("logo.svg", io.BytesIO(b"<svg xmlns='http://www.w3.org/2000/svg'/>"))
        r = self.lan().get("/file-preview/logo.svg")
        self.assertTrue(r.content_type.startswith("image/svg+xml"), "still usable in <img>")
        self.assertIn("sandbox", r.headers["Content-Security-Policy"])
        r.close()
        self.c.files.save_upload("doc.pdf", io.BytesIO(b"%PDF-1.4"))
        r = self.lan().get("/file-preview/doc.pdf")
        self.assertNotIn("Content-Security-Policy", r.headers, "Chrome's PDF viewer needs no sandbox")
        r.close()


class StaleConnectionTests(AppTestCase):
    def test_silent_phone_goes_offline(self):
        import simple_websocket
        from werkzeug.serving import make_server
        server = make_server("127.0.0.1", 0, self.app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with mock.patch("netshare.web.routes.chat._WS_RECEIVE_TIMEOUT", 1):
                ws = simple_websocket.Client.connect(f"ws://127.0.0.1:{server.server_port}/ws/chat")
                deadline = time.time() + 2
                while time.time() < deadline and not self.c.chat.hub.active_keys():
                    time.sleep(0.05)
                self.assertTrue(self.c.chat.hub.active_keys(), "online while connected")
                deadline = time.time() + 5  # the phone vanished: it never sends anything again
                while time.time() < deadline and self.c.chat.hub.active_keys():
                    time.sleep(0.1)
                self.assertFalse(self.c.chat.hub.active_keys(), "marked offline after the timeout")
                self.assertFalse(ws.connected, "server closed the dead connection")
        finally:
            server.shutdown()


class UnreadAndPreviewTests(ChatFixTests):
    def item(self, me, conv):
        sb = self.chat.sidebar(me, 0, 100)
        return next(i for i in sb["items"] if i["id"] == conv), sb["previews"].get(conv)

    def test_unread_counts_are_kept_on_the_server(self):
        self.post(self.alice, "group", "one")
        self.post(self.alice, "group", "two")
        self.assertEqual(self.item(self.bob, "group")[0]["unread"], 2)
        self.assertEqual(self.item(self.alice, "group")[0]["unread"], 0, "own messages aren't unread")
        last = self.post(self.alice, public_id(self.bob), "dm")
        self.assertEqual(self.item(self.bob, public_id(self.alice))[0]["unread"], 1)
        self.chat.handle_event(self.bob, {"type": "read", "to_target": "group"})
        self.chat.handle_event(self.bob, {"type": "read", "to_target": public_id(self.alice), "last_id": last})
        self.assertEqual(self.item(self.bob, "group")[0]["unread"], 0)
        self.assertEqual(self.item(self.bob, public_id(self.alice))[0]["unread"], 0)

    def test_newcomer_does_not_inherit_old_history_as_unread(self):
        self.post(self.alice, "group", "old news")
        self.chat.set_username("NN:00:00:00:00:07", "Newbie")
        self.assertEqual(self.item("NN:00:00:00:00:07", "group")[0]["unread"], 0)

    def test_preview_has_time_and_sender(self):
        self.post(self.alice, "group", "hello all")
        _, prev = self.item(self.bob, "group")
        self.assertEqual((prev["text"], prev["sender"], prev["mine"]), ("hello all", "Alice", False))
        self.assertTrue(prev["ts"])
        self.assertTrue(self.item(self.alice, "group")[1]["mine"])

    def test_group_items_carry_member_count(self):
        gid = self.chat.create_group(self.alice, "team", [public_id(self.bob), public_id(self.eve)])
        self.assertEqual(self.item(self.alice, gid)[0]["members"], 3)

    def test_one_reaction_per_person(self):
        mid = self.post(self.alice, "group", "react")
        self.chat.react(self.bob, mid, "👍", remove=False)
        self.chat.react(self.bob, mid, "❤️", remove=False)
        self.assertEqual(self.chat.messages(self.alice, "group", None, 1)[-1]["reactions"], {"❤️": [public_id(self.bob)]})

    def test_upgrade_marks_existing_history_read(self):
        import sqlite3
        self.post(self.alice, "group", "before the upgrade")
        with sqlite3.connect(self.settings.db_path) as db:
            db.execute("DROP TABLE read_marks")
        self.chat._repo.init_schema(self.chat._cipher.encode)  # app restarts with the new version
        self.assertEqual(self.item(self.bob, "group")[0]["unread"], 0)
