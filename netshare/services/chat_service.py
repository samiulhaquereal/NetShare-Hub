"""Messenger use-cases: profiles, groups, conversations, reactions, pins,
search and the real-time /ws/chat event handling.

The service talks in *device keys* internally and converts to *public ids*
(``u_xxxxxxxx``) at the boundary, so raw MAC/IP values never reach clients.
Message text is de-obfuscated here; browsers receive plain text and must render
it with textContent (never innerHTML).
"""

from __future__ import annotations

import json
import threading
import uuid
from collections import deque
from typing import Any

from ..domain.chat import (CUSTOM_GROUP_PREFIX, DELETED_TEXT, GLOBAL_TARGET, MESSAGE_TYPES,
                           is_group_target, public_id)
from ..infrastructure.chat_repository import ChatRepository
from ..infrastructure.cipher import TextObfuscator

FILE_URL_PREFIX = "/chat/download/"
MAX_TEXT = 20_000
MAX_NAME = 60
SEARCH_LIMIT = 50


class ChatConnection:
    """A /ws/chat socket with its own outgoing queue and sender thread.

    Broadcasts only *enqueue*: a phone that stopped reading (screen locked, Wi-Fi
    gone) can't hold up delivery to everybody else. If its queue overflows the
    connection is closed; the page reconnects and catches up from the server."""

    MAX_QUEUE = 1000

    def __init__(self, ws):
        self._ws = ws
        self._cond = threading.Condition()
        self._queue: deque[str] = deque()
        self._sending = False
        self._thread: threading.Thread | None = None
        self.closed = False

    def send(self, msg: str) -> None:
        with self._cond:
            if self.closed:
                return
            if len(self._queue) >= self.MAX_QUEUE:
                self._close_locked()
                return
            self._queue.append(msg)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="ws-chat-sender")
                self._thread.start()
            self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self.closed:
                    self._cond.wait()
                if self.closed:
                    return
                batch = list(self._queue)
                self._queue.clear()
                self._sending = True
            try:
                for msg in batch:
                    self._ws.send(msg)
            except Exception:
                self.close()
                return
            finally:
                with self._cond:
                    self._sending = False
                    self._cond.notify_all()

    def drain(self, timeout: float = 2.0) -> bool:
        """Wait until everything queued so far was handed to the socket (tests)."""
        with self._cond:
            return self._cond.wait_for(lambda: self.closed or (not self._queue and not self._sending), timeout)

    def _close_locked(self) -> None:
        self.closed = True
        self._queue.clear()
        self._cond.notify_all()
        try:
            self._ws.close()
        except Exception:
            pass

    def close(self) -> None:
        with self._cond:
            if not self.closed:
                self._close_locked()


class ChatHub:
    def __init__(self):
        self._by_key: dict[str, set[ChatConnection]] = {}
        self._lock = threading.Lock()

    def add(self, key: str, conn: ChatConnection) -> bool:
        """True when this is the user's first open connection (they just came online)."""
        with self._lock:
            conns = self._by_key.setdefault(key, set())
            first = not conns
            conns.add(conn)
            return first

    def remove(self, key: str, conn: ChatConnection) -> bool:
        """True when the user's last connection closed (they went offline)."""
        with self._lock:
            conns = self._by_key.get(key)
            if not conns or conn not in conns:
                return False
            conns.discard(conn)
            if not conns:
                del self._by_key[key]
                return True
            return False

    def active_keys(self) -> set[str]:
        with self._lock:
            return set(self._by_key)

    def send_to(self, key: str, msg: str) -> None:
        with self._lock:
            conns = list(self._by_key.get(key, ()))
        for c in conns:
            c.send(msg)

    def broadcast(self, msg: str, skip: str | None = None) -> None:
        with self._lock:
            conns = [c for k, cs in self._by_key.items() if k != skip for c in cs]
        for c in conns:
            c.send(msg)


def _system(action: str) -> str:
    return json.dumps({"type": "system", "action": action})


