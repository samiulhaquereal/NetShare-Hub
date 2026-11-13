"""SQLite persistence for chat users, groups, messages, reactions and pins.

Each operation opens a short-lived connection (cheap for SQLite) so no handle is
ever leaked across Flask's per-request threads. WAL mode lets readers proceed
while a writer commits, avoiding "database is locked" under concurrent use.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from ..domain.chat import GLOBAL_TARGET

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (mac TEXT PRIMARY KEY, username TEXT, avatar TEXT);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_mac TEXT,
    to_target TEXT,
    msg_type TEXT,
    content TEXT,
    filename TEXT,
    file_url TEXT,
    filetype TEXT,
    status TEXT DEFAULT 'sent',
    is_edited INTEGER DEFAULT 0,
    is_deleted INTEGER DEFAULT 0,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS custom_groups (
    id TEXT PRIMARY KEY, name TEXT, created_by TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS group_members (group_id TEXT, mac TEXT, PRIMARY KEY (group_id, mac));
CREATE TABLE IF NOT EXISTS message_reactions (msg_id INTEGER, mac TEXT, emoji TEXT, PRIMARY KEY (msg_id, mac, emoji));
CREATE TABLE IF NOT EXISTS pinned_chats (mac TEXT, target_id TEXT, PRIMARY KEY (mac, target_id));
CREATE INDEX IF NOT EXISTS idx_messages_target ON messages (to_target, id);
CREATE INDEX IF NOT EXISTS idx_messages_from_target ON messages (from_mac, to_target, id);
CREATE INDEX IF NOT EXISTS idx_group_members_mac ON group_members (mac);
CREATE TABLE IF NOT EXISTS read_marks (mac TEXT, target TEXT, last_id INTEGER NOT NULL DEFAULT 0,
                                       PRIMARY KEY (mac, target));
"""

_MSG_SELECT = ("SELECT m.*, u.username AS sender_name FROM messages m "
               "LEFT JOIN users u ON m.from_mac = u.mac ")


