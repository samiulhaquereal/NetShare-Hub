import base64

from netshare.domain.chat import public_id
from netshare.infrastructure.cipher import TextObfuscator
from netshare.services.chat_service import ChatConnection
from netshare.services.identity import COOKIE_NAME
from tests.helpers import LAN_IP, LAN_MAC, AppTestCase


class RecordingSocket:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


class CipherCompatTests(AppTestCase):
    def test_reads_data_written_by_old_server(self):
        key = "DefaultSecret123"
        text = "hello বাংলা 😀"
        xor = "".join(chr(ord(ch) ^ ord(key[i % len(key)])) for i, ch in enumerate(text))
        legacy = "ENC:" + base64.b64encode(xor.encode()).decode()
        ob = TextObfuscator(key)
        self.assertEqual(ob.decode(legacy), text)
        self.assertEqual(ob.encode(text), legacy)
        self.assertEqual(ob.decode("plain"), "plain")


class IdentityTests(AppTestCase):
    def test_lan_identity_uses_mac_once_then_cookie(self):
        c = self.lan()
        r = c.get("/api/user")
        self.assertEqual(r.get_json()["mac"], public_id(LAN_MAC))
        cookie = c.get_cookie(COOKIE_NAME)
        self.assertIsNotNone(cookie)
        calls = self.macs.calls
        c.get("/api/user")
        c.get("/api/sidebar")
        self.assertEqual(self.macs.calls, calls, "ARP must not run again once the cookie is set")

    def test_tunnel_visitors_get_distinct_identities(self):
        a, b = self.tunnel("203.0.113.1"), self.tunnel("203.0.113.2")
        ida, idb = a.get("/api/user").get_json()["mac"], b.get("/api/user").get_json()["mac"]
        self.assertNotEqual(ida, idb)
        self.assertEqual(a.get("/api/user").get_json()["mac"], ida)  # stable via cookie

    def test_forged_cookie_is_ignored(self):
        c = self.lan()
        c.set_cookie(COOKIE_NAME, "someone-else")
        self.assertEqual(c.get("/api/user").get_json()["mac"], public_id(LAN_MAC))

    def test_page_context_has_no_cipher_key(self):
        body = self.lan().get("/messenger").get_data(as_text=True)
        self.assertNotIn(self.settings.chat_cipher_key, body)
        self.assertNotIn("CIPHER_KEY", body)


class ChatServiceTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.chat = self.c.chat
        self.alice, self.bob, self.eve = "AA:00:00:00:00:01", "BB:00:00:00:00:02", "EE:00:00:00:00:03"
        for k, n in ((self.alice, "Alice"), (self.bob, "Bob"), (self.eve, "Eve")):
            self.chat.set_username(k, n)
        self.sockets = {}
        for k in (self.alice, self.bob, self.eve):
            s = RecordingSocket()
            self.sockets[k] = s
            self.chat.connect(k, ChatConnection(s))

    def _drain(self):
        for conns in self.chat.hub._by_key.values():
            for conn in conns:
                conn.drain()

    def _msgs(self, me, target):
        return self.chat.messages(me, target, None, 50)

    def test_text_is_stored_obfuscated_but_served_plain(self):
        self.chat.handle_event(self.alice, {"type": "text", "to_target": "group", "text": "<b>hi</b> there"})
        stored = self.c.chat._repo.list_messages(self.alice, "group", None, 1)[0]["content"]
        self.assertTrue(stored.startswith("ENC2:"), "AES-GCM encrypted at rest")
        self.assertEqual(self._msgs(self.bob, "group")[-1]["content"], "<b>hi</b> there")

    def test_search_finds_substrings(self):
        self.chat.handle_event(self.alice, {"type": "text", "to_target": "group", "text": "hello world"})
        self.chat.handle_event(self.alice, {"type": "text", "to_target": "group", "text": "nothing here"})
        hits = self.chat.search(self.bob, "WORLD")
        self.assertEqual([h["content"] for h in hits], ["hello world"])
        self.assertTrue(hits[0]["to_target"] == "group")

    def test_search_respects_private_conversations(self):
        self.chat.handle_event(self.alice, {"type": "text", "to_target": public_id(self.bob), "text": "secret plan"})
        self.assertEqual(len(self.chat.search(self.bob, "secret")), 1)
        self.assertEqual(self.chat.search(self.eve, "secret"), [])
        hit = self.chat.search(self.alice, "secret")[0]
        self.assertTrue(hit["to_target"].startswith("u_"), "raw device keys must not leak")

    def test_direct_message_only_reaches_participants(self):
        self.chat.handle_event(self.alice, {"type": "text", "to_target": public_id(self.bob), "text": "psst"})
        self._drain()
        self.assertTrue(any("psst" in m for m in self.sockets[self.bob].sent))
        self.assertTrue(any("psst" in m for m in self.sockets[self.alice].sent))
        self.assertFalse(any("psst" in m for m in self.sockets[self.eve].sent))
        self.assertEqual(self._msgs(self.eve, public_id(self.bob)), [])

    def test_group_membership_enforced(self):
        gid = self.chat.create_group(self.alice, "team", [public_id(self.bob)])
        self.chat.handle_event(self.eve, {"type": "text", "to_target": gid, "text": "intrude"})
        self.chat.handle_event(self.bob, {"type": "text", "to_target": gid, "text": "inside"})
        self.assertEqual([m["content"] for m in self._msgs(self.alice, gid)], ["inside"])
        self.assertEqual(self._msgs(self.eve, gid), [], "non-members can't read group history")
        self.assertIsNone(self.chat.group_members(self.eve, gid))
        self.assertFalse(self.chat.update_group(self.bob, gid, "hijack", []))

    def test_file_messages_require_local_url(self):
        for url in ("javascript:alert(1)", "https://evil.example/x", ""):
            self.chat.handle_event(self.alice, {"type": "file", "to_target": "group", "url": url, "filename": "x"})
        self.assertEqual(self._msgs(self.alice, "group"), [])
        self.chat.handle_event(self.alice, {"type": "file", "to_target": "group",
                                            "url": "/chat/download/a.png", "filename": "a.png", "filetype": "image/png"})
        self.assertEqual(self._msgs(self.alice, "group")[0]["file_url"], "/chat/download/a.png")

    def test_edit_delete_only_own_messages(self):
        self.chat.handle_event(self.alice, {"type": "text", "to_target": "group", "text": "v1"})
        mid = self._msgs(self.alice, "group")[0]["id"]
        self.chat.handle_event(self.bob, {"type": "edit", "msg_id": mid, "text": "hacked"})
        self.assertEqual(self._msgs(self.alice, "group")[0]["content"], "v1")
        self.chat.handle_event(self.alice, {"type": "edit", "msg_id": mid, "text": "v2"})
        m = self._msgs(self.alice, "group")[0]
        self.assertEqual((m["content"], m["is_edited"]), ("v2", 1))
        self.chat.handle_event(self.alice, {"type": "delete", "msg_id": mid})
        self.assertEqual(self._msgs(self.alice, "group")[0]["is_deleted"], 1)

    def test_reactions_and_sidebar(self):
        self.chat.handle_event(self.alice, {"type": "text", "to_target": "group", "text": "react to me"})
        mid = self._msgs(self.alice, "group")[0]["id"]
        self.chat.react(self.bob, mid, "🔥", remove=False)
        self.assertEqual(self._msgs(self.alice, "group")[0]["reactions"], {"🔥": [public_id(self.bob)]})
        sb = self.chat.sidebar(self.alice, 0, 20)
        self.assertEqual(sb["previews"]["group"]["text"], "react to me")
        self.assertEqual({i["title"] for i in sb["items"]}, {"Global Chat", "Bob", "Eve"})

    def test_public_ids_resolve_after_restart(self):
        self.chat._public_to_key.clear()
        self.assertEqual(self.chat.resolve(public_id(self.bob)), self.bob)


class ChatHttpTests(AppTestCase):
    def test_lan_user_flow(self):
        c = self.lan()
        self.assertEqual(c.post("/api/user", json={"username": "Phone"}).get_json()["username"], "Phone")
        users = self.host().get("/api/users").get_json()
        self.assertIn(public_id(LAN_MAC), [u["mac"] for u in users])
        self.assertEqual(c.get("/api/messages?target=group").get_json(), [])
        self.assertEqual(c.get(f"/api/search?q=x").get_json(), [])
        self.assertEqual(LAN_IP, self.c.devices.snapshot()[0]["ip"])
