"""SQLite persistence for saved clipboards (lives in the same chat.db file).

Text is stored obfuscated like chat messages (see ``cipher.TextObfuscator``). A
short preview is stored separately so the grid page can list hundreds of clips
without loading — or decoding — every full text.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL DEFAULT '',
    preview TEXT NOT NULL DEFAULT '',
    chars INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clips_updated ON clips (updated DESC);
"""


class ClipRepository:
    def __init__(self, db_path: Path, encode: Callable[[str], str], decode: Callable[[str | None], str]):
        self._db_path = str(db_path)
        self._encode, self._decode = encode, decode
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def create(self, clip_id: str, now: float) -> None:
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO clips (id, created, updated) VALUES (?, ?, ?)", (clip_id, now, now))

    def get(self, clip_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT id, content, version, created, updated FROM clips WHERE id=?",
                            (clip_id,)).fetchone()
        if not row:
            return None
        return {"id": row["id"], "text": self._decode(row["content"]), "version": row["version"],
                "created": row["created"], "updated": row["updated"]}

    def save_many(self, clips: list[tuple[str, str, int, float]]) -> None:
        """[(id, text, version, updated)] in one transaction."""
        rows = [(self._encode(text), self._encode(preview(text)), len(text), version, updated, cid)
                for cid, text, version, updated in clips]
        with self._conn() as c:
            c.executemany("UPDATE clips SET content=?, preview=?, chars=?, version=?, updated=? WHERE id=?", rows)

    def list(self, limit: int = 500) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT id, preview, chars, created, updated FROM clips "
                             "ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r["id"], "preview": self._decode(r["preview"]), "chars": r["chars"],
                 "created": r["created"], "updated": r["updated"]} for r in rows]

    def delete(self, clip_id: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM clips WHERE id=?", (clip_id,)).rowcount > 0

    def delete_empty_before(self, cutoff: float, keep: set[str]) -> list[str]:
        """Clips created and never written to (someone pressed "New" and left)."""
        with self._conn() as c:
            rows = c.execute("SELECT id FROM clips WHERE chars=0 AND updated < ?", (cutoff,)).fetchall()
            gone = [r["id"] for r in rows if r["id"] not in keep]
            c.executemany("DELETE FROM clips WHERE id=?", [(i,) for i in gone])
        return gone


def preview(text: str, limit: int = 400) -> str:
    return text[:limit]
