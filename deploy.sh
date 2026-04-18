#!/usr/bin/env bash
# Deploy pi-monitor to a remote Pi (or any Linux host) over SSH.
#
# Usage:
#   ./deploy.sh full --alias raspi4b
#   ./deploy.sh bot --alias raspi4b
#   ./deploy.sh config --alias raspi4b
#   ./deploy.sh full --alias raspi4b --monitoring
#
# Setup (recommended):
#   cp .deploy.local.template .deploy.local
#   # then set SSH_ALIAS in .deploy.local
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCAL_SRC="$SCRIPT_DIR"
MODE="full"
ENABLE_MONITORING_PROFILE="${ENABLE_MONITORING_PROFILE:-0}"

usage() {
  cat <<'EOF'
Usage: ./deploy.sh [full|bot|config] [--alias <ssh_alias>] [--monitoring]

Examples:
  ./deploy.sh full --alias raspi4b
  ./deploy.sh bot --alias raspi4b
  ./deploy.sh config --alias raspi4b
  ./deploy.sh full --alias raspi4b --monitoring

Configuration:
  1) Preferred: copy .deploy.local.template to .deploy.local and set SSH_ALIAS
  2) Alternative: set PI_USER/PI_HOST in .deploy.local or env
EOF
}

# Required: set these via environment or a sourced .deploy.local file.
#   PI_USER=youruser PI_HOST=yourhost ./deploy.sh
# Or create .deploy.local (git-ignored) with export PI_USER=... PI_HOST=...
if [[ -f "$SCRIPT_DIR/.deploy.local" ]]; then
  # shellcheck source=/dev/null
  source "$SCRIPT_DIR/.deploy.local"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    full|bot|config)
      MODE="$1"
      shift
      ;;
    --alias)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --alias"
        usage
        exit 1
      fi
      SSH_ALIAS="$2"
      shift 2
      ;;
    --monitoring)
      ENABLE_MONITORING_PROFILE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

SSH_ALIAS="${SSH_ALIAS:-}"
PI_USER="${PI_USER:-}"
PI_HOST="${PI_HOST:-}"

if [[ -n "$SSH_ALIAS" ]]; then
  if [[ -z "$PI_USER" ]]; then
    PI_USER="$(ssh -G "$SSH_ALIAS" | awk '/^user /{print $2; exit}')"
  fi
  if [[ -z "$PI_HOST" ]]; then
    PI_HOST="$(ssh -G "$SSH_ALIAS" | awk '/^hostname /{print $2; exit}')"
  fi
fi

if [[ -z "$PI_USER" || -z "$PI_HOST" ]]; then
  echo "Missing deploy target."
  echo "Set SSH_ALIAS via --alias/.deploy.local, or set PI_USER and PI_HOST."
  usage
  exit 1
fi

if [[ -z "$SSH_ALIAS" ]]; then
  SSH_ALIAS="$PI_HOST"
fi

PI_DEST="${PI_DEST:-/home/${PI_USER}/pi-monitor}"

echo "==> Deploying pi-monitor ($MODE) to ${PI_USER}@${PI_HOST}:${PI_DEST} ..."

case "$MODE" in
  full)
    # Full sync — never overwrite .env or data directories (root-owned by containers)
    rsync -avz \
      --exclude='.venv/' \
      --exclude='.git/' \
      --exclude='.gitignore' \
      --exclude='.env' \
      --exclude='__pycache__' \
      --exclude='*.pyc' \
      --exclude='bot-data/' \
      --exclude='portainer-data/' \
      --exclude='beszel-data/' \
      --exclude='beszel-agent-data/' \
      --exclude='tests/' \
      --exclude='.claude' \
      --exclude='*.md' \
      --exclude='LICENSE' \
      --exclude='deploy.sh' \
      --exclude='.deploy.local*' \
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

if [[ "$ENABLE_MONITORING_PROFILE" == "1" ]]; then
  echo "==> Starting monitoring profile (Beszel + Portainer)..."
  if ! ssh "$SSH_ALIAS" "cd '${PI_DEST}' && docker compose --profile monitoring up -d"; then
    echo "==> Monitoring startup failed; recreating monitoring containers..."
    ssh "$SSH_ALIAS" "cd '${PI_DEST}' && docker compose --profile monitoring up -d --force-recreate beszel portainer"
  fi
fi

# Optional one-off manual command:
# ssh "$SSH_ALIAS" "cd '${PI_DEST}' && docker compose --profile monitoring up -d"

echo "==> Done."
echo ""
echo "Logs:      ssh ${SSH_ALIAS} 'docker logs -f pi-control-bot'"
echo "Beszel:    http://${PI_HOST}:8090  (if monitoring profile is active)"
echo "Portainer: http://${PI_HOST}:9000  (if monitoring profile is active)"
