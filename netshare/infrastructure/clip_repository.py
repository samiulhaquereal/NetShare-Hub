"""Saved clipboards — the ``clipboards`` table of the app database.

Text is encrypted like chat messages. A short encrypted preview is kept beside
it so the grid page can list hundreds of clipboards without decrypting every
full text. Times are stored as UTC ``YYYY-MM-DD HH:MM:SS`` and handed to the
service as epoch seconds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .database import Database, utc

_EPOCH = "CAST(strftime('%s', {col}) AS REAL)"


class ClipRepository:
    def __init__(self, db: Database | Path, encode: Callable[[str], str], decode: Callable[[str | None], str]):
        self._db = db if isinstance(db, Database) else Database(Path(db))
        self._encode, self._decode = encode, decode

    def _conn(self):
        return self._db.connect()

    def create(self, clip_id: str, now: float) -> None:
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO clipboards (id, created_at, updated_at) VALUES (?, ?, ?)",
                      (clip_id, utc(now), utc(now)))

    def get(self, clip_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(f"SELECT id, body, version, {_EPOCH.format(col='created_at')} AS created, "
                            f"{_EPOCH.format(col='updated_at')} AS updated FROM clipboards WHERE id = ?",
                            (clip_id,)).fetchone()
        if not row:
            return None
        return {"id": row["id"], "text": self._decode(row["body"]), "version": row["version"],
                "created": row["created"], "updated": row["updated"]}

    def save_many(self, clips: list[tuple[str, str, int, float]]) -> None:
        """[(id, text, version, updated epoch)] in one transaction."""
        rows = [(self._encode(text), self._encode(preview(text)), len(text), version, utc(updated), cid)
                for cid, text, version, updated in clips]
        with self._conn() as c:
            c.executemany("UPDATE clipboards SET body = ?, preview = ?, char_count = ?, version = ?, updated_at = ? "
                          "WHERE id = ?", rows)

    def list(self, limit: int = 500) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(f"SELECT id, preview, char_count, {_EPOCH.format(col='created_at')} AS created, "
                             f"{_EPOCH.format(col='updated_at')} AS updated FROM clipboards "
                             f"ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r["id"], "preview": self._decode(r["preview"]), "chars": r["char_count"],
                 "created": r["created"], "updated": r["updated"]} for r in rows]

    def delete(self, clip_id: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM clipboards WHERE id = ?", (clip_id,)).rowcount > 0

    def delete_empty_before(self, cutoff: float, keep: set[str]) -> list[str]:
        """Clipboards created and never written to (someone pressed "New" and left)."""
        with self._conn() as c:
            rows = c.execute("SELECT id FROM clipboards WHERE char_count = 0 AND updated_at < ?", (utc(cutoff),)).fetchall()
            gone = [r["id"] for r in rows if r["id"] not in keep]
            c.executemany("DELETE FROM clipboards WHERE id = ?", [(i,) for i in gone])
        return gone


def preview(text: str, limit: int = 400) -> str:
    return text[:limit]
