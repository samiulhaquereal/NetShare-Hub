"""CI: write version.json beside the release files.

    python tools/write_manifest.py <release dir> <build number> <commit sha>

The app downloads releases/latest/download/version.json, compares ``build`` with
its own, then downloads its platform's file and checks the SHA-256 and size.
"""

import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = {"windows": "RetCore-OnLAN-Windows.exe", "linux": "RetCore-OnLAN-Linux.tar.gz"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    folder, build, commit = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3].strip()
    version = re.search(r'__version__ = "([^"]+)"', (ROOT / "netshare" / "__init__.py").read_text(encoding="utf-8"))[1]
    assets = {}
    for platform, name in FILES.items():
        path = folder / name
        if not path.exists():
            raise SystemExit(f"missing release file: {path}")
        assets[platform] = {"name": name, "sha256": sha256(path), "size": path.stat().st_size}
    manifest = {
        "version": version, "build": build, "commit": commit,
        "built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "assets": assets,
    }
    (folder / "version.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
