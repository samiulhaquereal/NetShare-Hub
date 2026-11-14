"""retcore_onlan.db: normalised schema, constraints, encryption and the migration
from the original chat.db layout."""

import sqlite3
import time

from netshare.container import build_container
from netshare.domain.chat import public_id
from netshare.infrastructure.cipher import TextCipher, TextObfuscator
from netshare.infrastructure.database import SCHEMA_VERSION, Database
from tests.helpers import AppTestCase

ALICE, BOB, EVE = "AA:00:00:00:00:01", "BB:00:00:00:00:02", "EE:00:00:00:00:03"
OLD = TextObfuscator("DefaultSecret123")

LEGACY_SCHEMA = """
CREATE TABLE users (mac TEXT PRIMARY KEY, username TEXT, avatar TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, from_mac TEXT, to_target TEXT, msg_type TEXT,
    content TEXT, filename TEXT, file_url TEXT, filetype TEXT, status TEXT DEFAULT 'sent',
    is_edited INTEGER DEFAULT 0, is_deleted INTEGER DEFAULT 0, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE custom_groups (id TEXT PRIMARY KEY, name TEXT, created_by TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE group_members (group_id TEXT, mac TEXT, PRIMARY KEY (group_id, mac));
CREATE TABLE message_reactions (msg_id INTEGER, mac TEXT, emoji TEXT, PRIMARY KEY (msg_id, mac, emoji));
CREATE TABLE pinned_chats (mac TEXT, target_id TEXT, PRIMARY KEY (mac, target_id));
CREATE TABLE read_marks (mac TEXT, target TEXT, last_id INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (mac, target));
CREATE TABLE clips (id TEXT PRIMARY KEY, content TEXT NOT NULL DEFAULT '', preview TEXT NOT NULL DEFAULT '',
    chars INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL);
"""


class MigrationTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.new_path = self.settings.db_path
        self.old_path = self.new_path.with_name("chat.db")

    def make_legacy_db(self):
        self.new_path.unlink()  # the AppTestCase container already made a fresh one
        for suffix in ("-wal", "-shm"):
            self.new_path.with_name(self.new_path.name + suffix).unlink(missing_ok=True)
        db = sqlite3.connect(self.old_path)
        db.executescript(LEGACY_SCHEMA)
        e = TextObfuscator(self.settings.chat_cipher_key).encode  # the key that install used (.env or default)
        db.executemany("INSERT INTO users VALUES (?, ?, ?)",
                       [(ALICE, "Alice", None), (BOB, "Bob", "/chat/download/avatar_b.jpg"), (EVE, "Eve", None)])
        db.execute("INSERT INTO custom_groups (id, name, created_by) VALUES ('cg_team1234', 'Team', ?)", (ALICE,))
        db.executemany("INSERT INTO group_members VALUES ('cg_team1234', ?)", [(ALICE,), (BOB,)])
        rows = [
            (1, ALICE, "group", "text", e("hello everyone"), None, None, None, "sent", 0, 0),
            (2, BOB, "group", "text", e("hi alice"), None, None, None, "sent", 1, 0),
            (3, ALICE, BOB, "text", e("private ছবি 😀"), None, None, None, "read", 0, 0),
            (4, BOB, ALICE, "text", e("reply"), None, None, None, "sent", 0, 0),
            (5, ALICE, "cg_team1234", "file", "", "plan.pdf", "/chat/download/plan.pdf", "application/pdf", "sent", 0, 0),
            (6, BOB, "group", "text", "🚫 _This message was deleted_", None, None, None, "sent", 0, 1),
            (7, EVE, "group", "text", "very old plain text", None, None, None, "sent", 0, 0),
        ]
        db.executemany("INSERT INTO messages (id, from_mac, to_target, msg_type, content, filename, file_url, filetype, "
                       "status, is_edited, is_deleted) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.executemany("INSERT INTO message_reactions VALUES (?, ?, ?)",
                       [(1, BOB, "👍"), (1, BOB, "❤️"), (2, ALICE, "😂"), (6, ALICE, "👍")])
        db.execute("INSERT INTO pinned_chats VALUES (?, 'cg_team1234')", (BOB,))
        db.executemany("INSERT INTO read_marks VALUES (?, ?, ?)", [(BOB, "group", 2), (ALICE, BOB, 4), (BOB, ALICE, 3)])
        now = time.time()
        db.execute("INSERT INTO clips VALUES ('clip1', ?, ?, 11, 3, ?, ?)", (e("wifi: abc12"), e("wifi: abc12"), now, now))
        db.commit()
        db.close()

    def test_old_chat_db_is_moved_backed_up_and_converted(self):
        self.make_legacy_db()
        c = build_container(self.settings)  # first start of the new version
        self.assertTrue(self.new_path.exists())
        self.assertFalse(self.old_path.exists(), "chat.db renamed")
        self.assertTrue(self.old_path.with_name("chat.db.backup").exists(), "untouched backup kept")

        with sqlite3.connect(self.new_path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"users", "conversations", "conversation_members", "messages", "attachments",
                             "message_reactions", "clipboards"} <= tables)
            self.assertFalse({t for t in tables if t.startswith("legacy_")} | ({"read_marks", "group_members"} & tables))
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            bodies = [r[0] for r in db.execute("SELECT body FROM messages WHERE body IS NOT NULL")]
            self.assertTrue(bodies and all(b.startswith("ENC2:") for b in bodies), "everything re-encrypted with AES")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM message_reactions WHERE message_id = 1").fetchone()[0], 1,
                             "one reaction per person")

        chat = c.chat
        glob = chat.messages(BOB, "group", None, 50)
        self.assertEqual([m["id"] for m in glob], [1, 2, 6, 7], "message ids kept")
        self.assertEqual(glob[0]["content"], "hello everyone")
        self.assertEqual(glob[1]["is_edited"], 1)
        self.assertEqual(glob[2]["is_deleted"], 1)
        self.assertEqual(glob[3]["content"], "very old plain text")
        dm = chat.messages(ALICE, public_id(BOB), None, 50)
        self.assertEqual([m["content"] for m in dm], ["private ছবি 😀", "reply"])
        self.assertEqual(dm[0]["status"], "read", "read receipt kept")
        self.assertEqual(dm[0]["to_target"], public_id(BOB))
        team = chat.messages(BOB, "cg_team1234", None, 50)
        self.assertEqual((team[0]["filename"], team[0]["file_url"]), ("plan.pdf", "/chat/download/plan.pdf"))
        self.assertEqual(chat.messages(EVE, "cg_team1234", None, 50), [], "non-member still can't read")
        self.assertEqual(chat.group_members(ALICE, "cg_team1234"), [public_id(ALICE), public_id(BOB)])

        sb = {i["id"]: i for i in chat.sidebar(BOB, 0, 50)["items"]}
        self.assertTrue(sb["cg_team1234"]["is_pinned"], "pin kept")
        self.assertEqual(sb["group"]["unread"], 1, "Bob had read up to #2; #7 is new (#6 is deleted)")
        self.assertEqual(sb[public_id(ALICE)]["unread"], 0)
        self.assertEqual(c.clipboard.snapshot("clip1"), ("wifi: abc12", 3))

        build_container(self.settings)  # second start: nothing to migrate, data unchanged
        self.assertEqual(len(chat.messages(BOB, "group", None, 50)), 4)

    def test_fresh_install(self):
        with sqlite3.connect(self.new_path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(db.execute("SELECT kind, public_id FROM conversations").fetchall(), [("global", "group")])
        self.assertFalse(self.old_path.exists())
        self.assertTrue(self.settings.db_key_path.exists(), "a random key was made")


class SchemaTests(AppTestCase):
    def test_constraints_are_enforced(self):
        db = Database(self.settings.db_path)
        with self.assertRaises(sqlite3.IntegrityError):
            with db.connect() as c:  # message in a conversation that doesn't exist
                c.execute("INSERT INTO users (device_key) VALUES ('x')")
                c.execute("INSERT INTO messages (conversation_id, sender_id, kind) VALUES (999, 1, 'text')")
        with self.assertRaises(sqlite3.IntegrityError):
            with db.connect() as c:
                c.execute("INSERT INTO conversations (kind) VALUES ('nonsense')")
        with self.assertRaises(sqlite3.IntegrityError):
            with db.connect() as c:  # a group needs a name
                c.execute("INSERT INTO conversations (kind, public_id) VALUES ('group', 'cg_x')")

    def test_deleting_a_conversation_cascades(self):
        chat = self.c.chat
        chat.set_username(ALICE, "Alice")
        chat.set_username(BOB, "Bob")
        gid = chat.create_group(ALICE, "team", [public_id(BOB)])
        chat.handle_event(ALICE, {"type": "text", "to_target": gid, "text": "hi"})
        with Database(self.settings.db_path).connect() as c:
            c.execute("DELETE FROM conversations WHERE public_id = ?", (gid,))
            left = c.execute("SELECT (SELECT COUNT(*) FROM messages m JOIN conversations v ON v.id = m.conversation_id "
                             "WHERE v.kind = 'group') + (SELECT COUNT(*) FROM conversation_members cm "
                             "LEFT JOIN conversations v ON v.id = cm.conversation_id WHERE v.id IS NULL)").fetchone()[0]
        self.assertEqual(left, 0)

    def test_direct_chat_is_one_conversation_both_ways(self):
        chat = self.c.chat
        chat.set_username(ALICE, "Alice")
        chat.set_username(BOB, "Bob")
        chat.handle_event(ALICE, {"type": "text", "to_target": public_id(BOB), "text": "a→b"})
        chat.handle_event(BOB, {"type": "text", "to_target": public_id(ALICE), "text": "b→a"})
        with Database(self.settings.db_path).connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM conversations WHERE kind = 'direct'").fetchone()[0], 1)
        before = self.c.chat._repo.max_message_id()
        chat.handle_event(ALICE, {"type": "text", "to_target": "u_deadbeef", "text": "to nobody"})
        self.assertEqual(self.c.chat._repo.max_message_id(), before, "no message to an unknown person")
        with Database(self.settings.db_path).connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM users WHERE device_key LIKE 'u_%'").fetchone()[0], 0)


class CipherTests(AppTestCase):
    def test_aes_roundtrip_and_legacy_reading(self):
        import os
        cipher = TextCipher(os.urandom(32), legacy=OLD)
        sealed = cipher.encode("hello বাংলা 😀")
        self.assertTrue(sealed.startswith("ENC2:"))
        self.assertNotEqual(sealed, cipher.encode("hello বাংলা 😀"), "random nonce each time")
        self.assertEqual(cipher.decode(sealed), "hello বাংলা 😀")
        self.assertEqual(cipher.decode(OLD.encode("old")), "old")
        self.assertEqual(cipher.decode("plain"), "plain")
        other = TextCipher(os.urandom(32))
        self.assertIn("can't be decrypted", other.decode(sealed), "wrong key never crashes")
