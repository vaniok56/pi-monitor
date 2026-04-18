# rpi_throttle_watch plugin

## Purpose
Monitors Raspberry Pi CPU throttling and under-voltage conditions using `vcgencmd`. Sends a Telegram alert when any active throttle flag is detected.

## Configuration (`plugins.yml`)
```yaml
enabled:
  rpi_throttle_watch:
    interval_seconds: 300   # optional auto-check interval (omit for manual-only)
```

## Actions and buttons
- Button: `⚡ Pi throttle` → callback `p.rpi_throttle_watch:menu`
- Manual report shows current `vcgencmd get_throttled` value, decoded active flags, and auto-mode status.
- Actions: `🔄 Recheck`, `◀️ Plugins`

## What it checks
Runs `vcgencmd get_throttled` and inspects the returned bitmask for these active flags:

| Flag | Meaning |
|------|---------|
| Bit 0 | Under-voltage now |
| Bit 1 | ARM frequency capped now |
| Bit 2 | Currently throttled |
| Bit 3 | Soft temperature limit active |

If any flag is set, fires a `HOST_RESOURCE` alert with the raw hex value and a plain-text description of each active condition.

## Scheduling
- If `interval_seconds` is set, registers repeating interval job `rpi_throttle_watch.check`.
- If `interval_seconds` is omitted, plugin runs in manual-only mode.
- Only runs on hosts where `vcgencmd` is available (`requires_platform: rpi`).

## Output and failure behavior
- Includes Telegram manual report UI via plugin button.
- Alerts are deduplicated by the hex throttle value (`rpi_throttle:<hex>`), so a new alert fires only when the throttle state changes.
- If `vcgencmd` is missing or returns unexpected output, a warning is logged and no alert is sent.

## Requirements
- Must run on a Raspberry Pi with `vcgencmd` in `PATH`.
- Plugin is automatically skipped if `vcgencmd` capability is not detected at registration time.
