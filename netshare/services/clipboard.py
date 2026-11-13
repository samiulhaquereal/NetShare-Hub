"""Clipboards synced live between every connected device and saved in the DB.

There is one shared clipboard (``DEFAULT_ID``) plus any number created from the
clipboard grid page. Devices editing one hold a WebSocket (``/ws/clipboard``): an edit is
one message in, one message out to everybody else — no poll, no extra fetch.
A single connection delivers messages in order, so an older edit can never
overwrite a newer one. Each peer has its own sender thread that only ever keeps
the *latest* text, so a slow phone can't hold up anybody else.
"""

from __future__ import annotations

import atexit
import json
import re
import secrets
import threading
import time
from typing import Callable

from .events import ChangeNotifier
from .stream_hub import StreamClient, StreamHub

TOPIC = "clipboard"
MAX_CLIPBOARD_BYTES = 1_000_000  # ~1 MB of text: plenty for links, notes, code


HISTORY_CHARS = 4_000_000  # recent versions kept per clipboard for merging concurrent edits
FLUSH_SECONDS = 1.0        # typing is saved to the DB at most this often
EMPTY_TTL = 10 * 60        # a clipboard created but never typed into disappears after this
MAX_CACHED = 200           # idle clipboards kept in memory (others reload from the DB)
_BOARD_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")


class ClipboardTooLargeError(ValueError):
    pass


def _splice(a: str, b: str) -> tuple[int, int, str]:
    """The single edit turning ``a`` into ``b``: replace a[p:e] with ``ins``."""
    n = min(len(a), len(b))
    p = 0
    while p < n and a[p] == b[p]:
        p += 1
    s = 0
    while s < n - p and a[len(a) - 1 - s] == b[len(b) - 1 - s]:
        s += 1
    return p, len(a) - s, b[p:len(b) - s]


def merge3(base: str, mine: str, theirs: str) -> str | None:
    """Combine two edits of ``base`` made at the same time. Works when they touch
    different parts of the text (the usual case: two people typing in different
    places); None when they overlap. Mirrors ``merge3`` in clipboard.js."""
    if mine == base or mine == theirs:
        return theirs
    if theirs == base:
        return mine
    mp, me, mins = _splice(base, mine)
    tp, te, _ = _splice(base, theirs)
    if me <= tp:  # my edit lies before theirs: positions up to tp are unchanged in theirs
        return theirs[:mp] + mins + theirs[me:]
    if mp >= te:  # after theirs: shift by how much their edit changed the length
        d = len(theirs) - len(base)
        return theirs[:mp + d] + mins + theirs[me + d:]
    return None


class ClipboardPeer:
    """One live clipboard connection. ``send`` is the socket's (blocking) send."""

    def __init__(self, send: Callable[[str], None]):
        self._send = send
        self._cond = threading.Condition()
        self._text: str | None = None     # latest text update (older ones are dropped)
        self._replies: list[str] = []     # acks / errors / pongs, in order
        self.closed = False

    def push_text(self, message: str) -> None:
        with self._cond:
            self._text = message
            self._cond.notify()

    def reply(self, message: str) -> None:
        with self._cond:
            self._replies.append(message)
            self._cond.notify()

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._cond.notify()

    def run_sender(self) -> None:
        while True:
            with self._cond:
                while not self.closed and self._text is None and not self._replies:
                    self._cond.wait()
                if self.closed:
                    return
                batch = self._replies
                self._replies = []
                if self._text is not None:
                    batch.append(self._text)
                    self._text = None
            try:
                for msg in batch:
                    self._send(msg)
            except Exception:
                self.close()
                return


class Board:
    """One clipboard: its text, recent versions (for merging) and live devices."""

    def __init__(self, board_id: str, text: str = "", version: int = 0,
                 created: float | None = None, updated: float | None = None):
        self.id = board_id
        self.text = text
        self.version = version
        self.created = created or time.time()
        self.updated = updated or self.created
        self.history: dict[int, str] = {version: text}
        self.history_chars = len(text)
        self.peers: set[ClipboardPeer] = set()
        self.dirty = False

    def remember(self, version: int, text: str) -> None:
        self.history[version] = text
        self.history_chars += len(text)
        while self.history_chars > HISTORY_CHARS and len(self.history) > 1:
            old = next(iter(self.history))
            self.history_chars -= len(self.history.pop(old))


