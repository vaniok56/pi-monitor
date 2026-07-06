# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
cp .env.example .env   # then set BOT_TOKEN and ALLOWED_USER_IDS
```

Required env vars: `BOT_TOKEN`, `ALLOWED_USER_IDS`. Set `DESKTOP_PATH` to actual Desktop path (used for subprocess `docker compose` calls inside the container).

## Running

```bash
# Start bot only
docker compose up -d

# With Beszel + Portainer monitoring stack
docker compose --profile monitoring up -d

# View live logs
docker logs -f pi-control-bot
```

## Deploy to remote host

```bash
./deploy.sh           # full rsync + rebuild
./deploy.sh bot       # bot/ code only, faster iteration
./deploy.sh config    # bot/config/ only, no rebuild

PI_USER=pi PI_HOST=mypi.local ./deploy.sh
```

## Architecture

All alert sources (3 background threads) push `AlertItem` objects into a single async notifier queue. One consumer deduplicates within the cooldown window and sends to all `ALLOWED_USER_IDS`.

**Threads started in `main.py` → `_post_init`:**
- `DockerEventsMonitor` (`alerts/events.py`) — listens to Docker socket for container exit/restart/unhealthy events
- `HostWatchdog` (`alerts/host.py`) — polls CPU load, RAM, swap, disk, temperature
- `LogLoopManager` (`alerts/logloop.py`) — spawns one log-streaming thread per running container; fingerprints repeated error patterns

**`alerts/notifier.py`** — thread-safe queue bridge between sync threads and asyncio; handles dedup + cooldown

**`commands.py`** — all PTB command handlers (`/start`, `/status`, `/testalert`, `/help`) and `handle_callback` for inline keyboard actions

**`registry.py`** — persists container→compose-family mapping to `REGISTRY_PATH` (default `/data/registry.json`); survives `compose down` so "ghost" families stay visible

**`docker_ops.py`** — Docker SDK wrappers for start/stop/restart/rebuild/logs; rebuild streams build output back to the Telegram message

**`config.py`** — typed `Config` dataclass loaded from env via `Config.from_env()`

## Plugin system

`bot/config/plugins.yml` lists enabled plugins under `enabled:` key. Each plugin is a subdirectory in `bot/plugins/` with `__init__.py` exposing:
- `META: PluginMeta` (optional) — declares `requires_platform` to gate loading by host class
- `register(ctx: PluginContext)` — entry point called at startup

`PluginContext` (`plugins/_ctx.py`) gives each plugin access to `notifier`, `watchdog`, `scheduler`, `actions` (callback registry), `buttons` (inline keyboard registry), `mute_store`, and its own `plugin_cfg` slice.

`ScopedActionRegistry` (`plugins/_registry.py`) wraps the shared `ActionRegistry` so plugin handlers receive their own `PluginContext`, not the base context. Action names must be globally unique (collision raises `ValueError`).

`ButtonRegistry.add(label, callback_data, sort_key=100)` — lower `sort_key` appears first in `/status` inline keyboard.

## Mute store

`bot/mute_store.py` — thread-safe, JSON-backed (`/data/mutes.json` → `./bot-data/mutes.json` on host). Scope: `container`, `family`, or `all`. `until`: ISO-8601 UTC string or `"forever"`. `is_muted()` checks all three scopes and auto-purges expired entries on read.

## Log rules

`bot/config/log_rules.yml` controls per-container log-loop detection (interesting/ignore patterns, threshold, window, cooldown). Changes take effect on bot restart. Containers not listed inherit `defaults`.
