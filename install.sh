#!/usr/bin/env bash
# RetCore OnLAN — Linux setup (Ubuntu/Debian). Run once: ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/3] System packages (needs sudo)…"
sudo apt-get update -q
sudo apt-get install -y python3 python3-venv python3-tk libgl1 libglib2.0-0 pulseaudio-utils
echo "      Optional: sudo apt install adb scrcpy uxplay avahi-daemon   (Android / iOS mirroring)"

echo "[2/3] Python virtualenv + dependencies…"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "[3/3] Done. Start with: ./run.sh"
if [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
  echo "NOTE: You are on Wayland. Screen capture needs an X11 session —"
  echo "      log out and pick 'Ubuntu on Xorg' (gear icon on the login screen)."
fi
