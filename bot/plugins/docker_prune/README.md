# docker_prune plugin

## Purpose
Shows Docker disk usage/reclaimable stats in Telegram, then runs cleanup operations on demand. Automatic cleanup is optional.

## Configuration (`plugins.yml`)
```yaml
enabled:
  docker_prune:
    schedule: "0 3 * * 0"   # optional cron in configured TZ (default UTC); omit for manual-only
    aggressive: false        # show aggressive manual button
```

## Actions and buttons
- Button: `🧹 Prune Docker` → callback `p.docker_prune:report` (report-first screen)
- From report screen:
  - `🧹 Clean standard` → confirm `p.docker_prune:run_confirm`
  - `💣 Clean aggressive` (when `aggressive: true`) → confirm `p.docker_prune:aggressive_confirm`
  - `🔄 Refresh`
  - `◀️ Plugins`

## What it executes
- Normal prune:
  1. `docker image prune -f`
  2. `docker builder prune -f`
  3. `docker volume prune -f`
- Aggressive prune:
  - `docker system prune -a --volumes -f`

## Scheduling
- If `schedule` is set, registers cron job `docker_prune.scheduled`.
- If `schedule` is omitted, plugin runs in manual-only mode.
- Cron expression is interpreted in the timezone set via `TZ` (default UTC).

## Output and failure behavior
- Manual runs start from a usage report screen, are confirmation-gated, then edit the Telegram message with command output (tail-capped).
- Scheduled runs log success/failure.
- Command timeouts are 120 seconds per Docker command.
- Plugin exit buttons return to the Plugins list (`plugins_menu`).

## Safety notes
- Aggressive prune can remove all unused images and volumes; it is intentionally behind a confirmation step.
- This plugin requires Docker CLI access from the bot container (Docker socket mount).
