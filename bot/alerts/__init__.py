"""Alert data types shared across all alert sources."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AlertType(str, Enum):
    CRASH = "crash"
    RESTART_LOOP = "restart_loop"
    UNHEALTHY = "unhealthy"
    HOST_RESOURCE = "host_resource"
    LOG_LOOP = "log_loop"
    LOG_FLOOD = "log_flood"


@dataclass
class AlertItem:
    type: AlertType
    title: str
    body: str
    # de-dup key — same key = same incident, subject to cooldown
    key: str
    container: Optional[str] = None
    # If True, show Restart / Rebuild buttons in the alert message
    show_container_buttons: bool = False
    # If set, show a "Silence this signature" button
    sig_hash: Optional[str] = None
