# stremio_cache plugin

## Purpose
Wipes the Stremio server cache directory on a schedule and on demand from Telegram. Useful when the cache grows large or becomes stale.

## Configuration (`plugins.yml`)
```yaml
enabled:
  stremio_cache:
    container: "stremio-server"                   # Docker container name
    path: "/root/.stremio-server/stremio-cache"   # cache directory inside the container
    schedule: "0 3 * * 0"                         # optional cron in configured TZ (default UTC); omit for manual-only
```

## Actions and buttons
- Button: `🧹 Stremio cache` → callback `p.stremio_cache:report` (report-first screen)
- From report screen:
  - `✅ Wipe cache` → confirmation screen → `p.stremio_cache:run`
  - `🔄 Refresh`
  - `◀️ Plugins`

## What it executes
1. Measures cache size with `docker exec <container> du -sb <path>`.
2. Wipes contents with `docker exec <container> rm -rf <path>/*` (keeps the directory itself).
3. Reports how many bytes were freed.

## Scheduling
- If `schedule` is set, registers cron job `stremio_cache.scheduled`.
- If `schedule` is omitted, plugin runs in manual-only mode.
- Cron expression is interpreted in the timezone set via `TZ` (default UTC).
- If `container` or `path` is empty, plugin logs a warning and does not register.

## Output and failure behavior
- Manual runs start from a cache report screen and remain confirmation-gated before deletion.
- Scheduled runs log the result silently.
- Wipe timeout is 120 seconds; size-check timeout is 30 seconds.
- Plugin exit buttons return to the Plugins list (`plugins_menu`).

## Requirements
- Docker socket must be mounted to the bot container.
- The Stremio container must be running for `docker exec` to work.
