# pi-monitor: plugin system + cross-platform refactor + 15 starter plugins

## Context

`pi-monitor` runs on the user's Raspberry Pi 4B and is being deployed to a friend's Mac mini 2012 (Debian, no GUI). Today it is a monolithic Telegram bot — every callback action is in one big `if/elif` chain in `bot/commands.py:297`, alerts are wired statically in `bot/main.py`, and recurring tasks live in *separate* sidecar containers (`docker-prune`, plus the user's own ad-hoc `midnight-restarter` and `stremio-cache-prune` containers on the Pi). There is no in-bot scheduler, no plugin loader, no host-identity in alerts, and a handful of Pi-specific assumptions (vcgencmd, `/home/pi` defaults).

This spec adds a plugin system so opt-in features can be enabled per host, refactors enough of the monolith to make plugins safe to add, polishes cross-platform compatibility so the same codebase runs cleanly on both the Pi and the Mac mini, adds a per-container alert-mute feature, and ships **15 starter plugins** that absorb the existing sidecar containers and add convenience automations the user has asked for.

Goal: one bot codebase, two hosts, opt-in features, a clear extension point for future features.

## Architecture

### Plugin system
- `bot/plugins/<name>/__init__.py` — each plugin exports `register(ctx) -> None` and (optionally) `META = PluginMeta(...)` with `requires_platform`, `default_config`, `description`.
- `bot/plugins/_loader.py` — reads `bot/config/plugins.yml`, instantiates `PluginContext`, calls `register(ctx)` on each enabled plugin. Skips plugins whose `requires_platform` doesn't match `ctx.host_class` (logs INFO, no error).
- `bot/plugins/_ctx.py` — `PluginContext` dataclass (frozen) with: `app` (PTB Application), `notifier`, `watchdog`, `log_loop_manager`, `cfg` (existing `Config`), `scheduler` (thin wrapper over PTB `JobQueue` already in `requirements.txt`), `actions` (ActionRegistry), `buttons` (ButtonRegistry), `host_class`, `host_label`, `plugin_cfg` (this plugin's config slice from `plugins.yml`).
- `bot/plugins/_registry.py` —
  - `ActionRegistry`: maps `action_prefix` → handler `async (query, parts, ctx) -> None`. Replaces the `if/elif` at `commands.py:297`.
  - `ButtonRegistry`: ordered list of `(label, callback_data, sort_key)` tuples appended to a new "Plugins" submenu off `_main_menu_keyboard`.
- Action namespacing: core actions stay bare (`rebuild:foo`); plugin actions are prefixed (`p.stremio_cache:run`). Loader rejects collisions at startup.

### Scheduling
- Use **PTB `JobQueue`** — already pulled in via `python-telegram-bot[job-queue]` in `bot/requirements.txt:1`. No new dependency.
- `Scheduler` wrapper in `bot/plugins/_scheduler.py` exposes `every(interval, callback, name)`, `daily_at(hh_mm, callback, name)`, and `cron(expr, callback, name)` (cron via `croniter` — new dep). Plugins call these in their `register()`.

### Cross-platform
- `bot/host_info.py` — runs once at startup. Returns `HostInfo(host_class, host_label, capabilities)`:
  - `host_class`: one of `rpi`, `debian_amd64`, `debian_arm64`, `mac_intel`, `mac_apple_silicon`, `linux_other`. Detected via `platform.machine()` + `/sys/firmware/devicetree/base/model` probe.
  - `host_label`: `HOST_LABEL` env override, else `socket.gethostname()`.
  - `capabilities`: dict of bool flags (`vcgencmd`, `smartctl`, `apt`, `systemctl`) — probed at startup, plugins read these.
- `bot/alerts/notifier.py:dispatch()` — prefix every alert with `[{host_label}]`.
- `bot/config.py` — drop the `/home/pi/Desktop` default for `DESKTOP_PATH`; require it explicitly. Update `.env.example` with both Pi and Mac mini sample stanzas.
- `commands.py:385` — replace hardcoded "May take a few minutes on the Pi" with neutral wording.

### Per-container alert mute (core, not plugin)
- `bot/mute_store.py` — JSON-backed (`bot-data/mutes.json`), thread-safe set of `{family, container, alert_type, until_iso}`. Methods: `is_muted(...)`, `mute(...)`, `unmute(...)`, `cleanup_expired()`.
- `bot/alerts/notifier.py:dispatch()` — consults `mute_store.is_muted()` before sending; muted alerts are dropped silently (counter only).
- New buttons on container detail (`commands.py:_container_keyboard`): "🔕 Mute (1h / 24h / forever)" → "🔔 Unmute".
- New button on family view: "🔕 Mute family".

### plugins.yml (YAML-only for v1)
```yaml
# bot/config/plugins.yml
enabled:
  docker_prune:
    schedule: "0 3 * * 0"
    aggressive: false
  host_controls: {}
  midnight_restarter:
    containers: [stremio-server]
    time: "04:00"
  stremio_cache:
    path: /home/raspik4b/Desktop/stremio-server/cache
    schedule: "0 3 * * 0"
  rpi_throttle_watch:
    interval_seconds: 300
  morning_digest:
    time: "09:00"
  # ... etc
```
README documents that **dynamic Telegram-toggle is on the roadmap** but v1 requires editing `plugins.yml` + bot restart.

## File map

### New files
| Path | Purpose |
|---|---|
| `bot/plugins/__init__.py` | exports `load_plugins(ctx)` |
| `bot/plugins/_loader.py` | reads `plugins.yml`, builds `PluginContext`, calls `register()` |
| `bot/plugins/_ctx.py` | `PluginContext` dataclass + `PluginMeta` |
| `bot/plugins/_registry.py` | `ActionRegistry`, `ButtonRegistry` |
| `bot/plugins/_scheduler.py` | thin wrapper over PTB `JobQueue` |
| `bot/plugins/<name>/__init__.py` | one file per plugin (15 total — see Plugin list) |
| `bot/host_info.py` | platform/capability detection |
| `bot/mute_store.py` | per-container mute persistence |
| `bot/config/plugins.yml.example` | annotated sample for both Pi + Mac mini |

### Touched files
| Path | Change |
|---|---|
| `bot/main.py:109-138` | load plugins after notifier/watchdog init; pass `ctx` |
| `bot/commands.py:297` | replace `if/elif` chain with `ACTION_REGISTRY[prefix](...)` |
| `bot/commands.py:62-88` | add "🧩 Plugins" button to `_main_menu_keyboard`; new `_plugins_menu_keyboard` |
| `bot/commands.py:_container_keyboard` | add mute/unmute buttons |
| `bot/alerts/notifier.py:dispatch()` | host-label prefix + mute-store consult |
| `bot/config.py` | drop Pi-default; add `HOST_LABEL`, `PLUGINS_YML_PATH` |
| `bot/requirements.txt` | add `croniter`, `wakeonlan`, `PyYAML` (if not present) |
| `docker-compose.yml:21-34` | **remove** `docker-prune` service (replaced by plugin); upgrade-notes added to README |
| `README.md` | new "Plugins" section, cross-platform setup, dual-host story, Telegram-toggle roadmap note |
| `.env.example` | dual-stanza sample (Pi + Mac mini), drop pi-specific defaults |
| `deploy.sh:18-20` | drop `pi`/`raspberrypi.local` defaults; require explicit env or `.deploy.local` sourced file |

## Plugin list (15)

### Cleanup / replaces existing sidecars
1. **`docker_prune`** — `docker image prune -f && docker builder prune -f && docker volume prune -f`. Schedule (cron) + manual button + "aggressive mode" (`system prune -a --volumes`) gated behind confirmation. Replaces the `docker-prune` compose service.
2. **`midnight_restarter`** — `compose restart` of whitelisted containers at configured time. Replaces user's `midnight-restarter` container.
3. **`stremio_cache`** — wipe configured path, scheduled + manual. Replaces user's `stremio-cache-prune` container.
4. **`cobalt_temp_cleanup`** — same pattern, configurable path + age threshold.
5. **`telegram_bot_api_cleanup`** — purge files older than N days from configured path (default `/var/lib/telegram-bot-api`).

### Host control
6. **`host_controls`** — buttons: reboot host, shutdown host, restart bot, drop caches. All destructive ops gated behind two-step confirm. Reboot/shutdown require host PID-namespace (the bot already mounts the docker socket so it shells out via a privileged side-container or `nsenter` — design note: pick **`docker run --rm --pid=host --privileged alpine reboot`** at exec-time to avoid persistent privilege).
7. **`wol_sender`** — manual buttons per configured target MAC. Uses `wakeonlan` Python lib.

### Monitoring
8. **`rpi_throttle_watch`** — Pi-only (`requires_platform=[rpi]`). Polls `vcgencmd get_throttled` every N seconds, alerts on undervoltage / throttling bits.
9. **`smart_disk_health`** — `requires_capability=smartctl`. Daily `smartctl -H` + selected attrs. Auto-disabled on Pi SD card.
10. **`disk_fill_eta`** — collects `df` samples in a 7-day ring buffer (`bot-data/disk_history.json`); linear-fits trend; alerts when ETA < N days.
11. **`image_update_notifier`** — daily `docker pull --quiet` per running image to compare digests; alert with **manual approve-pull button** per stale image. No auto-pull.
12. **`public_ip_watch`** — daily `curl -s ifconfig.me`; alert on change.
13. **`bot_health_check`** — for each whitelisted bot container, send a Telegram cmd via the bot-API + verify reply within timeout. Uses **a separate Telegram client identity** (configured in plugin cfg) to avoid loopback.

### UX
14. **`morning_digest`** — daily message at chosen time: containers up/down, overnight alerts, disk %, top 3 RAM hogs, uptime.
15. **`maintenance_window`** — daily window (e.g. `23:00-07:00`) during which all alerts are suppressed; one-shot button "🤫 Silence next 2h"; uses `mute_store` with wildcard.

## Phases

### Phase 1 — foundation (small, high-leverage, ship-first)
- Plugin loader, `_ctx`, `_registry`, `_scheduler`, `host_info`
- Refactor `commands.py:297` dispatch to `ActionRegistry`
- Host label + cross-platform polish (capability probe, drop Pi defaults, `.env.example` rewrite)
- Plugins: `docker_prune`, `midnight_restarter`, `host_controls`
- Add a comprehensive `README.md` for each implemented phase-1 plugin under `bot/plugins/<plugin>/`
- Remove `docker-prune` service from `docker-compose.yml` with README upgrade note
- README: "Plugins" section + cross-platform setup

### Phase 2 — cleanup + monitoring + mute
- `mute_store` + notifier integration + mute buttons
- Plugins: `stremio_cache`, `cobalt_temp_cleanup`, `telegram_bot_api_cleanup`, `wol_sender`, `rpi_throttle_watch`, `smart_disk_health`, `disk_fill_eta`
- Add a comprehensive `README.md` for each implemented phase-2 plugin under `bot/plugins/<plugin>/`

### Phase 3 — UX + polish
- Plugins: `image_update_notifier`, `morning_digest`, `public_ip_watch`, `bot_health_check`, `maintenance_window`
- Add a comprehensive `README.md` for each implemented phase-3 plugin under `bot/plugins/<plugin>/`
- README "Telegram-toggle planned" callout
- Final dual-host deploy walkthrough

User pauses between phases for review.

## Critical files to read before implementing

| File | Why |
|---|---|
| `bot/commands.py:62-575` | dispatch chain + keyboard builders — biggest refactor surface |
| `bot/main.py:70-138` | startup wiring, where plugins get loaded |
| `bot/alerts/notifier.py` | queue + dispatch — mute + host-label hooks here |
| `bot/alerts/host.py` | existing pattern for capability-gated probes (vcgencmd try/except) |
| `bot/config.py` | env-loading pattern; new `HOST_LABEL`, `PLUGINS_YML_PATH` go here |
| `docker-compose.yml:21-34` | the `docker-prune` service this spec removes |
| `bot/registry.py` | how the bot persists state — mirror its locking pattern in `mute_store.py` |

## Verification

### Phase 1
- Unit-style smoke: `python -c "from bot.plugins import load_plugins; ..."` in dev shell — loader picks up enabled plugins, skips wrong-platform.
- On raspi4b: `./deploy.sh full`, confirm `pi-control-bot` starts, `docker ps` no longer shows `docker-prune`, `/start` shows new "🧩 Plugins" button, manual prune button runs and reports bytes freed, scheduled prune fires at the configured cron time (test with a 1-min cron during dev).
- `host_controls`: tap "Restart bot" — bot self-restarts within 10s. Tap "Reboot host" → confirm dialog appears; **do not confirm during testing**.
- Cross-platform smoke on Mac mini: rsync, set `HOST_LABEL=macmini`, set `DESKTOP_PATH`, `docker compose up -d --build`, confirm `/status` reports `[macmini]` prefix, `/start` works, `rpi_throttle_watch` is absent from menu (gated out), capability probe logs show `vcgencmd=False, smartctl=True, apt=True`.

### Phase 2
- `/testalert crash` → mute container 1h → `/testalert crash` again → no message delivered → wait until expiry → `/testalert crash` → message delivered.
- Scheduled `stremio_cache` deletes test file in cache path; manual button reports bytes freed.
- `disk_fill_eta`: fabricate 7 disk samples by writing to `bot-data/disk_history.json`, restart bot, confirm ETA alert fires when projected fill < threshold.
- `rpi_throttle_watch` on Pi: temporarily lower threshold to fire immediately, confirm alert.
- `smart_disk_health` on Mac mini: confirm SMART read works; on Pi confirm plugin auto-disabled.

### Phase 3
- `morning_digest` fires at configured time (test with time set 2 min in future).
- `image_update_notifier`: bump a tag manually (`docker pull alpine:latest` after deleting local), confirm next daily run produces alert + approve-pull button works.
- `public_ip_watch`: stub `ifconfig.me` to a local file, change value, confirm alert.
- `bot_health_check`: kill a whitelisted bot container's process inside (without stopping container), confirm alert fires within configured timeout.
- `maintenance_window`: set window to current ±5min, fire `/testalert host`, confirm suppressed.

### Cross-cutting
- Both hosts run end of Phase 3 with identical bot version, host-specific `plugins.yml`, host-specific `.env`. Alerts arrive prefixed with hostname so you can tell them apart in the same Telegram chat.

## Out of scope (explicitly deferred)
- Telegram-toggle UI for plugins (planned, mentioned in README)
- `backup_runner` plugin (rclone + credential storage — its own spec)
- Multi-host federation (one bot managing both hosts) — separate deploys for v1
- TUI setup wizard
- Alert digest/batching mode
- Sparkline metric charts
- Multi-language UI
