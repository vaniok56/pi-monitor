# disk_fill_eta plugin

## Purpose
Predicts when a disk will run out of space by tracking usage over time and running a linear regression. Sends a Telegram alert when the estimated time-to-full drops below a configurable threshold.

## Configuration (`plugins.yml`)
```yaml
enabled:
  disk_fill_eta:
    path: "/"                              # filesystem mount point to monitor
    threshold_days: 14                     # alert if disk will fill within this many days
    schedule: "0 */4 * * *"               # optional cron in configured TZ (default UTC); omit for manual-only
    history_path: "/data/disk_history.json"  # where to persist usage samples
```

## Actions and buttons
- Button: `📉 Disk ETA` → callback `p.disk_fill_eta:menu`
- Manual report shows current usage, sample count, ETA (when available), and threshold status.
- Actions: `➕ Sample now`, `🔄 Refresh`, `◀️ Plugins`

## What it does
1. On every scheduled run, records current disk usage (used bytes + total bytes + timestamp) to `history_path`.
2. Keeps the last 168 samples (7 days at a 4-hour interval).
3. Once at least 12 samples are collected, fits a straight line (OLS regression) to predict when the disk will be full.
4. If the estimated days-to-full is below `threshold_days`, fires a `HOST_RESOURCE` alert.

## Scheduling
- If `schedule` is set, registers cron job `disk_fill_eta.sample`.
- If `schedule` is omitted, plugin runs in manual-only mode.
- Cron expression is interpreted in the timezone set via `TZ` (default UTC).

## Output and failure behavior
- Includes Telegram manual report UI via plugin button.
- Alerts fire through the bot's alert system and are deduplicated by `disk_fill_eta:<path>`.
- If disk usage is flat or shrinking, no ETA is calculated and no alert is sent.
- History file is written atomically (temp file + rename) to avoid corruption.

## Notes
- Requires `psutil` (included in bot dependencies).
- `history_path` must be on a persistent volume if running inside Docker, otherwise history is lost on restart.