class ChatRepository:
    def __init__(self, db_path: Path):
        self._db_path = str(db_path)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ── Schema ─────────────────────────────────────────────────────────
    def init_schema(self, encode_text) -> None:
        """Create tables/indexes and obfuscate any legacy plaintext messages."""
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            fresh_marks = c.execute("SELECT 1 FROM sqlite_master WHERE name='read_marks'").fetchone() is None
            c.executescript(_SCHEMA)
            if fresh_marks:  # upgrading: existing history counts as read, not as thousands of unread
                top = c.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
                c.execute("INSERT OR IGNORE INTO read_marks SELECT mac, 'group', ? FROM users", (top,))
                c.execute("INSERT OR IGNORE INTO read_marks SELECT mac, group_id, ? FROM group_members", (top,))
                c.execute("INSERT OR IGNORE INTO read_marks SELECT DISTINCT to_target, from_mac, ? FROM messages "
                          "WHERE to_target != 'group' AND to_target NOT LIKE 'cg_%'", (top,))
            rows = c.execute("SELECT id, content FROM messages WHERE msg_type='text' AND is_deleted=0 "
                             "AND content IS NOT NULL AND content NOT LIKE 'ENC:%'").fetchall()
            for r in rows:
                c.execute("UPDATE messages SET content=? WHERE id=?", (encode_text(r["content"]), r["id"]))
        if rows:
            print(f"[Chat] Obfuscated {len(rows)} legacy plaintext messages.")

    # ── Users ──────────────────────────────────────────────────────────
    def get_user(self, key: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT mac, username, avatar FROM users WHERE mac=?", (key,)).fetchone()

    def list_users(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT mac, username, avatar FROM users").fetchall()

    def set_username(self, key: str, name: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO users (mac, username) VALUES (?, ?) "
                      "ON CONFLICT(mac) DO UPDATE SET username=excluded.username", (key, name))

    def set_avatar(self, key: str, url: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE users SET avatar=? WHERE mac=?", (url, key))

    # ── Groups ─────────────────────────────────────────────────────────
    def create_group(self, group_id: str, name: str, creator: str, members: Iterable[str]) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO custom_groups (id, name, created_by) VALUES (?, ?, ?)", (group_id, name, creator))
            c.executemany("INSERT OR IGNORE INTO group_members (group_id, mac) VALUES (?, ?)",
                          [(group_id, m) for m in members])

    def get_group(self, group_id: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM custom_groups WHERE id=?", (group_id,)).fetchone()

    def replace_group(self, group_id: str, name: str | None, members: Iterable[str]) -> set[str]:
        """Rename/replace membership atomically. Returns the previous member set."""
        with self._conn() as c:
            old = {r["mac"] for r in c.execute("SELECT mac FROM group_members WHERE group_id=?", (group_id,))}
            if name:
                c.execute("UPDATE custom_groups SET name=? WHERE id=?", (name, group_id))
            c.execute("DELETE FROM group_members WHERE group_id=?", (group_id,))
            c.executemany("INSERT OR IGNORE INTO group_members (group_id, mac) VALUES (?, ?)",
                          [(group_id, m) for m in members])
            return old

    def groups_for(self, key: str) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT cg.* FROM custom_groups cg JOIN group_members gm ON cg.id = gm.group_id "
                             "WHERE gm.mac=?", (key,)).fetchall()

    def group_members(self, group_id: str) -> list[str]:
        with self._conn() as c:
            return [r["mac"] for r in c.execute("SELECT mac FROM group_members WHERE group_id=?", (group_id,))]

    def is_member(self, group_id: str, key: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM group_members WHERE group_id=? AND mac=?",
                             (group_id, key)).fetchone() is not None

    # ── Messages ───────────────────────────────────────────────────────
    def insert_message(self, sender: str, target: str, msg_type: str, content: str,
                       filename: str | None, file_url: str | None, filetype: str | None) -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO messages (from_mac, to_target, msg_type, content, filename, file_url, filetype) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (sender, target, msg_type, content, filename, file_url, filetype))
            return int(cur.lastrowid)

    def get_message(self, msg_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(_MSG_SELECT + "WHERE m.id=?", (msg_id,)).fetchone()

    def get_own_message(self, msg_id: int, sender: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM messages WHERE id=? AND from_mac=?", (msg_id, sender)).fetchone()

    def edit_message(self, msg_id: int, content: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE messages SET content=?, is_edited=1 WHERE id=?", (content, msg_id))

    def delete_message(self, msg_id: int, tombstone: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE messages SET content=?, is_edited=0, is_deleted=1, file_url=NULL, filename=NULL "
                      "WHERE id=?", (tombstone, msg_id))

    def mark_read(self, sender: str, reader: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE messages SET status='read' WHERE from_mac=? AND to_target=? AND status='sent'",
                      (sender, reader))

    def list_messages(self, me: str, target: str, before_id: int | None, limit: int) -> list[sqlite3.Row]:
        where, params = self._conversation_filter(me, target)
        if before_id:
            where += " AND m.id < ?"
            params.append(before_id)
        params.append(limit)
        with self._conn() as c:
            return c.execute(_MSG_SELECT + "WHERE " + where + " ORDER BY m.id DESC LIMIT ?", params).fetchall()

    def list_messages_after(self, me: str, target: str, after_id: int, limit: int) -> list[sqlite3.Row]:
        """Oldest first: what arrived in a conversation after ``after_id``."""
        where, params = self._conversation_filter(me, target)
        params += [after_id, limit]
        with self._conn() as c:
            return c.execute(_MSG_SELECT + "WHERE " + where + " AND m.id > ? ORDER BY m.id ASC LIMIT ?",
                             params).fetchall()

    def count_file_refs(self, file_url: str) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM messages WHERE file_url=? AND is_deleted=0",
                             (file_url,)).fetchone()[0]

    @staticmethod
    def _conversation_filter(me: str, target: str) -> tuple[str, list]:
        if target == GLOBAL_TARGET or target.startswith("cg_"):
            return "m.to_target=?", [target]
        return "((m.from_mac=? AND m.to_target=?) OR (m.from_mac=? AND m.to_target=?))", [me, target, target, me]

    def reactions_for(self, msg_ids: list[int]) -> dict[int, list[tuple[str, str]]]:
        """Batch-load reactions: {msg_id: [(emoji, key), ...]} (one query, not N)."""
        if not msg_ids:
            return {}
        marks = ",".join("?" * len(msg_ids))
        out: dict[int, list[tuple[str, str]]] = {}
        with self._conn() as c:
            for r in c.execute(f"SELECT msg_id, emoji, mac FROM message_reactions WHERE msg_id IN ({marks})", msg_ids):
                out.setdefault(r["msg_id"], []).append((r["emoji"], r["mac"]))
        return out

    def add_reaction(self, msg_id: int, key: str, emoji: str) -> None:
        """One reaction per person per message: a new emoji replaces the old one."""
        with self._conn() as c:
            c.execute("DELETE FROM message_reactions WHERE msg_id=? AND mac=?", (msg_id, key))
            c.execute("INSERT INTO message_reactions (msg_id, mac, emoji) VALUES (?, ?, ?)", (msg_id, key, emoji))

    def clear_reactions(self, msg_id: int, key: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM message_reactions WHERE msg_id=? AND mac=?", (msg_id, key))

    def last_message(self, target: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT id, content, msg_type, is_deleted FROM messages WHERE to_target=? "
                             "ORDER BY id DESC LIMIT 1", (target,)).fetchone()

    def last_messages(self, targets: list[str]) -> dict[str, sqlite3.Row]:
        """Latest message per group target, in one query."""
        if not targets:
            return {}
        marks = ",".join("?" * len(targets))
        sql = (f"SELECT m.id, m.to_target, m.from_mac, m.content, m.msg_type, m.is_deleted, m.timestamp FROM messages m "
               f"JOIN (SELECT to_target, MAX(id) AS mid FROM messages WHERE to_target IN ({marks}) "
               f"GROUP BY to_target) t ON m.id = t.mid")
        with self._conn() as c:
            return {r["to_target"]: r for r in c.execute(sql, targets)}

    def direct_message_heads(self, me: str) -> list[sqlite3.Row]:
        """Newest-first DMs involving `me` (caller keeps the first per peer)."""
        with self._conn() as c:
            return c.execute("SELECT id, from_mac, to_target, content, msg_type, is_deleted, timestamp FROM messages "
                             "WHERE (from_mac=? AND to_target != 'group' AND to_target NOT LIKE 'cg_%') "
                             "OR to_target=? ORDER BY id DESC", (me, me)).fetchall()

    def searchable_messages(self, me: str, group_ids: list[str], scan_limit: int = 5000) -> list[sqlite3.Row]:
        """Text messages visible to `me`, newest first (content still obfuscated)."""
        marks = ",".join("?" * len(group_ids)) or "''"
        sql = (_MSG_SELECT + f"WHERE (m.to_target='group' OR m.to_target IN ({marks}) "
               "OR m.from_mac=? OR m.to_target=?) AND m.msg_type='text' AND m.is_deleted=0 "
               "ORDER BY m.id DESC LIMIT ?")
        with self._conn() as c:
            return c.execute(sql, [*group_ids, me, me, scan_limit]).fetchall()

    # ── Read marks (unread counts that survive a reload) ───────────────
    def set_read_mark(self, key: str, target: str, last_id: int) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO read_marks (mac, target, last_id) VALUES (?, ?, ?) "
                      "ON CONFLICT(mac, target) DO UPDATE SET last_id=MAX(last_id, excluded.last_id)",
                      (key, target, last_id))

    def max_message_id(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]

    def unread_in_rooms(self, key: str, targets: list[str]) -> dict[str, int]:
        """Global chat / groups: messages from others after my read mark."""
        if not targets:
            return {}
        marks = ",".join("?" * len(targets))
        sql = (f"SELECT m.to_target AS t, COUNT(*) AS n FROM messages m "
               f"LEFT JOIN read_marks r ON r.mac = ? AND r.target = m.to_target "
               f"WHERE m.to_target IN ({marks}) AND m.from_mac != ? AND m.is_deleted = 0 "
               f"AND m.id > COALESCE(r.last_id, 0) GROUP BY m.to_target")
        with self._conn() as c:
            return {r["t"]: r["n"] for r in c.execute(sql, [key, *targets, key])}

    def unread_direct(self, key: str) -> dict[str, int]:
        """Direct messages to me, per sender, after my read mark for that person."""
        sql = ("SELECT m.from_mac AS t, COUNT(*) AS n FROM messages m "
               "LEFT JOIN read_marks r ON r.mac = ? AND r.target = m.from_mac "
               "WHERE m.to_target = ? AND m.is_deleted = 0 AND m.id > COALESCE(r.last_id, 0) GROUP BY m.from_mac")
        with self._conn() as c:
            return {r["t"]: r["n"] for r in c.execute(sql, (key, key))}

    def group_member_counts(self, group_ids: list[str]) -> dict[str, int]:
        if not group_ids:
            return {}
        marks = ",".join("?" * len(group_ids))
        with self._conn() as c:
            return {r[0]: r[1] for r in c.execute(
                f"SELECT group_id, COUNT(*) FROM group_members WHERE group_id IN ({marks}) GROUP BY group_id", group_ids)}

    # ── Pins ───────────────────────────────────────────────────────────
    def pin(self, key: str, target: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO pinned_chats (mac, target_id) VALUES (?, ?)", (key, target))

    def unpin(self, key: str, target: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM pinned_chats WHERE mac=? AND target_id=?", (key, target))

    def pinned_for(self, key: str) -> set[str]:
        with self._conn() as c:
            return {r["target_id"] for r in c.execute("SELECT target_id FROM pinned_chats WHERE mac=?", (key,))}
