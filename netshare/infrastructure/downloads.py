"""Pinned, hash-verified downloads of the helper tools we ship next to the app.

Every package is identified by an exact URL *and* its SHA-256, so what runs on
the user's machine is byte-for-byte the build we tested — not "whatever the
package manager serves today".
"""

from __future__ import annotations

import hashlib
import os
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ProgressFn = Callable[[int, int], None]  # (bytes_done, bytes_total)


@dataclass(frozen=True)
class Package:
    name: str
    version: str
    url: str
    sha256: str
    size: int = 0  # informational; used for progress when the server sends no length


class DownloadError(RuntimeError):
    pass


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """extractall that refuses absolute paths and ``..`` (zip-slip)."""
    root = dest.resolve()
    for info in zf.infolist():
        target = (dest / info.filename).resolve()
        if target != root and root not in target.parents:
            raise DownloadError(f"unsafe path in archive: {info.filename}")
    zf.extractall(dest)


def install_zip(pkg: Package, dest: Path, progress: ProgressFn | None = None,
                timeout: float = 60) -> Path:
    """Download ``pkg``, verify its SHA-256 and unpack it into ``dest``.

    Unpacking happens in a sibling temp dir that is renamed into place only after
    everything succeeded, so an interrupted install never leaves a half-extracted
    tool behind. Returns ``dest``.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.parent / f".{dest.name}.zip.part"
    staging = dest.parent / f".{dest.name}.staging"
    shutil.rmtree(staging, ignore_errors=True)
    digest = hashlib.sha256()
    try:
        req = urllib.request.Request(pkg.url, headers={"User-Agent": "RetCore-OnLAN"})
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or pkg.size or 0)
            done = 0
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if digest.hexdigest() != pkg.sha256.lower():
            raise DownloadError(f"{pkg.name} {pkg.version}: checksum mismatch — download rejected")
        with zipfile.ZipFile(part) as zf:
            _safe_extract(zf, staging)
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(staging, dest)
        return dest
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError(f"{pkg.name} download failed: {exc}") from exc
    finally:
        part.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
