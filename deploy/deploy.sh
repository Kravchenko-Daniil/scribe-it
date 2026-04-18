#!/usr/bin/env bash
# Deploy scribe-bot to VPS via git pull + systemd restart.
# Usage: ./deploy/deploy.sh
set -euo pipefail

HOST="${HOST:-vps-aeza}"
REMOTE_DIR="/opt/scribe-bot"

echo "→ pushing local commits"
git push origin main

echo "→ pulling on $HOST"
ssh "$HOST" "cd $REMOTE_DIR && git pull --ff-only && /root/.local/bin/uv sync"

echo "→ restarting service"
ssh "$HOST" "systemctl restart scribe-bot && systemctl status scribe-bot --no-pager -l | head -20"
