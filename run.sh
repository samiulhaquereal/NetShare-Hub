#!/usr/bin/env bash
# RetCore OnLAN — Linux launcher
cd "$(dirname "$0")" || exit 1
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
exec "$PY" server.py "$@"