class ChatService:
    def __init__(self, repo: ChatRepository, cipher: TextObfuscator, hub: ChatHub, files=None):
        self._repo = repo
        self._cipher = cipher
        self.hub = hub
        self._files = files  # chat attachment store: deleted photos/voice notes are removed
        self._public_to_key: dict[str, str] = {}
        self._map_lock = threading.Lock()

    def init(self) -> None:
        self._repo.init_schema(self._cipher.encode)

    # ── Identity mapping ──────────────────────────────────────────────
    def _remember(self, key: str) -> str:
        pid = public_id(key)
        if pid and pid != key:
            with self._map_lock:
                self._public_to_key[pid] = key
        return pid

    def resolve(self, pid: str | None) -> str:
        """Public id (or group id) -> internal key."""
        if not pid or is_group_target(pid):
            return pid or GLOBAL_TARGET
        with self._map_lock:
            key = self._public_to_key.get(pid)
        if key:
            return key
        for row in self._repo.list_users():  # cold cache (e.g. after restart)
            self._remember(row["mac"])
        for k in self.hub.active_keys():
            self._remember(k)
        with self._map_lock:
            return self._public_to_key.get(pid, pid)

    # ── Serialisation ─────────────────────────────────────────────────
    def _preview(self, row, me: str | None = None, names: dict[str, str] | None = None) -> dict:
        """Sidebar line: text, when, and who sent it ("You: …" / "Rina: …")."""
        if row is None:
            return {"text": "No messages yet", "time_id": -1, "ts": None, "mine": False, "sender": None}
        if row["is_deleted"]:
            text = "🚫 Deleted"
        elif row["msg_type"] == "text":
            text = self._cipher.decode(row["content"])
        else:
            text = "📎 Attachment"
        keys = row.keys()
        sender = row["from_mac"] if "from_mac" in keys else None
        return {"text": text[:200], "time_id": row["id"], "ts": row["timestamp"] if "timestamp" in keys else None,
                "mine": bool(me and sender == me), "sender": (names or {}).get(sender)}

    def _serialize(self, row, reactions: list[tuple[str, str]] | None = None) -> dict:
        d = dict(row)
        d["content"] = self._cipher.decode(d.get("content"))
        d["from_mac"] = self._remember(d["from_mac"])
        d["to_target"] = self._remember(d["to_target"])
        grouped: dict[str, list[str]] = {}
        for emoji, key in reactions or ():
            grouped.setdefault(emoji, []).append(self._remember(key))
        d["reactions"] = grouped
        return d

    # ── Profiles ──────────────────────────────────────────────────────
    def profile(self, me: str) -> dict:
        row = self._repo.get_user(me)
        return {"username": row["username"] if row else None, "mac": self._remember(me),
                "avatar": row["avatar"] if row else None}

    def set_username(self, me: str, name: str) -> dict:
        name = (name or "").strip()[:MAX_NAME]
        if name:
            if self._repo.get_user(me) is None:  # newcomer: the existing chat history isn't "unread"
                self._repo.set_read_mark(me, GLOBAL_TARGET, self._repo.max_message_id())
            self._repo.set_username(me, name)
            self.hub.broadcast(_system("update_users"))
        return {"status": "ok", "mac": self._remember(me), "username": name or None}

    def set_avatar(self, me: str, url: str) -> None:
        old = self._repo.get_user(me)
        self._repo.set_avatar(me, url)
        old_url = old["avatar"] if old else None
        if old_url and old_url != url and old_url.startswith(FILE_URL_PREFIX + "avatar_"):
            self._delete_file(old_url[len(FILE_URL_PREFIX):])
        self.hub.broadcast(_system("update_users"))

    def users(self) -> list[dict]:
        active = self.hub.active_keys()
        return [{"mac": self._remember(r["mac"]), "username": r["username"], "avatar": r["avatar"],
                 "is_active": r["mac"] in active} for r in self._repo.list_users()]

    # ── Groups ────────────────────────────────────────────────────────
    def groups(self, me: str) -> list[dict]:
        out = []
        for r in self._repo.groups_for(me):
            d = dict(r)
            d["created_by"] = self._remember(d.get("created_by"))
            out.append(d)
        return out

    def create_group(self, me: str, name: str, member_ids: list[str]) -> str:
        members = {self.resolve(m) for m in member_ids or []} | {me}
        group_id = f"{CUSTOM_GROUP_PREFIX}{uuid.uuid4().hex[:8]}"
        self._repo.create_group(group_id, (name or "Unnamed Group").strip()[:MAX_NAME], me, members)
        self._notify_keys(members, _system("update_groups"))
        return group_id

    def update_group(self, me: str, group_id: str, name: str | None, member_ids: list[str]) -> bool:
        group = self._repo.get_group(group_id)
        if not group or group["created_by"] != me:
            return False
        members = {self.resolve(m) for m in member_ids or []} | {me}
        old = self._repo.replace_group(group_id, (name or "").strip()[:MAX_NAME] or None, members)
        self._notify_keys(old | members, _system("update_groups"))
        return True

    def group_members(self, me: str, group_id: str) -> list[str] | None:
        if not self._repo.is_member(group_id, me):
            return None
        return [self._remember(k) for k in self._repo.group_members(group_id)]

    def _notify_keys(self, keys, msg: str) -> None:
        for k in keys:
            self.hub.send_to(k, msg)

    # ── Conversations ─────────────────────────────────────────────────
    def messages(self, me: str, target_id: str, before_id: int | None, limit: int,
                 after_id: int | None = None) -> list[dict]:
        """Oldest → newest. ``before_id`` pages back through history; ``after_id``
        returns what arrived since (catching up after a reconnect)."""
        target = self.resolve(target_id)
        if target.startswith(CUSTOM_GROUP_PREFIX) and not self._repo.is_member(target, me):
            return []
        limit = max(1, min(limit, 200))
        if after_id is not None:
            rows = self._repo.list_messages_after(me, target, after_id, limit)
        else:
            rows = list(reversed(self._repo.list_messages(me, target, before_id, limit)))
        reactions = self._repo.reactions_for([r["id"] for r in rows])
        return [self._serialize(r, reactions.get(r["id"])) for r in rows]

    def _can_see(self, me: str, row) -> bool:
        """May ``me`` see this message? (global: everyone; group: members; DM: the two people)"""
        target = row["to_target"]
        if target == GLOBAL_TARGET:
            return True
        if target.startswith(CUSTOM_GROUP_PREFIX):
            return self._repo.is_member(target, me)
        return me in (row["from_mac"], target)

    def sidebar(self, me: str, page: int, limit: int) -> dict:
        self._remember(me)
        users = self._repo.list_users()
        names = {u["mac"]: u["username"] for u in users}
        groups = self._repo.groups_for(me)
        rooms = [GLOBAL_TARGET] + [g["id"] for g in groups]
        heads = self._repo.last_messages(rooms)
        previews: dict[str, dict] = {t: self._preview(heads.get(t), me, names) for t in rooms}
        for row in self._repo.direct_message_heads(me):
            peer = row["from_mac"] if row["to_target"] == me else row["to_target"]
            if peer not in previews:
                previews[peer] = self._preview(row, me, names)
        unread = {**self._repo.unread_in_rooms(me, rooms), **self._repo.unread_direct(me)}
        members = self._repo.group_member_counts([g["id"] for g in groups])

        pinned = self._repo.pinned_for(me)
        active = self.hub.active_keys()
        items: list[dict] = []

        def add(tid: str, title: str, kind: str, created_by: str | None = None, is_active: bool = False,
                avatar: str | None = None):
            items.append({"id": self._remember(tid), "title": title, "type": kind,
                          "time_id": previews.get(tid, {"time_id": -1})["time_id"],
                          "created_by": self._remember(created_by) if created_by else None,
                          "is_active": is_active, "is_pinned": tid in pinned,
                          "unread": unread.get(tid, 0), "avatar": avatar,
                          "members": members.get(tid) if kind == "cgroup" else None})

        add(GLOBAL_TARGET, "Global Chat", "global")
        for g in groups:
            add(g["id"], g["name"], "cgroup", g["created_by"])
        for u in users:
            if u["mac"] != me and u["username"]:
                add(u["mac"], u["username"], "user", is_active=u["mac"] in active, avatar=u["avatar"])

        items.sort(key=lambda x: (x["is_pinned"], x["time_id"], x.get("is_active", False)), reverse=True)
        page, limit = max(0, page), max(1, min(limit, 100))
        start = page * limit
        return {"items": items[start:start + limit],
                "previews": {self._remember(k): v for k, v in previews.items()},
                "has_more": len(items) > start + limit}

    def search(self, me: str, query: str) -> list[dict]:
        """Case-insensitive substring search over de-obfuscated text.
        (Searching the obfuscated column with LIKE could never match reliably,
        because the XOR keystream depends on the character position.)"""
        q = (query or "").strip().lower()
        if not q:
            return []
        group_ids = [g["id"] for g in self._repo.groups_for(me)]
        results = []
        for row in self._repo.searchable_messages(me, group_ids):
            if q in self._cipher.decode(row["content"]).lower():
                d = self._serialize(row)
                d.pop("reactions", None)
                results.append(d)
                if len(results) >= SEARCH_LIMIT:
                    break
        return results

    def react(self, me: str, msg_id: int, emoji: str | None, remove: bool) -> bool:
        """False when the message doesn't exist, is deleted or isn't visible to ``me``."""
        row = self._repo.get_message(msg_id)
        if not row or row["is_deleted"] or not self._can_see(me, row):
            return False
        if remove:
            self._repo.clear_reactions(msg_id, me)
        else:
            emoji = str(emoji or "").strip()[:16]
            if not emoji:
                return False
            self._repo.add_reaction(msg_id, me, emoji)
        self._broadcast_message(msg_id)
        return True

    def set_pinned(self, me: str, target_id: str, pinned: bool) -> None:
        target = self.resolve(target_id)
        (self._repo.pin if pinned else self._repo.unpin)(me, target)

    # ── Real-time events ──────────────────────────────────────────────
    def connect(self, me: str, conn: ChatConnection) -> None:
        # Only a real change of online state is announced, as a tiny presence event —
        # not "everybody refetch everything" (that was N×N requests with N devices).
        if self.hub.add(me, conn):
            self.hub.broadcast(self._presence(me, True), skip=me)

    def disconnect(self, me: str, conn: ChatConnection) -> None:
        conn.close()
        if self.hub.remove(me, conn):
            self.hub.broadcast(self._presence(me, False))

    def _presence(self, key: str, online: bool) -> str:
        return json.dumps({"type": "presence", "user": self._remember(key), "online": online})

    def handle_event(self, me: str, data: dict[str, Any]) -> None:
        etype = data.get("type")
        if etype in ("edit", "delete"):
            self._edit_or_delete(me, data.get("msg_id"), data.get("text", ""), etype == "delete")
        elif etype == "typing":
            self._typing(me, data.get("target"), bool(data.get("is_typing", False)))
        elif etype == "read":
            self._read(me, data.get("to_target"), data.get("last_id"))
        elif etype in MESSAGE_TYPES:
            self._post(me, etype, data)

    def _edit_or_delete(self, me: str, msg_id, text: str, delete: bool) -> None:
        try:
            msg_id = int(msg_id)
        except (TypeError, ValueError):
            return
        own = self._repo.get_own_message(msg_id, me)
        if not own or own["is_deleted"]:
            return
        if delete:
            self._repo.delete_message(msg_id, DELETED_TEXT)
            self._remove_attachment(own["file_url"])
        else:
            text = str(text or "")[:MAX_TEXT]
            if not text.strip():
                return
            self._repo.edit_message(msg_id, self._cipher.encode(text))
        self._broadcast_message(msg_id)

    def _typing(self, me: str, target_id: str | None, is_typing: bool) -> None:
        target = self.resolve(target_id)
        me_pid = self._remember(me)

        def msg(conversation: str) -> str:
            # "target" is the conversation *as the receiver sees it*: in a DM that is
            # the typer, not the receiver (who has no sidebar row for themselves).
            return json.dumps({"type": "typing", "from": me_pid, "target": conversation, "is_typing": is_typing})

        if target == GLOBAL_TARGET:
            self.hub.broadcast(msg(GLOBAL_TARGET), skip=me)
        elif target.startswith(CUSTOM_GROUP_PREFIX):
            members = self._repo.group_members(target)
            if me in members:
                self._notify_keys((k for k in members if k != me), msg(target))
        elif target != me:
            self.hub.send_to(target, msg(me_pid))

    def _read(self, me: str, target_id: str | None, last_id=None) -> None:
        """I've seen ``target`` up to ``last_id`` (default: everything so far)."""
        target = self.resolve(target_id)
        if not target or target == me:
            return
        if target.startswith(CUSTOM_GROUP_PREFIX) and not self._repo.is_member(target, me):
            return
        try:
            last_id = int(last_id)
        except (TypeError, ValueError):
            last_id = self._repo.max_message_id()
        self._repo.set_read_mark(me, target, last_id)
        if not is_group_target(target):
            self._repo.mark_read(target, me)
            self.hub.send_to(target, json.dumps({"type": "read_receipt", "reader": self._remember(me)}))

    def _post(self, me: str, etype: str, data: dict) -> None:
        target = self.resolve(data.get("to_target", GLOBAL_TARGET))
        if target.startswith(CUSTOM_GROUP_PREFIX) and not self._repo.is_member(target, me):
            self.hub.send_to(me, json.dumps({"type": "error", "code": "not_member", "target": target,
                                             "message": "You are no longer a member of this group."}))
            return
        if etype == "text":
            text = str(data.get("text", ""))[:MAX_TEXT]
            if not text.strip():
                return
            msg_id = self._repo.insert_message(me, target, "text", self._cipher.encode(text), None, None, None)
        else:
            url = str(data.get("url") or "")
            if not url.startswith(FILE_URL_PREFIX):  # blocks javascript: / external links
                return
            msg_id = self._repo.insert_message(me, target, "file", "",
                                               str(data.get("filename") or "")[:255] or None, url,
                                               str(data.get("filetype") or "")[:100] or None)
        self._broadcast_message(msg_id, new=True)

    def _remove_attachment(self, url: str | None) -> None:
        """A deleted photo / voice note / file must not stay downloadable."""
        if not url or not self._files or not url.startswith(FILE_URL_PREFIX):
            return
        if self._repo.count_file_refs(url):  # still attached to another message
            return
        name = url[len(FILE_URL_PREFIX):]
        if not name.startswith("avatar_"):
            self._delete_file(name)

    def _delete_file(self, name: str, attempt: int = 0) -> None:
        """Windows refuses to delete a file someone is still downloading: retry later."""
        if not self._files:
            return
        try:
            self._files.delete(name)
        except PermissionError:
            if attempt < 5:
                timer = threading.Timer(30 * (attempt + 1), self._delete_file, (name, attempt + 1))
                timer.daemon = True
                timer.start()
        except (ValueError, OSError):
            pass

    def _broadcast_message(self, msg_id: int, new: bool = False) -> None:
        """``new_msg`` for a freshly posted message, ``update_msg`` for an edit,
        delete or reaction — clients must not count updates as unread messages."""
        row = self._repo.get_message(msg_id)
        if not row:
            return
        reactions = self._repo.reactions_for([msg_id]).get(msg_id)
        payload = json.dumps({"type": "new_msg" if new else "update_msg", "msg": self._serialize(row, reactions)})
        target = row["to_target"]
        if target == GLOBAL_TARGET:
            self.hub.broadcast(payload)
        elif target.startswith(CUSTOM_GROUP_PREFIX):
            self._notify_keys(self._repo.group_members(target), payload)
        else:
            self.hub.send_to(row["from_mac"], payload)
            if target != row["from_mac"]:
                self.hub.send_to(target, payload)
