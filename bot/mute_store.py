"""
Persistent, thread-safe mute store.

Mutes suppress alert delivery for a scoped target (container / family / all)
with an optional expiry. Storage: JSON at /data/mutes.json (bind-mounted from
./bot-data/mutes.json on the host).

Schema:
  {"mutes": [
    {"scope": "container", "target": "my-box", "alert_type": null,
     "until": "2026-04-17T12:30:00+00:00", "created": "..."},
    {"scope": "family",    "target": "mystack", "alert_type": null, "until": "forever", ...},
    {"scope": "all",       "target": "*",        "alert_type": null, "until": "forever", ...}
  ]}

until: ISO-8601 UTC string | "forever" | None (both forever variants never expire).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_PATH = os.environ.get("MUTE_STORE_PATH", "/data/mutes.json")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _is_expired(entry: dict) -> bool:
    until = entry.get("until")
    if until is None or until == "forever":
        return False
    try:
        exp = datetime.fromisoformat(until)
        return _now() > exp
    except Exception:
        return False


class MuteStore:
    def __init__(self, path: str = _DEFAULT_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> list[dict]:
        try:
            with open(self._path) as f:
                data = json.load(f)
            return data.get("mutes", [])
        except FileNotFoundError:
            return []
        except Exception as exc:
            logger.warning("MuteStore load failed (%s) — starting empty", exc)
            return []

    def _save(self, mutes: list[dict]) -> None:
        p = Path(self._path)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump({"mutes": mutes}, f, indent=2)
            os.replace(tmp, self._path)
        except Exception as exc:
            logger.error("MuteStore save failed: %s", exc)

    # ── Public API ────────────────────────────────────────────────────────────

    def mute(
        self,
        scope: str,
        target: str,
        until: Optional[str],
        alert_type: Optional[str] = None,
    ) -> None:
        """Add or replace a mute entry."""
        with self._lock:
            mutes = self._load()
            mutes = [
                m for m in mutes
                if not (m["scope"] == scope and m["target"] == target
                        and m.get("alert_type") == alert_type)
            ]
            mutes.append({
                "scope": scope,
                "target": target,
                "alert_type": alert_type,
                "until": until if until is not None else "forever",
                "created": _now_iso(),
            })
            self._save(mutes)

    def unmute(self, scope: str, target: str) -> None:
        """Remove all mute entries matching scope+target."""
        with self._lock:
            mutes = self._load()
            mutes = [
                m for m in mutes
                if not (m["scope"] == scope and m["target"] == target)
            ]
            self._save(mutes)

    def is_muted(
        self,
        container: Optional[str],
        family: Optional[str],
        alert_type: Optional[str],
    ) -> bool:
        """Return True if alert should be suppressed."""
        with self._lock:
            mutes = self._load()
            active = [m for m in mutes if not _is_expired(m)]
            if len(active) < len(mutes):
                self._save(active)

        for m in active:
            m_type = m.get("alert_type")
            if m_type is not None and m_type != alert_type:
                continue
            scope = m["scope"]
            target = m["target"]
            if scope == "all":
                return True
            if scope == "family" and family is not None and target == family:
                return True
            if scope == "container" and container is not None and target == container:
                return True
        return False

    def list_mutes(self) -> list[dict]:
        with self._lock:
            mutes = self._load()
            return [m for m in mutes if not _is_expired(m)]

    def cleanup_expired(self) -> int:
        """Remove expired entries; return count removed."""
        with self._lock:
            mutes = self._load()
            active = [m for m in mutes if not _is_expired(m)]
            removed = len(mutes) - len(active)
            if removed:
                self._save(active)
            return removed
