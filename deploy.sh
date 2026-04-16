#!/usr/bin/env bash
# Deploy pi-monitor to a remote Pi (or any Linux host) over SSH.
#
# Usage:
#   ./deploy.sh           — full deploy (rsync + docker compose up --build)
#   ./deploy.sh bot       — ship only bot/ (faster iteration on bot code)
#   ./deploy.sh config    — ship only bot/config/ (log rules changes, no rebuild)
#
# Configuration — override any of these via environment variables:
#   PI_USER=pi PI_HOST=mypi.local ./deploy.sh
#
# Or set them persistently in your shell profile / .env.local:
#   export PI_USER=pi
#   export PI_HOST=raspberrypi.local
#
set -euo pipefail

PI_USER="${PI_USER:-pi}"
PI_HOST="${PI_HOST:-raspberrypi.local}"
PI_DEST="${PI_DEST:-/home/${PI_USER}/Desktop/pi-monitor}"
# SSH_ALIAS: use an alias from ~/.ssh/config for key auth / custom port, or
# default to PI_HOST for a direct connection.
SSH_ALIAS="${SSH_ALIAS:-${PI_HOST}}"

LOCAL_SRC="$(cd "$(dirname "$0")" && pwd)"
MODE="${1:-full}"

echo "==> Deploying pi-monitor ($MODE) to ${PI_USER}@${PI_HOST}:${PI_DEST} ..."

case "$MODE" in
  full)
    # Full sync — never overwrite .env or data directories (root-owned by containers)
    rsync -avz \
      --exclude='.env' \
      --exclude='__pycache__' \
      --exclude='*.pyc' \
      --exclude='bot-data/' \
      --exclude='portainer-data/' \
      --exclude='beszel-data/' \
      --exclude='beszel-agent-data/' \
      "$LOCAL_SRC/" "${PI_USER}@${PI_HOST}:${PI_DEST}/"
    echo "==> Starting services..."
    ssh "$SSH_ALIAS" "cd '${PI_DEST}' && docker compose up -d --build"
    ;;
  bot)
    rsync -avz --exclude='__pycache__' --exclude='*.pyc' \
      "$LOCAL_SRC/bot/" "${PI_USER}@${PI_HOST}:${PI_DEST}/bot/"
    echo "==> Rebuilding and restarting pi-control-bot..."
    ssh "$SSH_ALIAS" "cd '${PI_DEST}' && docker compose up -d --build pi-control-bot"
    ;;
  config)
    rsync -avz "$LOCAL_SRC/bot/config/" "${PI_USER}@${PI_HOST}:${PI_DEST}/bot/config/"
    echo "==> Restarting pi-control-bot (config reload)..."
    ssh "$SSH_ALIAS" "docker restart pi-control-bot"
    ;;
  *)
    echo "Unknown mode: $MODE"
    echo "Usage: $0 [full|bot|config]"
    exit 1
    ;;
esac

echo "==> Done."
echo ""
echo "Logs:      ssh ${SSH_ALIAS} 'docker logs -f pi-control-bot'"
echo "Beszel:    http://${PI_HOST}:8090  (if monitoring profile is active)"
echo "Portainer: http://${PI_HOST}:9000  (if monitoring profile is active)"
