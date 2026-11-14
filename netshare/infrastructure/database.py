"""The app database: ``retcore_onlan.db`` (SQLite).

Schema (3rd normal form; every table has a single-purpose key, no repeating or
mixed-meaning columns):

    users                 one row per device — integer id, the device key is UNIQUE
    conversations         global chat, groups and direct chats in one table
    conversation_members  who is in a conversation + their role, read position, pin
    messages              text / file messages (text encrypted), soft delete & edit times
    attachments           the file of a file message (1:1 with messages)
    message_reactions     one reaction per person per message
    clipboards            saved clipboards (text encrypted)

Foreign keys are enforced (ON DELETE CASCADE where a row can't exist alone),
CHECK constraints keep values valid, ``PRAGMA user_version`` is the schema
version and ``migrate()`` upgrades older files — including the original
``chat.db`` layout, which is converted in one transaction after a backup copy.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

DB_FILENAME = "retcore_onlan.db"
LEGACY_FILENAMES = ("chat.db",)
SCHEMA_VERSION = 1

SCHEMA_V1 = """
CREATE TABLE users (
    id           INTEGER PRIMARY KEY,
    device_key   TEXT    NOT NULL UNIQUE,
    display_name TEXT    CHECK (display_name IS NULL OR length(display_name) BETWEEN 1 AND 60),
    avatar_url   TEXT,
    created_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE conversations (
    id          INTEGER PRIMARY KEY,
    kind        TEXT    NOT NULL CHECK (kind IN ('global', 'group', 'direct')),
    public_id   TEXT    UNIQUE,                 -- 'group' (global chat) or 'cg_…' (groups)
    title       TEXT,                           -- group name
    direct_key  TEXT    UNIQUE,                 -- '<lower user id>:<higher user id>' (direct chats)
    created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((kind = 'direct') = (direct_key IS NOT NULL)),
    CHECK ((kind = 'direct') = (public_id IS NULL)),
    CHECK (kind <> 'group' OR (title IS NOT NULL AND length(title) BETWEEN 1 AND 60))  -- length(NULL) is NULL
);

CREATE TABLE conversation_members (
    conversation_id      INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id              INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role                 TEXT    NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
    last_read_message_id INTEGER NOT NULL DEFAULT 0,
    pinned_at            TEXT,
    joined_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (conversation_id, user_id)
) WITHOUT ROWID;

CREATE TABLE messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,   -- never reused: clients page and mark by id
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    sender_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind            TEXT    NOT NULL CHECK (kind IN ('text', 'file')),
    body            TEXT,                                -- encrypted text; NULL for files and deleted messages
    created_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    edited_at       TEXT,
    deleted_at      TEXT
);

CREATE TABLE attachments (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    file_name  TEXT,
    url        TEXT NOT NULL,
    mime_type  TEXT
);

CREATE TABLE message_reactions (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    emoji      TEXT    NOT NULL CHECK (length(emoji) BETWEEN 1 AND 16),
    created_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (message_id, user_id)
) WITHOUT ROWID;

CREATE TABLE clipboards (
    id         TEXT    PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 32),
    body       TEXT    NOT NULL DEFAULT '',        -- encrypted
    preview    TEXT    NOT NULL DEFAULT '',        -- encrypted first 400 chars (grid without decrypting everything)
    char_count INTEGER NOT NULL DEFAULT 0 CHECK (char_count >= 0),
    version    INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_messages_conversation ON messages (conversation_id, id);
CREATE INDEX idx_messages_sender       ON messages (sender_id);
CREATE INDEX idx_members_user          ON conversation_members (user_id);
CREATE INDEX idx_attachments_url       ON attachments (url);
CREATE INDEX idx_clipboards_updated    ON clipboards (updated_at DESC);

