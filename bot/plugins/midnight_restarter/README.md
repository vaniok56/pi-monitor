# midnight_restarter plugin

## Purpose
Shows a planned-restarts view (targets, states, auto mode, next run) and restarts a configured allowlist of containers on demand. Automatic restarts are optional.

## Configuration (`plugins.yml`)
```yaml
enabled:
  midnight_restarter:
    containers:
      - stremio-server
    time: "04:00"   # optional HH:MM in configured TZ (default UTC); omit for manual-only
```

## Actions and buttons
- Button: `🔁 Night restarter` → callback `p.midnight_restarter:menu` (planned-restarts screen)
- From planned-restarts screen:
  - `✅ Restart now` → confirmation → `p.midnight_restarter:run`
  - `🔄 Refresh`
  - `◀️ Plugins`

## What it executes
- For each configured container:
  - `docker restart <container_name>`

## Scheduling
- If `time` is set, registers daily job `midnight_restarter.scheduled`.
- If `time` is omitted, plugin runs in manual-only mode.
- `time` is interpreted in the timezone set via `TZ` (default UTC).
- If `containers` is empty, plugin logs a warning and does not register.

## Output and failure behavior
- Manual run is no longer immediate; it starts from a planned-restarts screen and then confirmation.
- Manual run result posts per-container status lines:
  - `✅ <name> restarted` on success
  - `❌ <name>: <stderr>` on failure
- Scheduled run logs the result string.
- Per-container timeout is 60 seconds.
- Plugin exit buttons return to the Plugins list (`plugins_menu`).

## Safety notes
- Limit `containers` to services safe to restart unattended.
- Requires Docker CLI access from the bot container (Docker socket mount).
