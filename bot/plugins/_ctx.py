from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PluginMeta:
    name: str
    description: str = ""
    requires_platform: tuple = ()
    default_config: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PluginContext:
    app: Any                  # PTB Application
    notifier: Any             # Notifier
    watchdog: Any             # HostWatchdog
    log_loop_manager: Any     # LogLoopManager
    cfg: Any                  # Config
    scheduler: Any            # Scheduler
    actions: Any              # ActionRegistry
    buttons: Any              # ButtonRegistry
    host_class: str
    host_label: str
    host_capabilities: dict
    plugin_cfg: dict          # this plugin's config slice from plugins.yml
    mute_store: Any = None    # MuteStore instance (core, not a plugin)
