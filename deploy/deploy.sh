#!/usr/bin/env bash
# Deploy scribe-bot to VPS via git pull + systemd restart.
# Usage: ./deploy/deploy.sh
set -euo pipefail

HOST="${HOST:-my-hetzner}"
REMOTE_DIR="/opt/scribe-bot"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "→ pushing local commits"
git push origin main

echo "→ rsync to $HOST"
rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='tmp' --exclude='.git' "$LOCAL_DIR/" "$HOST:$REMOTE_DIR/"

echo "→ uv sync on $HOST"
ssh "$HOST" "cd $REMOTE_DIR && /root/.local/bin/uv sync"

echo "→ restarting service"
ssh "$HOST" "systemctl restart scribe-bot && systemctl status scribe-bot --no-pager -l | head -15"
