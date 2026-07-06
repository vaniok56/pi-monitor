# restic_backup_watcher

Checks restic snapshot age and alerts if the newest snapshot is older than `max_age_hours`.

## Requirements

- `restic` binary — already added to the bot's Dockerfile
- `RESTIC_PASSWORD` env var in `.env` (or use `password_file` / inline `password`)
- Restic repo accessible inside the container (e.g. `/mnt/backup-ssd/restic-repo`)

## Config

```yaml
restic_backup_watcher:
  repo: /mnt/backup-ssd/restic-repo
  password_env: RESTIC_PASSWORD   # env var name; default RESTIC_PASSWORD
  # password_file: /data/restic-password  # path inside container
  max_age_hours: 26               # alert if newest snapshot > this age
  schedule: "0 8 * * *"          # daily check at 08:00
```

## Button

`💾 Restic Backup` — shows last 5 snapshots with age and stale status.
