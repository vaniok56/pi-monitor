"""Load and expose typed configuration from environment variables."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import FrozenSet
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_users: FrozenSet[int]
    base_url: str          # e.g. "http://telegram-bot-api:8081/bot"
    base_file_url: str     # e.g. "http://telegram-bot-api:8081/file/bot"
    desktop_path: str      # absolute host path to ~/Desktop
    host_label: str        # human-readable name for this host in alerts
    plugins_yml_path: str  # path to plugins.yml inside the container
    mute_store_path: str   # path to mutes.json inside the container

    # Timezone
    tz: ZoneInfo
    tz_name: str

    # Alert thresholds
    disk_threshold_pct: float
    ram_threshold_pct: float
    swap_threshold_pct: float
    cpu_load_threshold: float
    temp_threshold_c: float
    alert_cooldown_minutes: int

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ["BOT_TOKEN"]

        raw_ids = os.environ.get("ALLOWED_USER_IDS", "")
        users: FrozenSet[int] = frozenset(
            int(x.strip()) for x in raw_ids.split(",") if x.strip()
        )

        base_url = os.environ.get(
            "TELEGRAM_API_BASE_URL", "https://api.telegram.org/bot"
        ).rstrip("/")
        # Derive file URL from base URL
        if "/bot" in base_url:
            base_file_url = base_url.replace("/bot", "/file/bot", 1)
        else:
            base_file_url = "https://api.telegram.org/file/bot"

        desktop_path = os.environ.get("DESKTOP_PATH", "").strip()
        if not desktop_path:
            raise ValueError(
                "DESKTOP_PATH is required. Set DESKTOP_PATH to an absolute host path "
                "(for example: /home/pi/Desktop or /home/admin/Desktop)."
            )
        if not desktop_path.startswith("/"):
            raise ValueError(
                f"DESKTOP_PATH must be an absolute path, got: {desktop_path!r}"
            )

        raw_tz = os.environ.get("TZ", "UTC").strip() or "UTC"
        try:
            tz = ZoneInfo(raw_tz)
            tz_name = raw_tz
        except ZoneInfoNotFoundError:
            _log.warning("Unknown TZ %r — falling back to UTC", raw_tz)
            tz = ZoneInfo("UTC")
            tz_name = "UTC"

        return cls(
            bot_token=token,
            allowed_users=users,
            base_url=base_url,
            base_file_url=base_file_url,
            desktop_path=desktop_path,
            host_label=os.environ.get("HOST_LABEL", ""),
            plugins_yml_path=os.environ.get("PLUGINS_YML_PATH", "/app/config/plugins.yml"),
            mute_store_path=os.environ.get("MUTE_STORE_PATH", "/data/mutes.json"),
            tz=tz,
            tz_name=tz_name,
            disk_threshold_pct=float(os.environ.get("DISK_THRESHOLD_PCT", "90")),
            ram_threshold_pct=float(os.environ.get("RAM_THRESHOLD_PCT", "90")),
            swap_threshold_pct=float(os.environ.get("SWAP_THRESHOLD_PCT", "80")),
            cpu_load_threshold=float(os.environ.get("CPU_LOAD_THRESHOLD", "3.0")),
            temp_threshold_c=float(os.environ.get("TEMP_THRESHOLD_C", "75")),
            alert_cooldown_minutes=int(os.environ.get("ALERT_COOLDOWN_MINUTES", "10")),
        )
