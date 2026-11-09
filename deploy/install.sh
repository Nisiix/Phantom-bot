#!/usr/bin/env bash
# One-shot installer for a fresh Ubuntu/Debian VPS. Run as root:
#   curl -fsSL https://raw.githubusercontent.com/nisiix/phantom-bot/main/deploy/install.sh | bash
set -euo pipefail
REPO="${REPO:-https://github.com/nisiix/phantom-bot.git}"
DIR="${DIR:-/opt/phantom-desk}"
BRANCH="${BRANCH:-main}"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
if [ ! -d "$DIR/.git" ]; then git clone -b "$BRANCH" "$REPO" "$DIR"; else git -C "$DIR" pull --ff-only; fi
cd "$DIR"
mkdir -p data && chown -R 1000:1000 data || true
[ -f .env ] || cp .env.example .env
docker compose up -d --build
echo
echo "Phantom Desk is running in PAPER mode and restarts automatically."
echo "  Live terminal : docker attach phantom-desk   (leave with Ctrl-P Ctrl-Q, NOT Ctrl-C)"
echo "  Recaps        : ls $DIR/data/reports"
echo "  Telegram etc. : nano $DIR/.env  then  docker compose up -d"
