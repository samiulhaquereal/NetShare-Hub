"""Chat persistence on the normalised schema (see ``database.py``).

The service speaks in *device keys* and *targets* — ``'group'`` (global chat),
``'cg_…'`` (a group) or another device key (a direct chat). This repository maps
those onto ``users`` / ``conversations`` / ``conversation_members`` and returns
message rows with the field names the service and API have always used
(``from_mac``, ``to_target``, ``msg_type``, ``content``, ``file_url`` …), so the
storage could be normalised without changing anything above it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from ..domain.chat import CUSTOM_GROUP_PREFIX, DELETED_TEXT, GLOBAL_TARGET
from .database import Database, utc

_TOMBSTONE = DELETED_TEXT.replace("'", "''")

# A message row in the shape the service expects.
_MSG_SELECT = f"""
SELECT m.id,
       su.device_key AS from_mac,
       CASE c.kind WHEN 'direct' THEN COALESCE(
                (SELECT u2.device_key FROM conversation_members cm2 JOIN users u2 ON u2.id = cm2.user_id
                 WHERE cm2.conversation_id = m.conversation_id AND cm2.user_id <> m.sender_id LIMIT 1),
                su.device_key)
            ELSE c.public_id END AS to_target,
       m.kind AS msg_type,
       CASE WHEN m.deleted_at IS NOT NULL THEN '{_TOMBSTONE}' ELSE m.body END AS content,
       a.file_name AS filename, a.url AS file_url, a.mime_type AS filetype,
       CASE WHEN c.kind = 'direct' AND EXISTS (
                SELECT 1 FROM conversation_members r
                WHERE r.conversation_id = m.conversation_id AND r.user_id <> m.sender_id
                  AND r.last_read_message_id >= m.id)
            THEN 'read' ELSE 'sent' END AS status,
       (m.edited_at IS NOT NULL AND m.deleted_at IS NULL) AS is_edited,
       (m.deleted_at IS NOT NULL) AS is_deleted,
       m.created_at AS timestamp,
       su.display_name AS sender_name
