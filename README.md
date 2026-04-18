# pi-monitor

A Telegram bot for monitoring and managing Docker containers on a Raspberry Pi (or any Linux host). Get real-time alerts, control your containers, and check host health — all from Telegram, with no port-forwarding required.

> **Quick start:** 3 steps — clone, configure, run. No database, no web server, no cloud account beyond a Telegram bot token.

> [!WARNING]
> **This repo was fully built using Claude (Anthropic AI).** Every line of code, every config, every README — vibecoded from scratch. No human wrote the implementation. Use at your own risk, audit before running on production hardware.

---

## Features

### Container Management
- **Full control** — start, stop, restart, and rebuild any container or compose project from Telegram
- **Family view** — containers are grouped by their Docker Compose project, showing running/total counts at a glance
- **Ghost containers** — families remain visible after `compose down`; the bot remembers what was there
- **Rebuild flow** — streams the build log, then tails the container output so you can see if it started correctly
- **Confirmation steps** — destructive actions (rebuild, stop all) require confirmation before executing

### Alerts
- **Crash detection** — fires when a container exits with a non-zero code, with last 15 log lines and dependency health
- **Restart loop detection** — alerts when a container restarts more than 3 times in 2 minutes
- **Health check failures** — alerts on Docker `unhealthy` health status events
- **Host resource alerts** — CPU load, RAM, swap, disk, and temperature thresholds (all configurable)
- **Log-loop detection** — smart fingerprinting catches repeated error patterns in container logs before they flood your disk
- **Flood guard** — if a container emits >10,000 log lines/second, alerts and pauses monitoring for that container
- **Alert cooldown** — same alert won't fire again within the configured cooldown window

### Inline Keyboard UI
- Navigate the full container tree from a single `/start` message
- Per-container detail view with quick-action buttons: logs, restart, rebuild, stop, start
- Jump directly to the last-alerted container from anywhere
- Silence a specific log-loop signature without restarting the bot
- Error-only log filter for quick diagnosis

### Plugin System
- **docker_prune** — weekly Docker image/builder/volume prune, plus a manual button; replaces the old `docker-prune` sidecar container
- **midnight_restarter** — restart whitelisted containers on a daily schedule (replaces ad-hoc restarter sidecars)
- **host_controls** — reboot, shutdown, restart bot, drop caches — all with two-step confirmation
- **apt_maintenance** — instant action menu with separate update and cleanup flows, plus docker-sensitive confirm before upgrade
- Opt-in per host via `bot/config/plugins.yml` — no Telegram toggle yet (planned)

### Optional Monitoring Stack
- **Beszel** — lightweight system and Docker monitoring dashboard (`:8090`)
- **Portainer** — Docker web UI (`:9000`)
- Enabled via a single Docker Compose profile flag — zero extra config required

---

## Quick Start

