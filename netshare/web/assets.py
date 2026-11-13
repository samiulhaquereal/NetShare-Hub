"""Static asset delivery: cache-busting URLs, HTTP caching and gzip.

* ``asset("css/base.css")`` (Jinja global) -> ``/static/css/base.css?v=<content hash>``.
  The hash changes whenever the file changes, so versioned URLs can be cached by
  browsers "forever" (``immutable``) — repeat visits load CSS/JS/fonts with zero
  network requests, yet an update is picked up immediately.
* Unversioned static URLs fall back to ``no-cache`` (revalidate via ETag → 304).
* Text responses (HTML, CSS, JS, JSON, SVG) are gzip-compressed when the client
  accepts it — typically 70–85 % smaller. Static files are compressed once and
  cached in memory. Downloads, previews, media and streams are never touched.
"""

from __future__ import annotations

import gzip
import hashlib
import mimetypes
import threading
from pathlib import Path

from flask import Flask, request

LONG_CACHE = "public, max-age=31536000, immutable"
COMPRESSIBLE = frozenset({
    "text/html", "text/css", "text/plain", "application/javascript", "text/javascript",
    "application/json", "application/manifest+json", "image/svg+xml",
})
MIN_SIZE = 1024
# User files are served verbatim (range requests, exact bytes, no CPU spent on big files).
_SKIP_PREFIXES = ("/download", "/file-preview", "/chat/download", "/events", "/api/adb/stream", "/ws/")


class AssetVersioner:
    def __init__(self, static_dir: Path):
        self._dir = Path(static_dir)
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def version(self, rel_path: str) -> str:
        path = self._dir / rel_path
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return "0"
        with self._lock:
            hit = self._cache.get(rel_path)
            if hit and hit[0] == mtime:
                return hit[1]
        digest = hashlib.sha1(path.read_bytes()).hexdigest()[:10]
        with self._lock:
            self._cache[rel_path] = (mtime, digest)
        return digest

    def url(self, rel_path: str) -> str:
        rel_path = rel_path.lstrip("/")
        return f"/static/{rel_path}?v={self.version(rel_path)}"


class _GzipCache:
    """Compressed bodies of static files, keyed by (path, etag)."""

    def __init__(self, max_entries: int = 256):
        self._data: dict[tuple[str, str], bytes] = {}
        self._max = max_entries
        self._lock = threading.Lock()

    def get_or_make(self, key, raw: bytes) -> bytes:
        with self._lock:
            hit = self._data.get(key)
        if hit is not None:
            return hit
        gz = gzip.compress(raw, compresslevel=6)
        with self._lock:
            if len(self._data) >= self._max:
                self._data.clear()
            self._data[key] = gz
        return gz


def init_assets(app: Flask, static_dir: Path) -> AssetVersioner:
    # On Windows the first mimetypes lookup reads the registry (~200 ms) while every
    # parallel static request waits — do it now instead of on the first visitor.
    mimetypes.init()
    versioner = AssetVersioner(static_dir)
    gz_cache = _GzipCache()
    app.jinja_env.globals["asset"] = versioner.url

    @app.after_request
    def _cache_headers(resp):
        path = request.path
        if path.startswith("/static/") and resp.status_code in (200, 304):
            if request.args.get("v") or path.startswith("/static/fonts/"):
                resp.headers["Cache-Control"] = LONG_CACHE
            else:
                resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.after_request
    def _gzip(resp):
        if (resp.status_code != 200
                or resp.mimetype not in COMPRESSIBLE
                or "gzip" not in request.headers.get("Accept-Encoding", "").lower()
                or "Content-Encoding" in resp.headers
                or request.path.startswith(_SKIP_PREFIXES)
                or resp.headers.get("Content-Disposition", "").startswith("attachment")):
            return resp
        if resp.is_streamed and not resp.direct_passthrough:
            return resp  # generator responses (SSE, MJPEG) — leave alone
        resp.direct_passthrough = False  # send_file() → materialise the (small, text) body
        raw = resp.get_data()
        if len(raw) < MIN_SIZE:
            return resp
        etag = resp.headers.get("ETag")
        if request.path.startswith("/static/") and etag:
            body = gz_cache.get_or_make((request.path, etag), raw)
        else:
            body = gzip.compress(raw, compresslevel=6)
        resp.set_data(body)
        resp.headers["Content-Encoding"] = "gzip"
        resp.vary.add("Accept-Encoding")
        if etag:
            # Same resource, different encoding → weak validator (still matches for 304s).
            tag, weak = resp.get_etag()
            if tag:
                resp.set_etag(tag, weak=True)
        return resp

    return versioner

