"""CI: stamp the build number and commit into the app before PyInstaller runs.

    python tools/write_build_info.py <build number> <commit sha>

Creates netshare/_build_info.py (git-ignored). The updater compares build numbers
to decide whether the "latest" release is newer than the running copy.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    build, commit = int(sys.argv[1]), sys.argv[2].strip()
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        raise SystemExit(f"bad commit: {commit!r}")
    version = re.search(r'__version__ = "([^"]+)"', (ROOT / "netshare" / "__init__.py").read_text(encoding="utf-8"))[1]
    (ROOT / "netshare" / "_build_info.py").write_text(
        f'"""Written by CI (tools/write_build_info.py) — do not edit."""\n'
        f"VERSION = {version!r}\nBUILD = {build}\nCOMMIT = {commit!r}\n", encoding="utf-8")
    print(f"build info: v{version} build {build} ({commit[:7]})")


if __name__ == "__main__":
    main()
