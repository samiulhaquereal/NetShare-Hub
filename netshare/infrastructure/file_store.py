"""On-disk file store with path-traversal protection.

Every externally supplied name goes through :meth:`FileStore.resolve`, which only
accepts a path *inside* the store root (``Photos/a.jpg`` is fine; ``../chat.db``,
hidden ``.incoming`` and links pointing outside the folder are rejected).

The root folder is what devices upload into and is tracked by a change journal.
Sub-folders are created by the host by hand (e.g. "Movies", "Class notes") and
are listed on demand.

Uploads are written to a hidden ``.part`` file and renamed into place only when
complete, so other devices never see (or download) a half-written file.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator

from PIL import Image

_SAFE_PUNCT = frozenset(" ._-()[]")
CHUNK = 256 * 1024  # per-upload buffer: 1000 parallel uploads = 256 MB, not 1 GB
PART_PREFIX = ".upload-"
FREE_SPACE_RESERVE = 256 * 1024 * 1024  # never fill the disk to the last byte
SNAPSHOT_MAX_STALE = 5.0  # seconds a big listing may lag behind (clients catch up via delta)
SLOW_BUILD = 0.25         # only listings slower than this are reused while stale
RECONCILE_SECONDS = 15.0
MAX_FOLDER_NAME = 100
MAX_FOLDER_DEPTH = 12

IMAGE_EXT = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"})
VIDEO_EXT = frozenset({".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"})
AUDIO_EXT = frozenset({".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus"})
PDF_EXT = frozenset({".pdf"})
TEXT_EXT = frozenset({".txt", ".md", ".csv", ".log", ".json", ".xml", ".yml", ".yaml", ".ini", ".cfg",
                      ".py", ".js", ".ts", ".html", ".css", ".java", ".kt", ".c", ".cpp", ".h", ".cs",
                      ".go", ".rs", ".php", ".rb", ".sh", ".bat", ".ps1", ".sql", ".dart", ".swift"})
DOC_EXT = frozenset({".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".rtf"}) | PDF_EXT | TEXT_EXT
ARCHIVE_EXT = frozenset({".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"})
THUMBNAIL_EXT = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})

_ICONS = {
    ".pdf": "📄", ".doc": "📝", ".docx": "📝", ".txt": "📃", ".md": "📃",
    ".xls": "📊", ".xlsx": "📊", ".csv": "📊", ".ppt": "📊", ".pptx": "📊",
    ".zip": "📦", ".rar": "📦", ".7z": "📦", ".tar": "📦", ".gz": "📦",
    ".jpg": "🖼", ".jpeg": "🖼", ".png": "🖼", ".gif": "🖼", ".svg": "🖼", ".webp": "🖼",
    ".mp4": "🎬", ".mov": "🎬", ".avi": "🎬", ".mkv": "🎬", ".webm": "🎬",
    ".mp3": "🎵", ".wav": "🎵", ".flac": "🎵", ".aac": "🎵",
    ".py": "🐍", ".js": "📜", ".html": "🌐", ".css": "🎨",
    ".exe": "⚙", ".msi": "⚙", ".bat": "⚙", ".apk": "📱", ".ipa": "📱",
}


_WINDOWS = os.name == "nt"
_UPLOAD_ID = re.compile(r"[0-9a-f]{32}")
_HIDDEN_ATTRS = 0x2 | 0x4  # Windows FILE_ATTRIBUTE_HIDDEN | SYSTEM (desktop.ini, Thumbs.db)


def _is_hidden(entry: os.DirEntry, st: os.stat_result | None = None) -> bool:
    if entry.name.startswith("."):
        return True
    attrs = getattr(st or entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attrs & _HIDDEN_ATTRS)


def _is_link(entry: os.DirEntry) -> bool:
    """Symlinks and NTFS junctions may point outside the shared folder."""
    try:
        return entry.is_symlink() or bool(getattr(entry, "is_junction", lambda: False)())
    except OSError:
        return True


class UnsafePathError(ValueError):
    """Raised when a requested name would escape the store directory."""


class InsufficientSpaceError(OSError):
    """The upload would not fit on the disk."""


class InvalidFolderNameError(ValueError):
    """Empty, too long, too deep or reserved folder name."""


class FolderExistsError(ValueError):
    """A file or folder with that name is already there."""


class UploadBusyError(OSError):
    """Another request for the same resumable upload is still running."""


class OffsetMismatchError(OSError):
    """The client's resume offset doesn't match what the server has stored."""

    def __init__(self, have: int):
        super().__init__(f"server has {have} bytes")
        self.have = have


class IncompleteUploadError(OSError):
    """The client sent fewer bytes than it announced (connection dropped)."""


def clean_filename(filename: str | None) -> str:
    """Keep spaces, brackets and unicode word chars; strip path parts and shell symbols."""
    name = os.path.basename((filename or "").replace("\\", "/"))
    # Letters, digits and combining marks of any script (Bengali vowel signs are
    # marks, which a plain \w would strip: "ছবি" must not become "ছব").
    name = "".join(ch for ch in name if ch in _SAFE_PUNCT or unicodedata.category(ch)[0] in "LNM")
    name = name.lstrip(".").strip()
    return name or f"upload_{int(time.time())}"


def file_kind(name: str) -> str:
    """image | video | audio | pdf | text | doc | archive | other — drives preview and filters."""
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in PDF_EXT:
        return "pdf"
    if ext in TEXT_EXT:
        return "text"
    if ext in DOC_EXT:
        return "doc"
    if ext in ARCHIVE_EXT:
        return "archive"
    return "other"


def file_icon(name: str) -> str:
    return _ICONS.get(os.path.splitext(name)[1].lower(), "📎")


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"  # pragma: no cover


@dataclass(frozen=True)
class FolderEntry:
    name: str
    mtime: float

    def to_dict(self) -> dict:
        return {"name": self.name, "mtime": int(self.mtime)}


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

    def to_dict(self) -> dict:
        return {"name": self.name, "size": self.size, "mtime": int(self.mtime), "kind": self.kind,
                "icon": file_icon(self.name), "ext": self.ext.lstrip(".").upper() or "FILE"}


class _Sink(io.RawIOBase):
    """Write-only buffer that a streaming ZipFile writes into; we drain it as we go."""

    def __init__(self):
        self.buf = bytearray()
        self.pos = 0

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        self.buf += b
        self.pos += len(b)
        return len(b)

    def tell(self) -> int:  # zipfile needs offsets, not seeking
        return self.pos

    def drain(self) -> bytes:
        out, self.buf = bytes(self.buf), bytearray()
        return out


class FileStore:
    """Upload folder + an in-memory change journal.

    Every add/delete bumps a version and is journaled, so a client that already
    has version ``v`` gets only what changed (``changes_since``) instead of the
    whole list — with 400 devices and 200k files that is the difference between
    kilobytes and gigabytes per second. Changes made outside the app (files
    copied into the folder by hand) are detected via the folder's mtime and reset
    the journal, which makes clients fall back to one full listing.
    """

    def __init__(self, root: Path, thumb_dir: Path | None = None, journal_size: int = 20000):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # In-progress uploads live in a hidden sub-folder: writing there doesn't touch
        # the upload folder's mtime, so it never looks like an external change.
        self.incoming = self.root / ".incoming"
        self.incoming.mkdir(exist_ok=True)
        self.thumb_dir = Path(thumb_dir) if thumb_dir else None
        self._lock = threading.Lock()          # names, journal, version
        self._build_lock = threading.Lock()    # one listing build at a time (no stampede)
        self._space_lock = threading.Lock()
        self._reserved = 0
        # Version numbers keep growing across restarts, so a stale client never
        # mistakes an old version for a current one.
        self._version = time.time_ns() // 1_000_000
        self._floor = self._version            # deltas need since >= floor
        # Versions in the journal are consecutive, so the entry for version v sits at
        # index v - journal[0].version: a delta request costs O(changes), not O(journal).
        self._journal: list[tuple[int, str, dict | str]] = []
        self._journal_size = journal_size
        self._own_sig = self.dir_signature()
        self._snapshot: tuple[int, bytes, bytes] | None = None
        self._snapshot_at = 0.0
        self._build_cost = 0.0  # small folders are always listed fresh
        self._count: int | None = None  # files in the folder as far as we know
        self._reconciled_at = time.monotonic()
        self._reconcile_lock = threading.Lock()
        self._busy: set[str] = set()            # resumable uploads with a request in flight
        self._finished: dict[str, str] = {}     # upload id → stored name (answers lost responses)
        self._sweep_stale_parts()

    # ── Path safety ────────────────────────────────────────────────────
    @staticmethod
    def _parts(rel: str) -> list[str]:
        parts = [p for p in (rel or "").replace("\\", "/").split("/") if p]
        for p in parts:
            # no "..", no hidden names (.incoming), no drive letters / NTFS streams ("C:", "a:b")
            if p in (".", "..") or p.startswith(".") or "\x00" in p or (_WINDOWS and ":" in p):
                raise UnsafePathError(rel)
        return parts

    def _inside(self, rel: str, parts: list[str]) -> Path:
        candidate = self.root.joinpath(*parts).resolve()
        # resolve() follows links: a junction to C:\\ ends up outside the root here
        if candidate == self.root or not candidate.is_relative_to(self.root):
            raise UnsafePathError(rel)
        return candidate

    def resolve(self, name: str) -> Path:
        """A file (or folder) path relative to the root, e.g. ``Photos/2024/a.jpg``."""
        parts = self._parts(name)
        if not parts:
            raise UnsafePathError(name)
        return self._inside(name, parts)

    def resolve_dir(self, rel: str | None) -> Path:
        """An existing folder inside the root ("" = the root itself)."""
        parts = self._parts(rel or "")
        if not parts:
            return self.root
        path = self._inside(rel, parts)
        if not path.is_dir():
            raise FileNotFoundError(rel)
        return path

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def exists(self, name: str) -> bool:
        try:
            return self.resolve(name).is_file()
        except UnsafePathError:
            return False

    # ── Queries ────────────────────────────────────────────────────────
    def list_entries(self, folder: Path | None = None) -> list[FileEntry]:
        """Files of ``folder`` (default: root), newest first. Hidden files are skipped."""
        return self.scan(folder)[1]

    def scan(self, folder: Path | None = None) -> tuple[list[FolderEntry], list[FileEntry]]:
        """(sub-folders, files) in one scandir pass; hidden entries and links are skipped."""
        folders, files = [], []
        with os.scandir(folder or self.root) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_file():
                        st = e.stat()
                        if not _is_hidden(e, st):
                            files.append(FileEntry(e.name, st.st_size, st.st_mtime))
                    elif e.is_dir() and not _is_link(e):
                        st = e.stat()
                        if not _is_hidden(e, st):
                            folders.append(FolderEntry(e.name, st.st_mtime))
                except OSError:
                    continue  # vanished or unreadable while listing
        files.sort(key=lambda f: f.mtime, reverse=True)
        folders.sort(key=lambda f: f.name.lower())
        return folders, files

    def folder_listing(self, rel: str) -> tuple[bytes, str]:
        """(json, etag) of a sub-folder. Sub-folders are filled by hand on the host, are
        usually small and aren't journaled, so they are listed fresh on request."""
        path = self.resolve_dir(rel)
        folders, files = self.scan(path)
        body = json.dumps({"dir": self.rel(path) if path != self.root else "",
                           "folders": [f.to_dict() for f in folders],
                           "files": [f.to_dict() for f in files]},
                          ensure_ascii=False, separators=(",", ":")).encode()
        return body, hashlib.sha1(body).hexdigest()[:20]

    def dir_signature(self) -> int:
        """Cheap change detector for the folder (mtime changes on add/remove/rename)."""
        try:
            return os.stat(self.root).st_mtime_ns
        except OSError:
            return 0

    @property
    def version(self) -> int:
        with self._lock:
            self._sync_external()
            return self._version

    def _sync_external(self) -> None:
        """Caller holds ``_lock``. Folder changed behind our back → journal is useless."""
        sig = self.dir_signature()
        if sig != self._own_sig:
            self._own_sig = sig
            self._reset_journal()

    def _reset_journal(self) -> None:
        self._version += 1
        self._floor = self._version
        self._journal.clear()
        self._count = None

    def _count_files(self) -> int:
        with os.scandir(self.root) as it:  # no stat(): Windows returns attributes with the listing
            return sum(1 for e in it if not e.name.startswith(".") and e.is_file())

    def reconcile(self, every: float = RECONCILE_SECONDS) -> None:
        """Safety net for the mtime check: Windows file times tick every ~15 ms, so a
        file copied in by hand right next to an upload can leave the folder mtime
        unchanged. A cheap file count every ``every`` seconds catches that."""
        now = time.monotonic()
        if now - self._reconciled_at < every or not self._reconcile_lock.acquire(blocking=False):
            return
        try:
            self._reconciled_at = now
            with self._lock:
                expected, version = self._count, self._version
            if expected is None:
                return
            actual = self._count_files()
            with self._lock:
                if self._version == version and self._count == expected and actual != expected:
                    self._reset_journal()
        finally:
            self._reconcile_lock.release()

    def _record(self, op: str, payload: dict | str) -> None:
        """Caller holds ``_lock`` and just changed the folder."""
        self._version += 1
        self._journal.append((self._version, op, payload))
        self._own_sig = self.dir_signature()
        if self._count is not None:
            self._count += 1 if op == "add" else -1
        if len(self._journal) > 2 * self._journal_size:  # amortised trim
            del self._journal[:self._journal_size]
            self._floor = self._journal[0][0] - 1

    def changes_since(self, since: int) -> tuple[int, list[dict], list[str]] | None:
        """(version, added, removed) since ``since``; None → client needs the full list."""
        self.reconcile()
        with self._lock:
            self._sync_external()
            if since == self._version:
                return self._version, [], []
            if since < self._floor or since > self._version:
                return None
            start = since - self._journal[0][0] + 1 if self._journal else 0
            changes = self._journal[max(0, start):]
            version = self._version
        net: dict[str, dict | None] = {}
        for _ver, op, payload in changes:
            if op == "add":
                net[payload["name"]] = payload
            else:
                net[payload] = None
        added = [e for e in net.values() if e is not None]
        removed = [n for n, e in net.items() if e is None]
        return version, added, removed

    def listing(self, max_stale: float = SNAPSHOT_MAX_STALE) -> tuple[int, bytes, bytes]:
        """(version, json, gzipped json) of the full list, shared by every client.

        A huge folder takes a while to list, so during a mass upload a snapshot up to
        ``max_stale`` seconds old is reused as long as the journal still covers it —
        the client then catches up with a tiny ``changes_since`` delta instead of
        triggering a rebuild per new version."""
        def usable(snap, version):
            return snap and (snap[0] == version or (
                self._build_cost > SLOW_BUILD and snap[0] >= self._floor
                and time.monotonic() - self._snapshot_at < max_stale))

        with self._lock:
            self._sync_external()
            version, snap = self._version, self._snapshot
            if usable(snap, version):
                return snap
        with self._build_lock:
            with self._lock:
                version, snap = self._version, self._snapshot
                if usable(snap, version):
                    return snap
            started = time.monotonic()
            folder_entries, file_entries = self.scan()
            files = [e.to_dict() for e in file_entries]
            body = json.dumps({"version": version, "folders": [f.to_dict() for f in folder_entries],
                               "files": files}, ensure_ascii=False, separators=(",", ":")).encode()
            snap = (version, body, gzip.compress(body, compresslevel=5))
            with self._lock:
                self._build_cost = time.monotonic() - started
                if self._version == version:
                    self._count = len(files)
                if version >= self._floor:
                    self._snapshot, self._snapshot_at = snap, time.monotonic()
            return snap

    # ── Disk space ─────────────────────────────────────────────────────
    def free_space(self) -> int:
        return shutil.disk_usage(self.root).free

    def check_space(self, size: int | None) -> None:
        if size and size + self._reserved + FREE_SPACE_RESERVE > self.free_space():
            raise InsufficientSpaceError(
                f"Not enough free space on the host PC for this file ({human_size(size)}).")

    def _reserve(self, size: int | None) -> int:
        """Count bytes of uploads still in flight, so 1000 parallel uploads can't each
        see the same free space and together overfill the disk."""
        if not size:
            return 0
        with self._space_lock:
            self.check_space(size)
            self._reserved += size
        return size

    def _release(self, size: int) -> None:
        if size:
            with self._space_lock:
                self._reserved -= size

    # ── Commands ───────────────────────────────────────────────────────
    def save_upload(self, original_name: str | None, stream: BinaryIO, expected_size: int | None = None,
                    folder: str = "") -> str:
        """Stream into a hidden ``.part`` file, then rename to a unique final name
        (``name(1).ext`` …) in ``folder`` (an existing sub-folder, "" = root).
        Nothing is visible until the upload is complete. Returns the relative path."""
        target_dir = self.resolve_dir(folder)  # before reading the body: fail fast
        reserved = self._reserve(expected_size)
        part = self.incoming / f"{PART_PREFIX}{uuid.uuid4().hex}.part"
        written = 0
        try:
            with open(part, "wb") as out:
                while chunk := stream.read(CHUNK):
                    out.write(chunk)
                    written += len(chunk)
            if expected_size is not None and written != expected_size:
                raise IncompleteUploadError(f"upload interrupted ({written} of {expected_size} bytes)")
            return self._publish(part, clean_filename(original_name), target_dir)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        finally:
            self._release(reserved)

    # ── Resumable uploads ──────────────────────────────────────────────
    # A phone's Wi-Fi drops, the screen locks, a 2 GB video is at 80 %… The client
    # names each upload with a random id; bytes received so far stay in
    # .incoming/.upload-<id>.part and the client continues from that offset.

    def _resumable_part(self, upload_id: str) -> Path:
        if not _UPLOAD_ID.fullmatch(upload_id or ""):
            raise UnsafePathError(upload_id)
        return self.incoming / f"{PART_PREFIX}{upload_id}.part"

    def upload_status(self, upload_id: str) -> dict:
        """{"offset": bytes already stored} or {"done": True, "filename": …} when an
        earlier attempt finished but its response never reached the client."""
        part = self._resumable_part(upload_id)
        with self._lock:
            done = self._finished.get(upload_id)
        if done:
            return {"done": True, "filename": done}
        try:
            return {"offset": part.stat().st_size}
        except FileNotFoundError:
            return {"offset": 0}

    def save_resumable(self, upload_id: str, original_name: str | None, stream: BinaryIO, offset: int,
                       length: int, total: int, folder: str = "") -> str | None:
        """Append ``length`` bytes at ``offset`` of a ``total``-byte upload. Returns the
        relative path once complete, None if more bytes are expected. On a dropped
        connection the received bytes are kept for the next attempt."""
        part = self._resumable_part(upload_id)
        if offset < 0 or length < 0 or offset + length > total:
            raise ValueError("bad range")
        target_dir = self.resolve_dir(folder)
        with self._lock:
            if upload_id in self._finished:
                return self._finished[upload_id]
            if upload_id in self._busy:  # previous attempt still being torn down
                raise UploadBusyError(upload_id)
            self._busy.add(upload_id)
        try:
            have = part.stat().st_size if part.exists() else 0
            if have != offset:
                raise OffsetMismatchError(have)
            reserved = self._reserve(length)
            try:
                with open(part, "ab") as out:
                    written = 0
                    try:
                        while chunk := stream.read(CHUNK):
                            out.write(chunk)
                            written += len(chunk)
                    except Exception as exc:  # connection dropped: keep what arrived
                        raise IncompleteUploadError(f"upload interrupted at {offset + written} bytes") from exc
                if written != length:
                    raise IncompleteUploadError(f"upload interrupted at {offset + written} bytes")
            finally:
                self._release(reserved)
            if offset + length < total:
                return None
            name = self._publish(part, clean_filename(original_name), target_dir)
            with self._lock:
                self._finished[upload_id] = name
                while len(self._finished) > 20000:
                    self._finished.pop(next(iter(self._finished)))
            return name
        finally:
            with self._lock:
                self._busy.discard(upload_id)

    def _publish(self, part: Path, filename: str, folder: Path | None = None) -> str:
        folder = folder or self.root
        stem, ext = os.path.splitext(filename)
        with self._lock:
            self._sync_external()
            counter = 1
            while (folder / filename).exists():
                filename = f"{stem}({counter}){ext}"
                counter += 1
            target = folder / filename
            os.replace(part, target)
            if folder == self.root:  # only the root is journaled
                st = target.stat()
                self._record("add", FileEntry(filename, st.st_size, st.st_mtime).to_dict())
        return self.rel(target)

    def _sweep_stale_parts(self, max_age: float = 6 * 3600) -> None:
        """Leftovers of uploads cut off by a crash or power loss."""
        now = time.time()
        try:
            for folder in (self.incoming, self.root):  # root: older versions wrote parts there
                for p in folder.glob(f"{PART_PREFIX}*.part"):
                    if now - p.stat().st_mtime > max_age:
                        p.unlink(missing_ok=True)
        except OSError:
            pass

    def create_folder(self, parent: str, name: str) -> str:
        """New empty folder inside ``parent`` ("" = root). Returns its relative path.
        Raises FolderExistsError / InvalidFolderNameError / FileNotFoundError (parent)."""
        clean = "".join(ch for ch in (name or "") if ch in _SAFE_PUNCT or unicodedata.category(ch)[0] in "LNM")
        clean = clean.strip().lstrip(".").rstrip(". ")  # Windows drops trailing dots/spaces
        if not clean or len(clean) > MAX_FOLDER_NAME:
            raise InvalidFolderNameError("Use letters, numbers, spaces and . _ - ( ) [ ]")
        base = self.resolve_dir(parent)
        if len(self.rel(base / clean).split("/")) > MAX_FOLDER_DEPTH:
            raise InvalidFolderNameError(f"Folders can be at most {MAX_FOLDER_DEPTH} levels deep")
        target = base / clean
        with self._lock:
            if target.exists():
                raise FolderExistsError(f'"{clean}" already exists here')
            try:
                target.mkdir()
            except OSError as exc:  # reserved names such as CON, NUL on Windows
                raise InvalidFolderNameError(f'"{clean}" can\'t be used as a folder name') from exc
            if base == self.root:  # a new top-level folder needs one full list refresh
                self._own_sig = self.dir_signature()
                self._reset_journal()
        return self.rel(target)

    def delete(self, name: str) -> bool:
        path = self.resolve(name)
        with self._lock:
            self._sync_external()
            if not path.is_file():
                return False  # folders are never deleted from the web UI
            path.unlink()
            if path.parent == self.root:
                self._record("del", path.name)
        return True

    def delete_many(self, names: Iterable[str]) -> tuple[list[str], list[str]]:
        """(deleted, failed) — unsafe or missing names count as failed."""
        deleted, failed = [], []
        for name in names:
            try:
                (deleted if self.delete(name) else failed).append(name)
            except (UnsafePathError, OSError):
                failed.append(name)
        return deleted, failed

    # ── Thumbnails ─────────────────────────────────────────────────────
    def thumbnail(self, name: str, size: int = 160) -> tuple[bytes, str] | None:
        """Small preview, cached on disk keyed by name + size + mtime (so it's built once)."""
        path = self.resolve(name)
        ext = path.suffix.lower()
        if ext not in THUMBNAIL_EXT or not path.is_file():
            return None
        st = path.stat()
        fmt = "PNG" if ext in (".png", ".webp", ".gif") else "JPEG"
        key = hashlib.sha1(f"{self.rel(path)}|{st.st_size}|{st.st_mtime_ns}|{size}".encode()).hexdigest()
        cached = self.thumb_dir / f"{key}.{fmt.lower()}" if self.thumb_dir else None
        mimetype = f"image/{fmt.lower()}"
        if cached and cached.exists():
            return cached.read_bytes(), mimetype
        try:
            with Image.open(path) as img:
                if fmt == "JPEG":
                    img.draft("RGB", (size * 2, size * 2))  # libjpeg DCT scaling: ~10x faster decode
                img.thumbnail((size, size), Image.Resampling.LANCZOS)
                if fmt == "JPEG" and img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format=fmt, quality=82, optimize=True)
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            print(f"[Thumb] {name}: {exc}")
            return None
        data = buf.getvalue()
        if cached:
            try:
                cached.parent.mkdir(parents=True, exist_ok=True)
                tmp = cached.with_suffix(".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, cached)
            except OSError:
                pass
        return data, mimetype

    # ── ZIP ────────────────────────────────────────────────────────────
    def iter_zip(self, names: Iterable[str]) -> Iterator[bytes]:
        """Stream a ZIP while it is being built: constant memory, first byte immediately.
        ZIP_STORED because media is already compressed (I/O-bound, not CPU-bound)."""
        sink = _Sink()
        seen: set[str] = set()
        with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
            for path, arcname in self._zip_members(names):
                if arcname in seen:
                    continue
                seen.add(arcname)
                try:
                    info = zipfile.ZipInfo.from_file(path, arcname=arcname)
                except OSError:
                    continue
                info.compress_type = zipfile.ZIP_STORED
                with open(path, "rb") as src, zf.open(info, "w", force_zip64=True) as dst:
                    while chunk := src.read(CHUNK):
                        dst.write(chunk)
                        if len(sink.buf) >= CHUNK:
                            yield sink.drain()
        if sink.buf:
            yield sink.drain()  # last entry + central directory

    def _zip_members(self, names: Iterable[str]) -> Iterator[tuple[Path, str]]:
        """(file, name inside the ZIP); a selected folder brings its whole tree along."""
        for name in names:
            try:
                path = self.resolve(name)
            except UnsafePathError:
                continue
            if path.is_file():
                yield path, path.name
            elif path.is_dir():
                yield from self._walk(path, path.name)

    def _walk(self, folder: Path, prefix: str) -> Iterator[tuple[Path, str]]:
        try:
            subfolders, files = self.scan(folder)
        except OSError:
            return
        for f in files:
            yield folder / f.name, f"{prefix}/{f.name}"
        for d in subfolders:
            yield from self._walk(folder / d.name, f"{prefix}/{d.name}")
