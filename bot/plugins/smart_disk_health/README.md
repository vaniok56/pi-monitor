# smart_disk_health plugin

## Purpose
Runs a daily S.M.A.R.T. health check on configured block devices and sends a Telegram alert if problems are found.

## Configuration (`plugins.yml`)
```yaml
enabled:
  smart_disk_health:
    devices:
      - "/dev/sda"          # list of block devices to check
    schedule: "0 6 * * *"  # optional cron in configured TZ (default UTC); omit for manual-only
    allow_on_pi: false      # set true to enable on Raspberry Pi hosts
```

## Actions and buttons
- Button: `💾 SMART health` → callback `p.smart_disk_health:menu`
- Manual report runs checks for configured devices and shows healthy/warning/error status per device.
- Actions: `🔄 Recheck`, `◀️ Plugins`

## What it checks
For each device, runs `smartctl -H -A <device>` and inspects:

| Attribute | Alert condition |
|-----------|----------------|
| Overall health | `SMART overall-health: FAILED` |
| Attribute 5 — Reallocated_Sector_Ct | Value > 0 |
| Attribute 197 — Current_Pending_Sector | Value > 0 |

Temperature (attributes 190 / 194) is included in the alert message if available.

## Execution model
- `smartctl` runs inside an ephemeral privileged Alpine container with `/dev` mounted:
  `docker run --rm --privileged --network=none -v /dev:/dev alpine:latest sh -c "apk add smartmontools && smartctl -H -A <device>"`
- Per-device timeout is 120 seconds.

## Scheduling
- If `schedule` is set, registers cron job `smart_disk_health.scheduled`.
- If `schedule` is omitted, plugin runs in manual-only mode.
- Cron expression is interpreted in the timezone set via `TZ` (default UTC).

## Output and failure behavior
- Includes Telegram manual report UI via plugin button.
- Alerts fire through the bot's alert system, deduplicated by `smart:<device>`.
- If `smartctl` is not found and the host is not a Pi, the plugin is skipped at startup.
- By default disabled on Raspberry Pi (SD card / USB drives may not support S.M.A.R.T.); set `allow_on_pi: true` to override.

## Requirements
- Docker socket must be mounted to the bot container.
- Host must allow privileged containers.