class ClipboardService:
    """All clipboards. ``DEFAULT_ID`` is the shared one used by the HTTP API and the
    screen viewers; the clipboard page can create as many more as people like.

    Boards are loaded from the repository on first use and written back by a
    background flusher (at most every ``FLUSH_SECONDS``), so typing never waits
    on the disk. Without a repository everything stays in memory (tests)."""

    DEFAULT_ID = "shared"

    def __init__(self, hub: StreamHub, notifier: ChangeNotifier, repo=None):
        self._hub = hub
        self._notifier = notifier
        self._repo = repo
        self._lock = threading.RLock()
        self._boards: dict[str, Board] = {}
        self._flusher: threading.Thread | None = None
        self._wake = threading.Event()
        self._cleaned_at = 0.0
        if repo:
            atexit.register(self._flush_at_exit)

    # ── Boards ─────────────────────────────────────────────────────────
    def board(self, board_id: str | None = None) -> Board | None:
        board_id = board_id or self.DEFAULT_ID
        with self._lock:
            b = self._boards.get(board_id)
            if b:
                return b
            row = self._repo.get(board_id) if self._repo and _BOARD_ID.fullmatch(board_id) else None
            if row:
                b = Board(row["id"], row["text"], row["version"], row["created"], row["updated"])
            elif board_id == self.DEFAULT_ID:
                # The shared clipboard always exists for screen viewers and the API; after
                # being deleted it starts again empty and is saved (and listed) once used.
                b = Board(board_id)
            else:
                return None
            self._boards[board_id] = b
            return b

    def create(self) -> str:
        board_id = secrets.token_urlsafe(9)
        now = time.time()
        with self._lock:
            if self._repo:
                self._repo.create(board_id, now)
            self._boards[board_id] = Board(board_id, created=now)
        self._notifier.bump(TOPIC)
        return board_id

    def delete(self, board_id: str) -> bool:
        with self._lock:
            b = self._boards.pop(board_id, None)
            found = self._repo.delete(board_id) if self._repo else b is not None
            peers = list(b.peers) if b else []
        for peer in peers:  # open editors leave the deleted clipboard
            peer.reply('{"type":"deleted"}')
        if found or b:
            self._notifier.bump(TOPIC)
        return bool(found or b)

    def list(self) -> list[dict]:
        """Newest first, for the grid page. Read-only: boards being edited right now are
        taken from memory, so many grid pages refreshing never cause DB writes."""
        if self._repo:
            now = time.monotonic()
            if now - self._cleaned_at > 60:  # "New" pressed and abandoned: tidy up now and then
                self._cleaned_at = now
                with self._lock:
                    keep = {i for i, b in self._boards.items() if b.peers or b.dirty or b.text} | {self.DEFAULT_ID}
                    for gone in self._repo.delete_empty_before(time.time() - EMPTY_TTL, keep=keep):
                        self._boards.pop(gone, None)
            items = self._repo.list()
        else:
            items = []
        with self._lock:
            by_id = {it["id"]: it for it in items}
            for b in self._boards.values():
                it = by_id.get(b.id)
                if it is None:
                    if self._repo:
                        continue  # deleted from the DB meanwhile
                    it = by_id[b.id] = {"id": b.id, "created": b.created}
                    items.append(it)
                if b.dirty or not self._repo:
                    it.update(preview=b.text[:400], chars=len(b.text), updated=b.updated)
                it["live"] = len(b.peers)
            for it in items:
                it.setdefault("live", 0)
                it["shared"] = it["id"] == self.DEFAULT_ID
        items.sort(key=lambda x: x["updated"], reverse=True)
        return items

    # ── Default board (HTTP API, screen viewers, older callers) ────────
    @property
    def text(self) -> str:
        return self.board().text

    @property
    def version(self) -> int:
        return self.board().version

    def snapshot(self, board_id: str | None = None) -> tuple[str, int]:
        b = self.board(board_id)
        if b is None:
            raise KeyError(board_id)
        with self._lock:
            return b.text, b.version

    def update(self, text: str, origin: StreamClient | ClipboardPeer | None = None) -> int:
        """Set the shared text; returns the new version (unchanged text keeps it)."""
        return self.edit(text, origin)[0]

    # ── Editing ────────────────────────────────────────────────────────
    def edit(self, text: str, origin: StreamClient | ClipboardPeer | None = None,
             base: int | None = None, board_id: str | None = None) -> tuple[int, str]:
        """Apply a device's text. With ``base`` (the version the device was looking at)
        an edit that raced with someone else's is merged into the current text
        instead of replacing it. Returns (version, resulting text)."""
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        self._check_size(text)
        b = self.board(board_id)
        if b is None:
            raise KeyError(board_id)
        with self._lock:
            if base is not None and base != b.version and base in b.history:
                merged = merge3(b.history[base], text, b.text)
                if merged is not None:
                    self._check_size(merged)
                    text = merged
            if text == b.text:
                return b.version, text
            b.text = text
            b.version += 1
            b.updated = time.time()
            b.dirty = True
            version = b.version
            b.remember(version, text)
            peers = [p for p in b.peers if p is not origin]
        msg = self.message(text, version)
        for peer in peers:
            peer.push_text(msg)
        if b.id == self.DEFAULT_ID:
            self._hub.publish_clipboard(text, exclude=origin if isinstance(origin, StreamClient) else None)
        self._notifier.bump(TOPIC)
        self._schedule_flush()
        return version, text

    @staticmethod
    def _check_size(text: str) -> None:
        if len(text) > MAX_CLIPBOARD_BYTES or len(text.encode("utf-8", "surrogatepass")) > MAX_CLIPBOARD_BYTES:
            raise ClipboardTooLargeError(f"Clipboard text is limited to {MAX_CLIPBOARD_BYTES // 1_000_000} MB")

    @staticmethod
    def message(text: str, version: int) -> str:
        return json.dumps({"type": "text", "text": text, "version": version}, ensure_ascii=False)

    # ── Persistence ────────────────────────────────────────────────────
    def _schedule_flush(self) -> None:
        if not self._repo:
            return
        with self._lock:
            if self._flusher is None:
                self._flusher = threading.Thread(target=self._flush_loop, daemon=True, name="clipboard-flush")
                self._flusher.start()
        self._wake.set()

    def _flush_loop(self) -> None:
        while True:
            self._wake.wait()
            time.sleep(FLUSH_SECONDS)  # gather a second of typing into one write
            self._wake.clear()
            try:
                self.flush()
            except Exception as exc:  # disk full, locked DB…: keep the edits, retry later
                print(f"[Clipboard] save failed: {exc}")
                self._wake.set()

    def _flush_at_exit(self) -> None:
        try:
            self.flush()
        except Exception:  # DB gone (temp dir removed) — nothing more we can do
            pass

    def flush(self) -> None:
        """Write unsaved boards now (also runs at exit)."""
        if not self._repo:
            return
        with self._lock:
            dirty = [b for b in self._boards.values() if b.dirty]
            batch = [(b.id, b.text, b.version, b.updated) for b in dirty]
            for b in dirty:
                b.dirty = False
        if not batch:
            return
        try:
            shared = next((b for b in dirty if b.id == self.DEFAULT_ID), None)
            if shared:  # (re)created on first use, e.g. after being deleted
                self._repo.create(shared.id, shared.created)
            self._repo.save_many(batch)
        except Exception:
            with self._lock:
                for b in dirty:
                    b.dirty = True
            raise
        with self._lock:  # forget idle boards; they are reloaded from the DB on demand
            for b in dirty:
                if not b.peers and not b.dirty and b.id != self.DEFAULT_ID and len(self._boards) > MAX_CACHED:
                    self._boards.pop(b.id, None)

    # ── Live peers ─────────────────────────────────────────────────────
    def add_peer(self, peer: ClipboardPeer, board_id: str | None = None) -> bool:
        """Register and queue the current text as the first message (False: no such board)."""
        b = self.board(board_id)
        if b is None:
            return False
        with self._lock:
            b.peers.add(peer)
            text, version = b.text, b.version
        peer.push_text(json.dumps({"type": "state", "text": text, "version": version}, ensure_ascii=False))
        self._notifier.bump(TOPIC)  # grid shows who's live
        return True

    def remove_peer(self, peer: ClipboardPeer, board_id: str | None = None) -> None:
        with self._lock:
            b = self._boards.get(board_id or self.DEFAULT_ID)
            if b:
                b.peers.discard(peer)
        peer.close()
        self._notifier.bump(TOPIC)

    @property
    def peer_count(self) -> int:
        with self._lock:
            return sum(len(b.peers) for b in self._boards.values())
