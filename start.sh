#!/usr/bin/env bash
# PHANTOM DESK launcher for Linux/macOS: ./start.sh [recap|gate|wallet] [--headless]
set -euo pipefail
cd "$(dirname "$0")"
PY=""
for c in python3.12 python3.13 python3.11 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    PY="$c"; break
  fi
done
[ -n "$PY" ] || { echo "Python 3.11+ not found"; exit 1; }
[ -x .venv/bin/python ] || "$PY" -m venv .venv
if ! cmp -s requirements.txt .venv/installed-requirements.txt; then
  .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
  cp requirements.txt .venv/installed-requirements.txt
fi
[ -f .env ] || cp .env.example .env
exec .venv/bin/python run.py "$@"
