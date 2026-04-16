"""
Persistent container registry.

Remembers every container the bot has ever observed so that families
remain visible after `docker compose down` (which removes containers
from Docker entirely).

Storage: JSON file at REGISTRY_PATH (default /data/registry.json,
bind-mounted from ./bot-data on the host). Writes survive restarts.

Auto-expiry: entries not refreshed within EXPIRE_DAYS days are silently
dropped on load. Users can also remove entries immediately with forget().
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

EXPIRE_DAYS = 30
_DEFAULT_PATH = os.environ.get(
    "REGISTRY_PATH",
    "/data/registry.json",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_expired(entry: dict) -> bool:
    try:
        last = datetime.fromisoformat(entry["last_seen"])
        return datetime.now(timezone.utc) - last > timedelta(days=EXPIRE_DAYS)
    except Exception:
        return False


def load(path: str = _DEFAULT_PATH) -> dict:
    """
    Read registry from disk. Returns {} if file missing or corrupt.
    Expired entries (last_seen > EXPIRE_DAYS ago) are filtered out and
    the file is rewritten if any were pruned.
    """
    try:
        with open(path) as f:
            data: dict = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Registry load failed (%s) — starting fresh", exc)
        return {}

    before = len(data)
    data = {k: v for k, v in data.items() if not _is_expired(v)}
    if len(data) < before:
        logger.info("Registry: pruned %d expired entries", before - len(data))
        save(data, path)
    return data


def save(data: dict, path: str = _DEFAULT_PATH) -> None:
    """Atomic write via tempfile + os.replace."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception as exc:
        logger.error("Registry save failed: %s", exc)


def upsert(container, path: str = _DEFAULT_PATH) -> None:
    """
    Record a live container into the registry (or refresh its last_seen).
    Reads, updates, writes atomically.
    """
    labels = container.labels or {}
    working_dir = labels.get("com.docker.compose.project.working_dir", "")
    config_files = labels.get("com.docker.compose.project.config_files", "")
    compose_file = config_files.split(",")[0].strip() if config_files else ""
    if not compose_file and working_dir:
        for fname in ("docker-compose.yml", "compose.yml"):
            candidate = os.path.join(working_dir, fname)
            if os.path.isfile(candidate):
                compose_file = candidate
                break

    image = container.attrs.get("Image", "") or ""

    entry = {
        "name": container.name,
        "family": labels.get("com.docker.compose.project", container.name),
        "service": labels.get("com.docker.compose.service", container.name),
        "working_dir": working_dir,
        "compose_file": compose_file,
        "image": image,
        "last_seen": _now_iso(),
    }
    data = load(path)
    data[container.name] = entry
    save(data, path)


def upsert_many(containers: list, path: str = _DEFAULT_PATH) -> None:
    """
    Record multiple live containers in a single read+write cycle.
    Much cheaper than calling upsert() per container on an SD card.
    """
    if not containers:
        return
    data = load(path)
    for container in containers:
        labels = container.labels or {}
        working_dir = labels.get("com.docker.compose.project.working_dir", "")
        config_files = labels.get("com.docker.compose.project.config_files", "")
        compose_file = config_files.split(",")[0].strip() if config_files else ""
        if not compose_file and working_dir:
            for fname in ("docker-compose.yml", "compose.yml"):
                candidate = os.path.join(working_dir, fname)
                if os.path.isfile(candidate):
                    compose_file = candidate
                    break
        image = container.attrs.get("Image", "") or ""
        data[container.name] = {
            "name": container.name,
            "family": labels.get("com.docker.compose.project", container.name),
            "service": labels.get("com.docker.compose.service", container.name),
            "working_dir": working_dir,
            "compose_file": compose_file,
            "image": image,
            "last_seen": _now_iso(),
        }
    save(data, path)


def forget(name: str, path: str = _DEFAULT_PATH) -> None:
    """Remove a single entry from the registry."""
    data = load(path)
    if name in data:
        del data[name]
        save(data, path)
        logger.info("Registry: forgot %s", name)


def all_entries(path: str = _DEFAULT_PATH) -> dict:
    """Return all non-expired registry entries."""
    return load(path)
