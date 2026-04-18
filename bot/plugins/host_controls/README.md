# host_controls plugin

## Purpose
Provides high-impact host and bot control actions in Telegram with explicit confirmation prompts.

## Configuration (`plugins.yml`)
```yaml
enabled:
  host_controls: {}
```

This plugin has no configurable keys.

## Actions and buttons
- `🔄 Restart bot` → `p.host_controls:restart_bot` → confirm → `restart_bot_confirm`
- `💿 Drop caches` → `p.host_controls:drop_caches` → confirm → `drop_caches_confirm`
- `🔁 Reboot host` → `p.host_controls:reboot` → confirm → `reboot_confirm`
- `⏹ Shutdown host` → `p.host_controls:shutdown` → confirm → `shutdown_confirm`

## Execution model
- **Restart bot** — runs `docker restart <bot_container_name>` via the Docker socket.
- **Drop caches / Reboot / Shutdown** — run through host namespaces using `nsenter -t 1 ...` from a privileged ephemeral helper container, so commands execute on the host OS (not only inside the helper container).
- Helper image defaults to `debian:12-slim` (contains `nsenter`). You can override via `HOST_CONTROLS_HELPER_IMAGE` env var.

## Output and failure behavior
- Each confirm action updates the Telegram message with `✅`/`❌` status and command output (truncated).
- Command timeouts are 30-60 seconds depending on action.
- On reboot/shutdown success, the host may go down immediately after "⏳" message.
- On reboot/shutdown failure, plugin now shows explicit `❌` error output and a back button.
- Exit navigation returns to the Plugins list (`plugins_menu`).

## Operational requirements
- Docker socket must be mounted to the bot container.
- Host must allow privileged containers for host-level actions.

## Safety notes
- All four actions require an explicit confirmation tap before executing.
- Reboot and shutdown affect the whole machine; do not confirm during testing.
