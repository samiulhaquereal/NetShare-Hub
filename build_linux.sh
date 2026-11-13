#!/usr/bin/env bash
# Build a single-file Linux executable (same flags as the CI workflow).
set -euo pipefail
cd "$(dirname "$0")"
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python

"$PY" -m pip install -q pyinstaller
if [ ! -x ngrok ]; then
  "$PY" -c "import shutil; from pyngrok import conf, installer; p = conf.get_default().ngrok_path; installer.install_ngrok(p); shutil.copy(p, 'ngrok')"
  chmod +x ngrok
fi

"$PY" -m PyInstaller --noconfirm --onefile --noconsole --collect-submodules Xlib \
  --add-data "templates:templates" --add-data "static:static" --add-binary "ngrok:." \
  --name "NetShare-Hub-Linux" server.py

./dist/NetShare-Hub-Linux --check
echo "Build complete: dist/NetShare-Hub-Linux"
