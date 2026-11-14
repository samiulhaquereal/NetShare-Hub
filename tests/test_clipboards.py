import json
import sqlite3
import time

from netshare.container import build_container
from tests.helpers import AppTestCase
from tests.test_streaming import LiveWebSocketTests


class SavedClipboardTests(AppTestCase):
    def test_create_list_open_and_delete(self):
        c = self.lan()
        self.assertEqual(c.post("/api/clips", data="{}").status_code, 415, "JSON only")
        r = c.post("/api/clips", json={})
        self.assertEqual(r.status_code, 201)
        cid = r.get_json()["id"]
        self.c.clipboard.edit("Wi-Fi password\nhunter2", board_id=cid)
        clips = c.get("/api/clips").get_json()["clips"]
        mine = next(x for x in clips if x["id"] == cid)
        self.assertEqual(mine["preview"], "Wi-Fi password\nhunter2")
        self.assertEqual(mine["chars"], 22)
        self.assertEqual(clips[0]["id"], cid, "newest first")
        self.assertFalse(any(x["shared"] for x in clips), "an unused shared clipboard isn't listed")
        self.assertEqual(c.get(f"/api/clips/{cid}").get_json()["text"], "Wi-Fi password\nhunter2")
        self.assertEqual(c.get(f"/clipboard-page/{cid}").status_code, 200)
        self.assertIn(b"Clipboards", c.get("/clipboard-page").data)
        self.assertEqual(c.delete(f"/api/clips/{cid}").status_code, 200)
        self.assertEqual(c.get(f"/api/clips/{cid}").status_code, 404)
        self.assertEqual(c.get(f"/clipboard-page/{cid}").status_code, 302, "deleted → back to the grid")
        self.c.clipboard.update("shared text")
        self.c.clipboard.flush()
        self.assertIn("shared", [x["id"] for x in c.get("/api/clips").get_json()["clips"]])
        self.assertEqual(c.delete("/api/clips/shared").status_code, 200, "the shared one can be deleted too")
        self.assertNotIn("shared", [x["id"] for x in c.get("/api/clips").get_json()["clips"]])
        self.assertEqual(self.c.clipboard.text, "", "API / screen viewers get a fresh empty one")
        self.c.clipboard.update("used again")
        self.c.clipboard.flush()
        self.assertIn("shared", [x["id"] for x in c.get("/api/clips").get_json()["clips"]], "listed again once used")

    def test_saved_in_db_obfuscated_and_survive_restart(self):
        cid = self.c.clipboard.create()
        self.c.clipboard.edit("ছবি link: https://example.com/secret", board_id=cid)
        self.c.clipboard.update("shared text")
        self.c.clipboard.flush()
        with sqlite3.connect(self.settings.db_path) as db:
            raw = db.execute("SELECT body FROM clipboards WHERE id=?", (cid,)).fetchone()[0]
        self.assertNotIn("secret", raw, "stored obfuscated like chat messages")
        restarted = build_container(self.settings).clipboard  # the app was restarted
        self.assertEqual(restarted.snapshot(cid)[0], "ছবি link: https://example.com/secret")
        self.assertEqual(restarted.text, "shared text")
        self.assertGreater(restarted.snapshot(cid)[1], 0, "versions continue after a restart")

    def test_typing_is_saved_in_the_background(self):
        cid = self.c.clipboard.create()
        self.c.clipboard.edit("typed", board_id=cid)
        deadline = time.time() + 5
        while time.time() < deadline:
            with sqlite3.connect(self.settings.db_path) as db:
                if db.execute("SELECT char_count FROM clipboards WHERE id=?", (cid,)).fetchone()[0] == 5:
                    break
            time.sleep(0.1)
        else:
            self.fail("edit never reached the DB")

    def test_abandoned_empty_clipboards_are_cleaned_up(self):
        cid = self.c.clipboard.create()
        kept = self.c.clipboard.create()
        self.c.clipboard.edit("keep me", board_id=kept)
        self.c.clipboard.flush()
        with sqlite3.connect(self.settings.db_path) as db:
            db.execute("UPDATE clipboards SET updated_at=datetime(updated_at, '-1 hour'), created_at=datetime(created_at, '-1 hour')")
        self.c.clipboard._boards.pop(cid)  # nobody has it open
        self.c.clipboard._boards.pop(kept)
        self.c.clipboard._cleaned_at = 0
        ids = [x["id"] for x in self.c.clipboard.list()]
        self.assertNotIn(cid, ids)
        self.assertIn(kept, ids)

    def test_bad_ids(self):
        c = self.lan()
        for bad in ("nope", "../../x", "a" * 40):
            self.assertIn(c.get("/api/clips/" + bad).status_code, (404,), bad)
        self.assertEqual(c.delete("/api/clips/nope").status_code, 404)


class LiveSavedClipboardTests(LiveWebSocketTests):
    def _recv(self, ws, want, timeout=3):
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = ws.receive(timeout=1)
            if raw and json.loads(raw)["type"] == want:
                return json.loads(raw)
        self.fail(f"no {want!r}")

    def test_each_clipboard_syncs_separately_and_delete_is_pushed(self):
        one, two = self.c.clipboard.create(), self.c.clipboard.create()
        a, b, x = (self._ws(f"/ws/clipboard?id={i}") for i in (one, one, two))
        try:
            for ws in (a, b, x):
                self._recv(ws, "state")
            a.send(json.dumps({"type": "set", "text": "only in one", "seq": 1, "base": 0}))
            self.assertEqual(self._recv(b, "text")["text"], "only in one")
            self.assertIsNone(x.receive(timeout=0.5), "other clipboard untouched")
            self.assertEqual(self.c.clipboard.snapshot(two)[0], "")
            self.c.clipboard.delete(one)
            self._recv(a, "deleted")
            self._recv(b, "deleted")
            gone = self._ws("/ws/clipboard?id=" + one)
            self.assertEqual(json.loads(gone.receive(timeout=3))["type"], "deleted")
        finally:
            for ws in (a, b, x):
                ws.close()

    # the inherited stream tests already run in LiveWebSocketTests
    test_stream_handshake_frames_and_clipboard = None
