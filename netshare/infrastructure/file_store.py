"""Flat on-disk file store with path-traversal protection.

Every externally supplied name goes through :meth:`FileStore.resolve`, which only
accepts a direct child of the store root — ``../chat.db`` and friends are rejected.
"""

from __future__ import annotations

import io
import os
import re
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable

from PIL import Image

_UNSAFE_CHARS = re.compile(r"[^\w\s.\-()\[\]]")

IMAGE_EXT = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"})
VIDEO_EXT = frozenset({".mp4", ".webm", ".mov", ".mkv", ".avi"})
AUDIO_EXT = frozenset({".mp3", ".wav", ".flac", ".ogg", ".m4a"})
THUMBNAIL_EXT = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})

_ICONS = {
    ".pdf": "📄", ".doc": "📝", ".docx": "📝", ".txt": "📃",
    ".xls": "📊", ".xlsx": "📊", ".csv": "📊", ".ppt": "📊", ".pptx": "📊",
    ".zip": "📦", ".rar": "📦", ".7z": "📦", ".tar": "📦", ".gz": "📦",
    ".jpg": "🖼", ".jpeg": "🖼", ".png": "🖼", ".gif": "🖼", ".svg": "🖼", ".webp": "🖼",
    ".mp4": "🎬", ".mov": "🎬", ".avi": "🎬", ".mkv": "🎬", ".webm": "🎬",
    ".mp3": "🎵", ".wav": "🎵", ".flac": "🎵", ".aac": "🎵",
    ".py": "🐍", ".js": "📜", ".html": "🌐", ".css": "🎨",
    ".exe": "⚙", ".msi": "⚙", ".bat": "⚙", ".apk": "📱", ".ipa": "📱",
}


class UnsafePathError(ValueError):
    """Raised when a requested name would escape the store directory."""


def clean_filename(filename: str | None) -> str:
    """Keep spaces, brackets and unicode word chars; strip path parts and shell symbols."""
    name = os.path.basename(filename or "")
    name = _UNSAFE_CHARS.sub("", name).lstrip(".").strip()
    return name or f"upload_{int(time.time())}"


def file_kind(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    return "other"


def file_icon(name: str) -> str:
    return _ICONS.get(os.path.splitext(name)[1].lower(), "📎")


def human_size(size: int) -> str:
    mb = size / (1024 * 1024)
    return f"{mb / 1024:.2f} GB" if mb > 1024 else f"{mb:.2f} MB"


@dataclass(frozen=True)
class FileEntry:
    name: str
    size: int
    mtime: float

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lower()

    @property
    def kind(self) -> str:
        return file_kind(self.name)


class FileStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._name_lock = threading.Lock()

    # ── Path safety ────────────────────────────────────────────────────
    def resolve(self, name: str) -> Path:
        if not name or "\x00" in name:
            raise UnsafePathError(name)
        candidate = (self.root / name).resolve()
        if candidate.parent != self.root or candidate.name in ("", ".", ".."):
            raise UnsafePathError(name)
        return candidate

    def exists(self, name: str) -> bool:
        try:
            return self.resolve(name).is_file()
        except UnsafePathError:
            return False

    # ── Queries ────────────────────────────────────────────────────────
    def list_entries(self) -> list[FileEntry]:
        """Newest first. One scandir pass — no per-file stat() round trips."""
        entries = []
        with os.scandir(self.root) as it:
            for e in it:
                if e.is_file():
                    st = e.stat()
                    entries.append(FileEntry(e.name, st.st_size, st.st_mtime))
        entries.sort(key=lambda f: f.mtime, reverse=True)
        return entries

    def dir_signature(self) -> int:
        """Cheap change detector for the folder (mtime changes on add/remove/rename)."""
        try:
            return os.stat(self.root).st_mtime_ns
        except OSError:
            return 0

    # ── Commands ───────────────────────────────────────────────────────
    def save_upload(self, original_name: str | None, stream: BinaryIO) -> str:
        """Store under a unique name (``name(1).ext`` …) and return it. Safe for
        hundreds of parallel uploads: the name is claimed atomically first."""
        filename = clean_filename(original_name)
        stem, ext = os.path.splitext(filename)
        with self._name_lock:
            counter = 1
            path = self.root / filename
            while path.exists():
                filename = f"{stem}({counter}){ext}"
                path = self.root / filename
                counter += 1
            path.touch(exist_ok=False)  # placeholder claims the name
        try:
            with open(path, "wb") as out:
                while chunk := stream.read(1024 * 1024):
                    out.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)  # never leave a 0-byte placeholder behind
            raise
        return filename

    def delete(self, name: str) -> bool:
        path = self.resolve(name)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def thumbnail(self, name: str, size: tuple[int, int] = (120, 120)) -> tuple[bytes, str] | None:
        path = self.resolve(name)
        ext = path.suffix.lower()
        if ext not in THUMBNAIL_EXT:
            return None
        try:
            with Image.open(path) as img:
                img.thumbnail(size, Image.Resampling.LANCZOS)
                fmt = "PNG" if ext in (".png", ".webp", ".gif") else "JPEG"
                if fmt == "JPEG" and img.mode in ("RGBA", "P", "LA"):
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format=fmt, quality=85)
                return buf.getvalue(), f"image/{fmt.lower()}"
        except (OSError, ValueError) as exc:
            print(f"[Thumb] {name}: {exc}")
            return None

    def zip_to_tempfile(self, names: Iterable[str]) -> BinaryIO:
        """Bundle files into a spooled temp file (RAM for small, disk for large) so
        multi-GB selections don't exhaust memory. ZIP_STORED: media is already
        compressed and this makes zipping I/O-bound instead of CPU-bound."""
        tmp = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
            for name in names:
                try:
                    path = self.resolve(name)
                except UnsafePathError:
                    continue
                if path.is_file():
                    zf.write(path, arcname=path.name)
        tmp.seek(0)
        return tmp