### Prerequisites
- Docker + Docker Compose on your Pi (or any Linux host)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram user ID (message [@userinfobot](https://t.me/userinfobot) to find it)

### 1. Clone and configure

```bash
git clone https://github.com/yourusername/pi-monitor.git
cd pi-monitor
cp .env.example .env
```

Edit `.env` — three values are required to get started:

```bash
BOT_TOKEN=your_bot_token_here
ALLOWED_USER_IDS=123456789
DESKTOP_PATH=/home/pi/Desktop
```

Set `DESKTOP_PATH` to your actual Desktop path (used when the bot runs `docker compose` commands):

```bash
# Raspberry Pi (default user)
DESKTOP_PATH=/home/pi/Desktop

# Mac mini / custom user
DESKTOP_PATH=/home/yourname/Desktop
```

Optionally set `HOST_LABEL` to a short name for this machine. It will prefix all alerts so you can tell which host sent them when running the same bot on multiple machines:

```bash
HOST_LABEL=raspik4b
```

### 2. Start the bot

```bash
docker compose up -d
```

### 3. Open Telegram

Send `/start` to your bot. You should see a menu with all your running containers.

**Test alerts are working:**
```
/testalert crash
/testalert host
/testalert logloop
```

---

## Optional: Full Monitoring Stack

Add Beszel (system monitoring) and Portainer (Docker web UI):

```bash
docker compose --profile monitoring up -d
```

Then open:
- **Beszel**: `http://<your-pi>:8090` — first login creates an admin account. Go to *Systems → Add system* to connect the Beszel agent. Copy the public key into `BESZEL_KEY` in your `.env`, then run `docker compose --profile monitoring up -d` again.
- **Portainer**: `http://<your-pi>:9000` — first login creates an admin account.

---

## Configuration

### Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | ✅ | — | Telegram bot token from @BotFather |
| `ALLOWED_USER_IDS` | ✅ | — | Comma-separated Telegram user IDs |
| `DESKTOP_PATH` | ✅ | — | Absolute host path to your Desktop directory |
| `HOST_LABEL` | | hostname | Short label prefixed on all alerts (e.g. `raspik4b`) |
| `PLUGINS_YML_PATH` | | `/app/config/plugins.yml` | Path to plugin config inside the container |
| `REGISTRY_PATH` | | `/data/registry.json` | Where the bot persists the container registry |
| `TELEGRAM_API_BASE_URL` | | Telegram cloud | Override to use a local Bot API server |
| `DISK_THRESHOLD_PCT` | | `90` | Disk usage % that triggers an alert |
| `RAM_THRESHOLD_PCT` | | `90` | RAM usage % that triggers an alert |
| `SWAP_THRESHOLD_PCT` | | `80` | Swap usage % that triggers an alert |
| `CPU_LOAD_THRESHOLD` | | `3.0` | 1-min load average per core that triggers an alert |
| `TEMP_THRESHOLD_C` | | `75` | CPU/SoC temperature (°C) that triggers an alert |
| `ALERT_COOLDOWN_MINUTES` | | `10` | Minimum gap between repeated alerts for the same issue |
| `TZ` | | `UTC` | IANA timezone for all display timestamps and cron/daily schedules |
| `BESZEL_KEY` | | — | Beszel agent public key (monitoring profile only) |

### Plugins

Plugins are enabled in `bot/config/plugins.yml` (copy from `bot/config/plugins.yml.example`):

```yaml
enabled:
  docker_prune:
    schedule: "0 3 * * 0"   # optional cron (configured TZ, default UTC); omit for manual-only
    aggressive: false
  apt_maintenance:
    max_listed_updates: 20
  midnight_restarter:
    containers: [stremio-server]
    time: "04:00"            # optional HH:MM (configured TZ, default UTC); omit for manual-only
  host_controls: {}
```

For auto-capable plugins, omitting `schedule` / `time` / `interval_seconds` disables automatic execution and keeps the plugin manual-only in Telegram.

After editing, restart the bot to apply: `docker compose up -d --build pi-control-bot`

By default, `plugins.yml` is loaded from `/app/config/plugins.yml` inside the image, so rebuilding `pi-control-bot` is required after edits. If you prefer restart-only config updates, set `PLUGINS_YML_PATH=/data/plugins.yml` and keep that file in the `bot-data` volume.

Enabled plugins appear under the **🧩 Plugins** button in `/start`.

> **Dynamic plugin toggle via Telegram is planned for a future release.**

**Upgrading from a previous version:** The old `docker-prune` sidecar container has been replaced by the `docker_prune` plugin. After upgrading, remove it if still present:

```bash
docker rm -f docker-prune
```

Then enable the plugin in `plugins.yml`.

### Log Rules

Customize log-loop detection per container in `bot/config/log_rules.yml`. Each container can override:
- Which log patterns count as interesting (`interesting` regex list)
- Which patterns to silently ignore (`ignore` regex list)
- How many matching lines in a window trigger an alert (`threshold`)
- The size of the sliding window (`window_seconds`)
- How long to wait before re-alerting on the same pattern (`cooldown_minutes`)

Containers not listed inherit from `defaults`. Example:

```yaml
containers:
  my-webserver:
    ignore:
      - "GET /"
      - "favicon.ico"

  my-quiet-worker:
    threshold: 5
    window_seconds: 30
```

Changes to `log_rules.yml` take effect on the next container restart or when the bot is restarted.

---

## Bot Commands

| Command | Description |
|---|---|
| `/start` or `/menu` | Open the main container menu |
| `/status` | Host resource overview (CPU, RAM, disk, temp) |
| `/testalert [crash\|host\|logloop]` | Test the alert system |
| `/help` | List all commands |

### Inline Actions

From the container detail view:
- **Logs** — tail 30, 100, or 200 lines; or errors-only filter
- **Restart** — quick restart via Docker SDK
- **Rebuild** — `compose down` → build → `compose up` with live build log streaming
- **Stop / Start** — compose-level stop (removes container) and start
- **Forget** — remove a ghost container from the persistent registry

---

## Architecture

```
Telegram ←────────────────────────────── Bot (python-telegram-bot)
                                              │
                            ┌─────────────────┼─────────────────────┐
                            │                 │                     │
                     Docker Socket     Docker Socket          Docker Socket
                            │                 │                     │
                    DockerEventsMonitor  HostWatchdog         LogLoopManager
                    (crash / restart /   (CPU / RAM /         (per-container
                     health alerts)       disk / temp)         log streaming
                                                               + fingerprint
                                                               alerting)
                            └─────────────────┴─────────────────────┘
                                              │
                                       Notifier queue
                                       (dedup + cooldown)
                                              │
                                         Telegram alert
```

All alert sources push into a single async notifier queue. One consumer sends to all allowed users, deduplicating within the cooldown window.

---

## Remote Deploy (Optional)

If you develop on a separate machine and deploy to your Pi over SSH, use the included `deploy.sh`:

```bash
# One-time setup
cp .deploy.local.template .deploy.local
# Edit .deploy.local and set SSH_ALIAS=raspi4b

# Full deploy (rsync + rebuild)
./deploy.sh full --alias raspi4b

# Bot code only (faster iteration)
./deploy.sh bot --alias raspi4b

# Config files only (log rules, no rebuild)
./deploy.sh config --alias raspi4b

# Optional: also start monitoring profile (Beszel + Portainer)
./deploy.sh full --alias raspi4b --monitoring
```

`deploy.sh` supports alias-first resolution and auto-derives host/user from your SSH config.

---

## Troubleshooting

**Bot doesn't respond**
- Check `docker logs pi-control-bot` for errors
- Verify `BOT_TOKEN` is set and valid in `.env`
- Make sure your Telegram user ID is in `ALLOWED_USER_IDS`

**"Permission denied" on Docker socket**
- The bot container needs access to `/var/run/docker.sock`. On some systems you may need to add the user to the `docker` group or adjust socket permissions.

**Rebuild fails: "docker compose not found"**
- The Dockerfile installs Docker CLI + Compose plugin. If the build fails at that step, check your internet connection and Docker Hub access from the Pi.

**No temperature reading in `/status`**
- Temperature reading tries three methods: `psutil`, `/sys/class/thermal/thermal_zone0/temp`, and `vcgencmd`. If none work on your hardware, the field is simply omitted from the status display.

**Want to use a local Telegram Bot API server?**
- Set `TELEGRAM_API_BASE_URL=http://your-bot-api-host:8081/bot` in `.env`
- Add the local Bot API server's Docker network to `docker-compose.yml` as an external network and attach `pi-control-bot` to it (see the comment at the bottom of `docker-compose.yml`)

---

## License

[MIT](LICENSE)