INSERT INTO conversations (kind, public_id) VALUES ('global', 'group');
"""

_LEGACY_TABLES = ("users", "messages", "custom_groups", "group_members", "message_reactions",
                  "pinned_chats", "read_marks", "clips")


def utc(ts: float | None = None) -> str:
    """SQLite's CURRENT_TIMESTAMP format, for times computed in Python."""
    t = datetime.now(timezone.utc) if ts is None else datetime.fromtimestamp(ts, timezone.utc)
    return t.strftime("%Y-%m-%d %H:%M:%S")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Short-lived connection (cheap in SQLite) — never shared across threads."""
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── File name: chat.db → retcore_onlan.db ─────────────────────────
    @staticmethod
    def adopt_legacy_file(path: Path) -> Path | None:
        """Move an older database (``chat.db``) to ``path`` if ``path`` doesn't exist
        yet. Returns the backup copy that was kept, or None if nothing was moved."""
        path = Path(path)
        if path.exists():
            return None
        for name in LEGACY_FILENAMES:
            old = path.with_name(name)
            if not old.exists():
                continue
            conn = sqlite3.connect(old)  # fold the WAL into the main file first
            try:  # (``with connect()`` only commits — it doesn't close, and Windows can't move an open file)
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
            backup = old.with_name(old.name + ".backup")
            shutil.copy2(old, backup)
            os.replace(old, path)
            for suffix in ("-wal", "-shm"):
                old.with_name(old.name + suffix).unlink(missing_ok=True)
            return backup
        return None

    # ── Schema versions ────────────────────────────────────────────────
    def migrate(self, reencrypt: Callable[[str | None], str]) -> int:
        """Bring the file up to SCHEMA_VERSION. ``reencrypt`` turns a stored legacy
        value into the new encrypted form (decrypt old → encrypt new)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = OFF")  # tables are rebuilt below
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"{self.path.name} was made by a newer version of RetCore OnLAN")
            if version < 1:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    legacy = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    legacy &= set(_LEGACY_TABLES)
                    for t in legacy:
                        conn.execute(f'ALTER TABLE "{t}" RENAME TO "legacy_{t}"')
                    for idx in [r["name"] for r in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")]:
                        conn.execute(f'DROP INDEX "{idx}"')
                    # executescript() would COMMIT mid-way: run the statements one by one instead
                    for stmt in _statements(SCHEMA_V1):
                        conn.execute(stmt)
                    if legacy:
                        _import_legacy(conn, legacy, reencrypt)
                        for t in legacy:
                            conn.execute(f'DROP TABLE "legacy_{t}"')
                    problems = conn.execute("PRAGMA foreign_key_check").fetchall()
                    if problems:
                        raise RuntimeError(f"migration left {len(problems)} broken references")
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                if legacy:
                    conn.execute("VACUUM")
            return SCHEMA_VERSION
        finally:
            conn.close()


def _statements(script: str) -> list[str]:
    out, buf = [], []
    for line in script.splitlines():
        line = line.split("--", 1)[0].rstrip()
        if not line:
            continue
        buf.append(line)
        if line.endswith(";"):
            out.append("\n".join(buf))
            buf = []
    return out


def _columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _import_legacy(conn: sqlite3.Connection, legacy: set[str], reencrypt) -> None:
    """Old chat.db layout → normalised tables (ids of messages are kept)."""
    q = conn.execute
    # users: every device key that appears anywhere
    if "users" in legacy:
        q("INSERT INTO users (device_key, display_name, avatar_url) "
          "SELECT mac, NULLIF(substr(trim(username), 1, 60), ''), avatar FROM legacy_users WHERE mac IS NOT NULL")
    refs = []
    if "messages" in legacy:
        refs += ["SELECT from_mac FROM legacy_messages",
                 "SELECT to_target FROM legacy_messages WHERE to_target <> 'group' AND to_target NOT LIKE 'cg\\_%' ESCAPE '\\'"]
    if "group_members" in legacy:
        refs.append("SELECT mac FROM legacy_group_members")
    if "custom_groups" in legacy:
        refs.append("SELECT created_by FROM legacy_custom_groups")
    for sql in refs:  # OR IGNORE also skips NULL keys (NOT NULL constraint)
        q(f"INSERT OR IGNORE INTO users (device_key) SELECT DISTINCT * FROM ({sql})")
    uid = {r["device_key"]: r["id"] for r in q("SELECT id, device_key FROM users")}
    global_id = q("SELECT id FROM conversations WHERE kind='global'").fetchone()[0]

    # groups
    conv_of: dict[str, int] = {"group": global_id}
    if "custom_groups" in legacy:
        ts = "timestamp" if "timestamp" in _columns(conn, "legacy_custom_groups") else "CURRENT_TIMESTAMP"
        for g in q(f"SELECT id, name, created_by, {ts} AS ts FROM legacy_custom_groups").fetchall():
            cur = q("INSERT INTO conversations (kind, public_id, title, created_by, created_at) VALUES ('group', ?, ?, ?, ?)",
                    (g["id"], (g["name"] or "Group").strip()[:60] or "Group", uid.get(g["created_by"]), g["ts"] or utc()))
            conv_of[g["id"]] = cur.lastrowid

    def member(conv: int, user: int, role: str = "member") -> None:
        q("INSERT OR IGNORE INTO conversation_members (conversation_id, user_id, role) VALUES (?, ?, ?)", (conv, user, role))

    if "group_members" in legacy:
        admins = {r["id"]: r["created_by"] for r in q("SELECT id, created_by FROM legacy_custom_groups")} \
            if "custom_groups" in legacy else {}
        for m in q("SELECT group_id, mac FROM legacy_group_members").fetchall():
            if m["group_id"] in conv_of and m["mac"] in uid:
                member(conv_of[m["group_id"]], uid[m["mac"]], "admin" if admins.get(m["group_id"]) == m["mac"] else "member")
    for u in uid.values():  # everybody takes part in the global chat
        member(global_id, u)

    direct: dict[tuple[int, int], int] = {}

    def direct_conv(a: int, b: int) -> int:
        pair = (min(a, b), max(a, b))
        if pair not in direct:
            cur = q("INSERT INTO conversations (kind, direct_key) VALUES ('direct', ?)", (f"{pair[0]}:{pair[1]}",))
            direct[pair] = cur.lastrowid
            member(cur.lastrowid, a)
            member(cur.lastrowid, b)
        return direct[pair]

    def conversation(sender_key: str, target: str) -> int | None:
        if target in conv_of:
            return conv_of[target]
        if target and target.startswith("cg_"):
            return None  # group that no longer exists
        if sender_key in uid and target in uid:
            return direct_conv(uid[sender_key], uid[target])
        return None

    # messages (+ attachments), re-encrypted, ids kept
    read_in_dm: dict[tuple[int, int], int] = {}
    if "messages" in legacy:
        cols = _columns(conn, "legacy_messages")
        has = lambda c: c in cols  # noqa: E731
        rows = q("SELECT * FROM legacy_messages ORDER BY id").fetchall()
        for r in rows:
            conv = conversation(r["from_mac"], r["to_target"] or "group")
            sender = uid.get(r["from_mac"])
            if conv is None or sender is None:
                continue
            ts = (r["timestamp"] if has("timestamp") else None) or utc()
            deleted = bool(r["is_deleted"]) if has("is_deleted") else False
            kind = "file" if r["msg_type"] == "file" else "text"
            body = None if deleted or kind == "file" else (reencrypt(r["content"]) or "")
            edited = ts if has("is_edited") and r["is_edited"] and not deleted else None
            q("INSERT INTO messages (id, conversation_id, sender_id, kind, body, created_at, edited_at, deleted_at) "
              "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (r["id"], conv, sender, kind, body, ts, edited, ts if deleted else None))
            if kind == "file" and not deleted and has("file_url") and r["file_url"]:
                q("INSERT INTO attachments (message_id, file_name, url, mime_type) VALUES (?, ?, ?, ?)",
                  (r["id"], r["filename"], r["file_url"], r["filetype"]))
            if has("status") and r["status"] == "read" and conv in direct.values() and r["to_target"] in uid:
                key = (conv, uid[r["to_target"]])
                read_in_dm[key] = max(read_in_dm.get(key, 0), r["id"])
        q("INSERT OR REPLACE INTO sqlite_sequence (name, seq) SELECT 'messages', COALESCE(MAX(id), 0) FROM messages")

    # read positions: old read marks, read receipts, otherwise "all read" (no surprise unread pile)
    top = q("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
    if "read_marks" in legacy:
        q("UPDATE conversation_members SET last_read_message_id = 0")
        for m in q("SELECT mac, target, last_id FROM legacy_read_marks").fetchall():
            if m["mac"] not in uid:
                continue
            conv = conversation(m["mac"], m["target"]) if m["target"] not in conv_of else conv_of[m["target"]]
            if conv:
                q("UPDATE conversation_members SET last_read_message_id = MAX(last_read_message_id, ?) "
                  "WHERE conversation_id = ? AND user_id = ?", (m["last_id"] or 0, conv, uid[m["mac"]]))
    else:
        q("UPDATE conversation_members SET last_read_message_id = ?", (top,))
    for (conv, user), last in read_in_dm.items():
        q("UPDATE conversation_members SET last_read_message_id = MAX(last_read_message_id, ?) "
          "WHERE conversation_id = ? AND user_id = ?", (last, conv, user))
    for row in q("SELECT conversation_id, sender_id, MAX(id) AS last FROM messages GROUP BY conversation_id, sender_id"):
        q("UPDATE conversation_members SET last_read_message_id = MAX(last_read_message_id, ?) "
          "WHERE conversation_id = ? AND user_id = ?", (row["last"], row["conversation_id"], row["sender_id"]))

    # reactions: one per person (the old table allowed several)
    if "message_reactions" in legacy:
        q("INSERT OR IGNORE INTO message_reactions (message_id, user_id, emoji) "
          "SELECT r.msg_id, u.id, substr(r.emoji, 1, 16) FROM legacy_message_reactions r "
          "JOIN users u ON u.device_key = r.mac JOIN messages m ON m.id = r.msg_id "
          "WHERE m.deleted_at IS NULL AND length(r.emoji) > 0")

    # pins
    if "pinned_chats" in legacy:
        for p in q("SELECT mac, target_id FROM legacy_pinned_chats").fetchall():
            if p["mac"] not in uid:
                continue
            conv = conversation(p["mac"], p["target_id"])
            if conv:
                member(conv, uid[p["mac"]])
                q("UPDATE conversation_members SET pinned_at = ? WHERE conversation_id = ? AND user_id = ?",
                  (utc(), conv, uid[p["mac"]]))

    # saved clipboards
    if "clips" in legacy:
        for c in q("SELECT * FROM legacy_clips").fetchall():
            q("INSERT INTO clipboards (id, body, preview, char_count, version, created_at, updated_at) "
              "VALUES (?, ?, ?, ?, ?, ?, ?)",
              (c["id"], reencrypt(c["content"]) or "", reencrypt(c["preview"]) or "", c["chars"] or 0,
               c["version"] or 0, utc(c["created"]), utc(c["updated"])))
