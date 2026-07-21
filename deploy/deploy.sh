#!/usr/bin/env bash
# Deploy scribe-it (API + bot) to the VPS.
#
# Delivery is via rsync, NOT git pull: gitignored secrets/runtime (cookies.txt,
# .env.prod) and untracked helpers never travel through git, so the rsync'd tree
# is the single source of truth for what runs on the box. `git push` below is an
# optional archival step only.
#
# api.py and the thin bot.py are one tree, one unit of deployment. Strict order:
#   rsync code → uv sync → install units + daemon-reload (+enable) →
#   restart scribe-api → bounded health-loop → 200 → restart scribe-bot.
# If the API never goes healthy we abort BEFORE touching the bot, so the bot
# stays on the previous good code (= rollback).
#
# Usage: ./deploy/deploy.sh
set -euo pipefail

HOST="${HOST:-my-hetzner}"
REMOTE_DIR="/opt/scribe-bot"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HEALTH_RETRIES="${HEALTH_RETRIES:-30}"
HEALTH_INTERVAL_SEC="${HEALTH_INTERVAL_SEC:-2}"

# --- Step 1: archival push (NOT the delivery path) ------------------------------
# The tree on the VPS comes from rsync (Step 2), not from git. This push only
# keeps the remote git mirror current. It is optional/archival, so a failure here
# (non-fast-forward, auth) must NOT abort the real delivery under set -e.
echo "→ [1/7] git push origin main (archival, not delivery)"
git push origin main || echo "  (push failed — archival only, continuing to rsync delivery)"

# --- Step 2: rsync code (code + dependency manifest land FIRST) -----------------
# No destructive delete flag: with a plain overlay the real risk is not deletion
# but clobber — local tmp/cache/staging/jobs would overwrite the prod ones (a
# dev/stale status.json the bot is polling). The excludes are exactly what
# prevents that. A delete flag would wipe in-flight prod jobs, so it stays OUT.
#
# .env.prod and cookies.txt are EXCLUDED on purpose (deviation from the plan's
# "sync .env.prod"): the local .env.prod is NOT the prod version — prod has
# OWNER_ID and TELEGRAM_LOCAL that the local file lacks. rsync'ing the local copy
# would clobber the working prod config and drop OWNER_ID (breaking the bot's
# allow-list). All required secrets (ELEVENLABS_API_KEY, OWNER_ID,
# TELEGRAM_BOT_TOKEN) already live on the box; the new SCRIBE_*/BOTAPI_DOWNLOAD_DIR/
# SCRIBE_API_URL settings default in code to the exact prod paths, so nothing new
# needs to be written into .env.prod.
echo "→ [2/7] rsync code to $HOST:$REMOTE_DIR (overlay, no delete flag)"
rsync -av \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='.git' \
  --exclude='tmp' \
  --exclude='cache' \
  --exclude='staging' \
  --exclude='jobs' \
  --exclude='.adw' \
  --exclude='arsen_state' \
  --exclude='*.session' \
  --exclude='.env' \
  --exclude='.env.local' \
  --exclude='.env.prod' \
  --exclude='cookies.txt' \
  "$LOCAL_DIR/" "$HOST:$REMOTE_DIR/"

# --- Step 3: install/update Python deps (fastapi/uvicorn) BEFORE starting api ---
echo "→ [3/7] uv sync on $HOST"
ssh "$HOST" "cd $REMOTE_DIR && /root/.local/bin/uv sync"

# --- Step 4: install/update systemd units, reload, enable api -------------------
# Without daemon-reload the bot's new After=/Wants=scribe-api.service won't take
# effect and (on the first deploy) `systemctl restart scribe-api` would fail under
# set -e because the unit isn't installed yet. `enable` is idempotent.
echo "→ [4/7] install systemd units + daemon-reload + enable scribe-api"
rsync -av \
  "$LOCAL_DIR/deploy/scribe-api.service" \
  "$LOCAL_DIR/deploy/scribe-bot.service" \
  "$HOST:/etc/systemd/system/"
ssh "$HOST" "systemctl daemon-reload && systemctl enable scribe-api.service"

# --- Step 5: restart the API first (bot depends on it) --------------------------
echo "→ [5/7] restart scribe-api"
ssh "$HOST" "systemctl restart scribe-api"

# --- Step 6: bounded health-loop (abort BEFORE the bot on failure) --------------
# connection-refused = uvicorn still coming up → retry within budget. A non-200
# (e.g. key-less API returns 503) makes `curl -f` non-zero → we never reach ok=1
# → abort without restarting the bot, so the bot stays on the old code.
echo "→ [6/7] health-loop (budget: ${HEALTH_RETRIES}×${HEALTH_INTERVAL_SEC}s)"
ok=""
for i in $(seq 1 "$HEALTH_RETRIES"); do
  if ssh "$HOST" 'curl -fsS http://127.0.0.1:8080/health' >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep "$HEALTH_INTERVAL_SEC"
done
if [ "${ok:-}" != 1 ]; then
  echo "✗ API health FAILED after budget; NOT restarting bot (stays on old code = rollback)" >&2
  exit 1
fi
echo "  ✓ API healthy"

# --- Step 7: restart the bot — ONLY after a healthy API -------------------------
echo "→ [7/7] restart scribe-bot"
ssh "$HOST" "systemctl restart scribe-bot"

echo "✓ deploy complete: scribe-api healthy, scribe-bot restarted on $HOST"