FROM messages m
JOIN users su ON su.id = m.sender_id
JOIN conversations c ON c.id = m.conversation_id
LEFT JOIN attachments a ON a.message_id = m.id
"""


class ChatRepository:
    def __init__(self, db: Database | Path):
        self._db = db if isinstance(db, Database) else Database(Path(db))

    def _conn(self):
        return self._db.connect()

    def init_schema(self, _encode=None) -> None:
        """Kept for callers; the schema is created / migrated by ``Database.migrate``."""

    # ── Lookups ────────────────────────────────────────────────────────
    @staticmethod
    def _user_id(c: sqlite3.Connection, key: str, create: bool = False) -> int | None:
        row = c.execute("SELECT id FROM users WHERE device_key = ?", (key,)).fetchone()
        if row:
            return row[0]
        if not create or not key:
            return None
        return c.execute("INSERT INTO users (device_key) VALUES (?)", (key,)).lastrowid

    @staticmethod
    def _global_id(c: sqlite3.Connection) -> int:
        return c.execute("SELECT id FROM conversations WHERE kind = 'global'").fetchone()[0]

    def _conversation(self, c: sqlite3.Connection, me: str, target: str, create: bool = False) -> int | None:
        """Conversation id for ``target`` as seen by ``me`` (None if it doesn't exist)."""
        if not target or target == GLOBAL_TARGET:
            conv = self._global_id(c)
            if create:
                self._join(c, conv, self._user_id(c, me, create=True))
            return conv
        if target.startswith(CUSTOM_GROUP_PREFIX):
            row = c.execute("SELECT id FROM conversations WHERE public_id = ? AND kind = 'group'", (target,)).fetchone()
            return row[0] if row else None
        a, b = self._user_id(c, me, create), self._user_id(c, target, create)
        if a is None or b is None:
            return None
        key = f"{min(a, b)}:{max(a, b)}"
        row = c.execute("SELECT id FROM conversations WHERE direct_key = ?", (key,)).fetchone()
        if row:
            return row[0]
        if not create:
            return None
        conv = c.execute("INSERT INTO conversations (kind, direct_key) VALUES ('direct', ?)", (key,)).lastrowid
        self._join(c, conv, a)
        self._join(c, conv, b)
        return conv

    @staticmethod
    def _join(c: sqlite3.Connection, conv: int, user: int, role: str = "member", last_read: int = 0) -> None:
        c.execute("INSERT OR IGNORE INTO conversation_members (conversation_id, user_id, role, last_read_message_id) "
                  "VALUES (?, ?, ?, ?)", (conv, user, role, last_read))

    def _target_of(self, c: sqlite3.Connection, me_id: int, conv: int) -> str:
        """The service's target string for a conversation, from ``me``'s point of view."""
        row = c.execute("SELECT kind, public_id FROM conversations WHERE id = ?", (conv,)).fetchone()
        if row["kind"] != "direct":
            return row["public_id"]
        peer = c.execute("SELECT u.device_key FROM conversation_members cm JOIN users u ON u.id = cm.user_id "
                         "WHERE cm.conversation_id = ? AND cm.user_id <> ?", (conv, me_id)).fetchone()
        return peer[0] if peer else ""

    # ── Users ──────────────────────────────────────────────────────────
    _USER_COLS = "device_key AS mac, display_name AS username, avatar_url AS avatar"

    def get_user(self, key: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(f"SELECT {self._USER_COLS} FROM users WHERE device_key = ?", (key,)).fetchone()

    def list_users(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(f"SELECT {self._USER_COLS} FROM users ORDER BY id").fetchall()

    def set_username(self, key: str, name: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO users (device_key, display_name) VALUES (?, ?) "
                      "ON CONFLICT(device_key) DO UPDATE SET display_name = excluded.display_name, "
                      "updated_at = CURRENT_TIMESTAMP", (key, name))

    def set_avatar(self, key: str, url: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE users SET avatar_url = ?, updated_at = CURRENT_TIMESTAMP WHERE device_key = ?", (url, key))

    # ── Groups ─────────────────────────────────────────────────────────
    _GROUP_SELECT = ("SELECT g.public_id AS id, g.title AS name, cu.device_key AS created_by, g.created_at AS timestamp "
                     "FROM conversations g LEFT JOIN users cu ON cu.id = g.created_by ")

    def create_group(self, group_id: str, name: str, creator: str, members: Iterable[str]) -> None:
        with self._conn() as c:
            creator_id = self._user_id(c, creator, create=True)
            conv = c.execute("INSERT INTO conversations (kind, public_id, title, created_by) VALUES ('group', ?, ?, ?)",
                             (group_id, name, creator_id)).lastrowid
            for key in set(members) | {creator}:
                user = self._user_id(c, key, create=True)
                self._join(c, conv, user, "admin" if key == creator else "member")

    def get_group(self, group_id: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(self._GROUP_SELECT + "WHERE g.kind = 'group' AND g.public_id = ?", (group_id,)).fetchone()

    def replace_group(self, group_id: str, name: str | None, members: Iterable[str]) -> set[str]:
        """Rename / set membership in one transaction. Members who stay keep their read
        position and pins. Returns the previous member set."""
        with self._conn() as c:
            conv = c.execute("SELECT id, created_by FROM conversations WHERE public_id = ? AND kind = 'group'",
                             (group_id,)).fetchone()
            if not conv:
                return set()
            old = {r[0]: r[1] for r in c.execute(
                "SELECT u.device_key, u.id FROM conversation_members cm JOIN users u ON u.id = cm.user_id "
                "WHERE cm.conversation_id = ?", (conv["id"],))}
            if name:
                c.execute("UPDATE conversations SET title = ? WHERE id = ?", (name, conv["id"]))
            wanted = {key: self._user_id(c, key, create=True) for key in set(members)}
            for key, user in old.items():
                if key not in wanted:
                    c.execute("DELETE FROM conversation_members WHERE conversation_id = ? AND user_id = ?", (conv["id"], user))
            for key, user in wanted.items():
                self._join(c, conv["id"], user, "admin" if user == conv["created_by"] else "member")
            return set(old)

    def groups_for(self, key: str) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(self._GROUP_SELECT +
                             "JOIN conversation_members cm ON cm.conversation_id = g.id "
                             "JOIN users me ON me.id = cm.user_id "
                             "WHERE g.kind = 'group' AND me.device_key = ? ORDER BY g.id", (key,)).fetchall()

    def group_members(self, group_id: str) -> list[str]:
        with self._conn() as c:
            return [r[0] for r in c.execute(
                "SELECT u.device_key FROM conversations g JOIN conversation_members cm ON cm.conversation_id = g.id "
                "JOIN users u ON u.id = cm.user_id WHERE g.public_id = ? AND g.kind = 'group' ORDER BY cm.joined_at",
                (group_id,))]

    def is_member(self, group_id: str, key: str) -> bool:
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM conversations g JOIN conversation_members cm ON cm.conversation_id = g.id "
                "JOIN users u ON u.id = cm.user_id WHERE g.public_id = ? AND g.kind = 'group' AND u.device_key = ?",
                (group_id, key)).fetchone() is not None

    def group_member_counts(self, group_ids: list[str]) -> dict[str, int]:
        if not group_ids:
            return {}
        marks = ",".join("?" * len(group_ids))
        with self._conn() as c:
            return {r[0]: r[1] for r in c.execute(
                f"SELECT g.public_id, COUNT(*) FROM conversations g JOIN conversation_members cm ON cm.conversation_id = g.id "
                f"WHERE g.public_id IN ({marks}) GROUP BY g.id", group_ids)}

    # ── Messages ───────────────────────────────────────────────────────
    def insert_message(self, sender: str, target: str, msg_type: str, content: str,
                       filename: str | None, file_url: str | None, filetype: str | None) -> int:
        with self._conn() as c:
            sender_id = self._user_id(c, sender, create=True)
            conv = self._conversation(c, sender, target, create=True)
            if conv is None:
                raise ValueError(f"unknown conversation {target!r}")
            msg_id = c.execute("INSERT INTO messages (conversation_id, sender_id, kind, body) VALUES (?, ?, ?, ?)",
                               (conv, sender_id, msg_type, content if msg_type == "text" else None)).lastrowid
            if msg_type == "file" and file_url:
                c.execute("INSERT INTO attachments (message_id, file_name, url, mime_type) VALUES (?, ?, ?, ?)",
                          (msg_id, filename, file_url, filetype))
            # sending means you've seen the conversation up to here
            c.execute("UPDATE conversation_members SET last_read_message_id = MAX(last_read_message_id, ?) "
                      "WHERE conversation_id = ? AND user_id = ?", (msg_id, conv, sender_id))
            return int(msg_id)

    def get_message(self, msg_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(_MSG_SELECT + "WHERE m.id = ?", (msg_id,)).fetchone()

    def get_own_message(self, msg_id: int, sender: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(_MSG_SELECT + "WHERE m.id = ? AND su.device_key = ?", (msg_id, sender)).fetchone()

    def edit_message(self, msg_id: int, content: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE messages SET body = ?, edited_at = CURRENT_TIMESTAMP "
                      "WHERE id = ? AND kind = 'text' AND deleted_at IS NULL", (content, msg_id))

    def delete_message(self, msg_id: int, _tombstone: str = DELETED_TEXT) -> None:
        """Soft delete: the text, the attachment row and reactions are removed for good."""
        with self._conn() as c:
            c.execute("UPDATE messages SET body = NULL, deleted_at = CURRENT_TIMESTAMP WHERE id = ?", (msg_id,))
            c.execute("DELETE FROM attachments WHERE message_id = ?", (msg_id,))
            c.execute("DELETE FROM message_reactions WHERE message_id = ?", (msg_id,))

    def mark_read(self, sender: str, reader: str) -> None:
        """``reader`` has read the direct chat with ``sender`` up to now."""
        with self._conn() as c:
            conv = self._conversation(c, reader, sender)
            reader_id = self._user_id(c, reader)
            if conv and reader_id:
                c.execute("UPDATE conversation_members SET last_read_message_id = "
                          "MAX(last_read_message_id, (SELECT COALESCE(MAX(id), 0) FROM messages WHERE conversation_id = ?)) "
                          "WHERE conversation_id = ? AND user_id = ?", (conv, conv, reader_id))

    def list_messages(self, me: str, target: str, before_id: int | None, limit: int) -> list[sqlite3.Row]:
        """Newest first (the service reverses)."""
        with self._conn() as c:
            conv = self._conversation(c, me, target)
            if conv is None:
                return []
            sql, args = _MSG_SELECT + "WHERE m.conversation_id = ?", [conv]
            if before_id:
                sql += " AND m.id < ?"
                args.append(before_id)
            return c.execute(sql + " ORDER BY m.id DESC LIMIT ?", args + [limit]).fetchall()

    def list_messages_after(self, me: str, target: str, after_id: int, limit: int) -> list[sqlite3.Row]:
        """Oldest first: what arrived after ``after_id``."""
        with self._conn() as c:
            conv = self._conversation(c, me, target)
            if conv is None:
                return []
            return c.execute(_MSG_SELECT + "WHERE m.conversation_id = ? AND m.id > ? ORDER BY m.id ASC LIMIT ?",
                             (conv, after_id, limit)).fetchall()

    def last_messages(self, targets: list[str]) -> dict[str, sqlite3.Row]:
        """Latest message of the global chat / each group, in one query."""
        if not targets:
            return {}
        marks = ",".join("?" * len(targets))
        with self._conn() as c:
            rows = c.execute(_MSG_SELECT + f"WHERE m.id IN (SELECT MAX(m2.id) FROM messages m2 "
                             f"JOIN conversations c2 ON c2.id = m2.conversation_id "
                             f"WHERE c2.public_id IN ({marks}) GROUP BY m2.conversation_id)", targets).fetchall()
        return {r["to_target"]: r for r in rows}

    def direct_message_heads(self, me: str) -> list[sqlite3.Row]:
        """Latest message of each of my direct chats, newest first."""
        with self._conn() as c:
            return c.execute(_MSG_SELECT + "WHERE m.id IN (SELECT MAX(m2.id) FROM messages m2 "
                             "JOIN conversations c2 ON c2.id = m2.conversation_id AND c2.kind = 'direct' "
                             "JOIN conversation_members cm ON cm.conversation_id = c2.id "
                             "JOIN users me ON me.id = cm.user_id AND me.device_key = ? "
                             "GROUP BY m2.conversation_id) ORDER BY m.id DESC", (me,)).fetchall()

    def searchable_messages(self, me: str, _group_ids: list[str] | None = None, scan_limit: int = 5000) -> list[sqlite3.Row]:
        """Text messages ``me`` can see (global, my groups, my direct chats), newest first."""
        with self._conn() as c:
            return c.execute(_MSG_SELECT + "WHERE m.kind = 'text' AND m.deleted_at IS NULL AND ("
                             "c.kind = 'global' OR m.conversation_id IN (SELECT cm.conversation_id "
                             "FROM conversation_members cm JOIN users u ON u.id = cm.user_id WHERE u.device_key = ?)) "
                             "ORDER BY m.id DESC LIMIT ?", (me, scan_limit)).fetchall()

    def count_file_refs(self, file_url: str) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM attachments a JOIN messages m ON m.id = a.message_id "
                             "WHERE a.url = ? AND m.deleted_at IS NULL", (file_url,)).fetchone()[0]

    def max_message_id(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]

    # ── Reactions ──────────────────────────────────────────────────────
    def reactions_for(self, msg_ids: list[int]) -> dict[int, list[tuple[str, str]]]:
        """{message id: [(emoji, device key), …]} in one query."""
        if not msg_ids:
            return {}
        marks = ",".join("?" * len(msg_ids))
        out: dict[int, list[tuple[str, str]]] = {}
        with self._conn() as c:
            for r in c.execute(f"SELECT r.message_id, r.emoji, u.device_key FROM message_reactions r "
                               f"JOIN users u ON u.id = r.user_id WHERE r.message_id IN ({marks}) "
                               f"ORDER BY r.created_at", msg_ids):
                out.setdefault(r[0], []).append((r[1], r[2]))
        return out

    def add_reaction(self, msg_id: int, key: str, emoji: str) -> None:
        """One reaction per person per message: a new emoji replaces the old one."""
        with self._conn() as c:
            user = self._user_id(c, key, create=True)
            c.execute("INSERT INTO message_reactions (message_id, user_id, emoji) VALUES (?, ?, ?) "
                      "ON CONFLICT(message_id, user_id) DO UPDATE SET emoji = excluded.emoji, "
                      "created_at = CURRENT_TIMESTAMP", (msg_id, user, emoji))

    def clear_reactions(self, msg_id: int, key: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM message_reactions WHERE message_id = ? AND user_id = "
                      "(SELECT id FROM users WHERE device_key = ?)", (msg_id, key))

    # ── Pins & read positions ──────────────────────────────────────────
    def _membership(self, c, key: str, target: str) -> tuple[int, int] | None:
        """(conversation, user) for ``key``'s own membership row, created for the global
        chat and direct chats (groups only for members)."""
        user = self._user_id(c, key, create=True)
        conv = self._conversation(c, key, target, create=not target.startswith(CUSTOM_GROUP_PREFIX))
        if conv is None:
            return None
        if not target.startswith(CUSTOM_GROUP_PREFIX):
            self._join(c, conv, user)
        return conv, user

    def pin(self, key: str, target: str) -> None:
        with self._conn() as c:
            m = self._membership(c, key, target)
            if m:
                c.execute("UPDATE conversation_members SET pinned_at = ? WHERE conversation_id = ? AND user_id = ?",
                          (utc(), *m))

    def unpin(self, key: str, target: str) -> None:
        with self._conn() as c:
            conv, user = self._conversation(c, key, target), self._user_id(c, key)
            if conv and user:
                c.execute("UPDATE conversation_members SET pinned_at = NULL WHERE conversation_id = ? AND user_id = ?",
                          (conv, user))

    def pinned_for(self, key: str) -> set[str]:
        with self._conn() as c:
            me = self._user_id(c, key)
            if me is None:
                return set()
            convs = [r[0] for r in c.execute("SELECT conversation_id FROM conversation_members "
                                             "WHERE user_id = ? AND pinned_at IS NOT NULL", (me,))]
            return {self._target_of(c, me, conv) for conv in convs} - {""}

    def set_read_mark(self, key: str, target: str, last_id: int) -> None:
        with self._conn() as c:
            m = self._membership(c, key, target)
            if m:
                c.execute("UPDATE conversation_members SET last_read_message_id = MAX(last_read_message_id, ?) "
                          "WHERE conversation_id = ? AND user_id = ?", (last_id, *m))

    def unread_in_rooms(self, key: str, targets: list[str]) -> dict[str, int]:
        """Global chat / groups: messages from others after my read position."""
        if not targets:
            return {}
        marks = ",".join("?" * len(targets))
        with self._conn() as c:
            me = self._user_id(c, key)
            if me is None:
                return {}
            return {r[0]: r[1] for r in c.execute(
                f"SELECT cv.public_id, COUNT(*) FROM messages m JOIN conversations cv ON cv.id = m.conversation_id "
                f"LEFT JOIN conversation_members cm ON cm.conversation_id = cv.id AND cm.user_id = ? "
                f"WHERE cv.public_id IN ({marks}) AND m.sender_id <> ? AND m.deleted_at IS NULL "
                f"AND m.id > COALESCE(cm.last_read_message_id, 0) GROUP BY cv.id", [me, *targets, me])}

    def unread_direct(self, key: str) -> dict[str, int]:
        """Direct chats: unread messages per person."""
        with self._conn() as c:
            me = self._user_id(c, key)
            if me is None:
                return {}
            return {r[0]: r[1] for r in c.execute(
                "SELECT su.device_key, COUNT(*) FROM conversation_members cm "
                "JOIN conversations cv ON cv.id = cm.conversation_id AND cv.kind = 'direct' "
                "JOIN messages m ON m.conversation_id = cv.id AND m.sender_id <> cm.user_id "
                "JOIN users su ON su.id = m.sender_id "
                "WHERE cm.user_id = ? AND m.deleted_at IS NULL AND m.id > cm.last_read_message_id "
                "GROUP BY m.sender_id", (me,))}
