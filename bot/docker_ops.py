"""
Docker operations: list families, start, stop, restart, rebuild, logs.

Family detection uses Docker labels:
  com.docker.compose.project        → family name
  com.docker.compose.project.config_files → compose file path
  com.docker.compose.project.working_dir  → compose dir (for rebuild)

Ghost entries (containers that existed before but are no longer in Docker)
are synthesised from the persistent registry so families remain visible
after `docker compose down`.

Rebuild flow:
  1. docker compose down       (subprocess — kills the whole project stack)
  2. docker.APIClient.build()  (Python SDK streams tar to daemon — path-safe)
  3. docker compose up -d      (subprocess — brings the whole project back up)
  4. Return last 100 log lines of the rebuilt container
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone

import timez
from typing import Optional

import docker

import registry as reg

logger = logging.getLogger(__name__)

_STATUS_EMOJI = {
    "running":    "🟢",
    "exited":     "🔴",
    "dead":       "🔴",
    "restarting": "🟡",
    "paused":     "⏸",
    "created":    "⚪",
    "gone":       "⚫",
}


# ── Entry dataclass ──────────────────────────────────────────────────────────

@dataclass
class Entry:
    """Unified representation of a container — live or ghost (registry-only)."""
    name: str
    family: str
    service: str
    status: str          # docker status or "gone" for ghosts
    working_dir: str
    compose_file: str
    image: str
    live: Optional[object] = field(default=None, repr=False)  # docker Container

    @property
    def is_ghost(self) -> bool:
        return self.live is None


def _resolve_compose_file(working_dir: str, compose_file: str) -> str:
    """Return the best compose file path for a working_dir."""
    if compose_file and os.path.isfile(compose_file):
        return compose_file
    if working_dir:
        for fname in ("docker-compose.yml", "compose.yml"):
            candidate = os.path.join(working_dir, fname)
            if os.path.isfile(candidate):
                return candidate
    return compose_file  # may be empty/nonexistent — callers handle it


def _entry_from_container(container) -> Entry:
    labels = container.labels or {}
    working_dir = labels.get("com.docker.compose.project.working_dir", "")
    config_files = labels.get("com.docker.compose.project.config_files", "")
    compose_file = config_files.split(",")[0].strip() if config_files else ""
    compose_file = _resolve_compose_file(working_dir, compose_file)

    image = container.attrs.get("Image", "") or ""

    return Entry(
        name=container.name,
        family=labels.get("com.docker.compose.project", container.name),
        service=labels.get("com.docker.compose.service", container.name),
        status=container.status,
        working_dir=working_dir,
        compose_file=compose_file,
        image=image,
        live=container,
    )


def _entry_from_registry(rec: dict) -> Entry:
    working_dir = rec.get("working_dir", "")
    compose_file = _resolve_compose_file(working_dir, rec.get("compose_file", ""))
    return Entry(
        name=rec["name"],
        family=rec.get("family", rec["name"]),
        service=rec.get("service", rec["name"]),
        status="gone",
        working_dir=working_dir,
        compose_file=compose_file,
        image=rec.get("image", ""),
        live=None,
    )


# ── Family / container discovery ─────────────────────────────────────────────

def list_families() -> dict[str, list[Entry]]:
    """
    Return all families, merging live Docker containers with registry ghosts.

    Live containers are upserted into the registry on every call.
    Registry entries not present in Docker appear as ghost entries (status="gone").
    """
    client = docker.from_env()
    live_containers = client.containers.list(all=True)

    # Build live entries and batch-upsert into registry (one read+write cycle)
    live_map: dict[str, Entry] = {}
    for c in live_containers:
        entry = _entry_from_container(c)
        live_map[c.name] = entry
    try:
        reg.upsert_many(live_containers)
    except Exception as exc:
        logger.debug("registry upsert_many failed: %s", exc)

    # Merge with registry ghosts
    all_reg = reg.all_entries()
    entries: dict[str, Entry] = dict(live_map)
    for name, rec in all_reg.items():
        if name not in entries:
            entries[name] = _entry_from_registry(rec)

    # Group by family
    groups: dict[str, list[Entry]] = {}
    for entry in entries.values():
        groups.setdefault(entry.family, []).append(entry)

    # Sort members within each family
    for members in groups.values():
        members.sort(key=lambda e: (
            0 if not e.is_ghost else 1,  # live before ghosts
            e.name,
        ))

    return dict(sorted(groups.items()))


def is_leaf_family(family_name: str, members: list[Entry]) -> bool:
    """True when family has exactly one member whose name equals the family."""
    return len(members) == 1 and members[0].name == family_name


def family_status_emoji(members: list[Entry]) -> str:
    """Aggregate emoji: 🟢 all running, 🔴 stopped, ⚫ all gone, 🟡 mixed."""
    statuses = {e.status for e in members}
    if statuses == {"running"}:
        return "🟢"
    if statuses == {"gone"}:
        return "⚫"
    if statuses <= {"exited", "dead"}:
        return "🔴"
    if statuses == {"created"}:
        return "⚪"
    return "🟡"


# ── Container info helpers ───────────────────────────────────────────────────

def container_status_emoji(status: str) -> str:
    return _STATUS_EMOJI.get(status, "❓")


def format_uptime(started_at: str) -> str:
    try:
        dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        total = int(delta.total_seconds())
        if total < 0:
            return "not started"
        d, rem = divmod(total, 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        if d:
            return f"{d}d {h}h {m}m"
        if h:
            return f"{h}h {m}m"
        return f"{m}m {s}s"
    except Exception:
        return "?"


def container_detail_text(entry: Entry) -> str:
    """Container detail view header. Uses live attrs when available."""
    emoji = container_status_emoji(entry.status)

    if entry.is_ghost:
        return (
            f"{emoji} <b>{entry.name}</b>\n"
            f"Family:  {entry.family}\n"
            f"Image:   <code>{entry.image or '?'}</code>\n"
            f"Status:  gone (not in Docker)\n"
            f"Last working dir: <code>{entry.working_dir or '?'}</code>"
        )

    container = entry.live
    attrs = container.attrs
    state = attrs.get("State", {})
    started = state.get("StartedAt", "")
    uptime = format_uptime(started) if entry.status == "running" else "—"

    created_raw = attrs.get("Created", "")
    try:
        created_dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        created = timez.fmt(created_dt, "%Y-%m-%d %H:%M")
    except Exception:
        created = "?"

    return (
        f"{emoji} <b>{entry.name}</b>\n"
        f"Family:  {entry.family}\n"
        f"Image:   <code>{entry.image}</code>\n"
        f"Created: {created}\n"
        f"Uptime:  {uptime}"
    )


def is_rebuildable(entry: Entry) -> bool:
    """A container is rebuildable if its compose working dir has a Dockerfile."""
    if not entry.working_dir:
        return False
    return os.path.isfile(os.path.join(entry.working_dir, "Dockerfile"))


# ── Actions ──────────────────────────────────────────────────────────────────

async def restart_container(name: str) -> str:
    client = docker.from_env()
    container = client.containers.get(name)
    await asyncio.to_thread(container.restart, timeout=30)
    return f"✅ <b>{name}</b> restarted."


async def stop_container(name: str) -> str:
    client = docker.from_env()
    container = client.containers.get(name)
    await asyncio.to_thread(container.stop, timeout=30)
    return f"⏹ <b>{name}</b> stopped."


async def start_container(name: str) -> str:
    """
    Start a container.
    Ghost entries (no live container): use compose up -d <service>.
    Live stopped containers: same if compose info available, else SDK start.
    """
    families = await asyncio.to_thread(list_families)
    entry: Entry | None = None
    for members in families.values():
        for e in members:
            if e.name == name:
                entry = e
                break
        if entry:
            break

    if entry and entry.working_dir and entry.service and os.path.isfile(entry.compose_file):
        res = await asyncio.to_thread(
            subprocess.run,
            ["docker", "compose", "-f", entry.compose_file, "up", "-d", entry.service],
            capture_output=True, text=True, cwd=entry.working_dir,
        )
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or res.stdout.strip())
    else:
        client = docker.from_env()
        container = client.containers.get(name)
        await asyncio.to_thread(container.start)

    return f"▶️ <b>{name}</b> started."


async def get_container_logs(name: str, tail: int = 100) -> str:
    client = docker.from_env()
    container = client.containers.get(name)
    raw = await asyncio.to_thread(container.logs, tail=tail, timestamps=True)
    return raw.decode("utf-8", errors="replace")


async def quick_logs(name: str) -> str:
    return await get_container_logs(name, tail=30)


# Log-loop interesting regex (mirrors log_rules.yml defaults)
_INTERESTING = re.compile(r"ERROR|WARN|CRITICAL|FATAL|EXCEPTION|TRACEBACK", re.IGNORECASE)


def filter_error_lines(logs: str) -> str:
    matching = [line for line in logs.splitlines() if _INTERESTING.search(line)]
    return "\n".join(matching)


async def restart_family(family_name: str) -> str:
    families = await asyncio.to_thread(list_families)
    members = families.get(family_name, [])
    if not members:
        raise ValueError(f"Family not found: {family_name}")
    results = []
    for e in members:
        if e.is_ghost:
            results.append(f"⚫ {e.name} (gone — skipped)")
            continue
        try:
            await restart_container(e.name)
            results.append(f"✅ {e.name}")
        except Exception as exc:
            results.append(f"❌ {e.name}: {exc}")
    return f"<b>Restart all — {family_name}</b>\n" + "\n".join(results)


async def stop_family(family_name: str) -> str:
    """
    docker compose down the entire family.
    Looks up working_dir from any member (live or ghost).
    """
    families = await asyncio.to_thread(list_families)
    members = families.get(family_name, [])
    if not members:
        raise ValueError(f"Family not found: {family_name}")

    # Find working dir / compose file — prefer live entry
    working_dir = ""
    compose_file = ""
    for e in sorted(members, key=lambda x: x.is_ghost):
        if e.working_dir:
            working_dir = e.working_dir
            compose_file = e.compose_file
            break

    if not working_dir or not compose_file:
        raise ValueError(
            f"No compose file found for family {family_name} — cannot run compose down."
        )

    res = await asyncio.to_thread(
        subprocess.run,
        ["docker", "compose", "-f", compose_file, "down"],
        capture_output=True, text=True, cwd=working_dir,
    )
    if res.returncode != 0:
        raise RuntimeError(f"compose down failed:\n{res.stderr.strip()}")
    return f"⏹ <b>{family_name}</b> stopped (compose down)."


async def start_family(family_name: str) -> str:
    """
    docker compose up -d the entire family (works even when all ghosts).
    """
    families = await asyncio.to_thread(list_families)
    members = families.get(family_name, [])
    if not members:
        raise ValueError(f"Family not found: {family_name}")

    working_dir = ""
    compose_file = ""
    for e in members:
        if e.working_dir:
            working_dir = e.working_dir
            compose_file = e.compose_file
            break

    if not working_dir or not compose_file:
        raise ValueError(
            f"No compose file found for family {family_name} — cannot run compose up."
        )

    res = await asyncio.to_thread(
        subprocess.run,
        ["docker", "compose", "-f", compose_file, "up", "-d"],
        capture_output=True, text=True, cwd=working_dir,
    )
    if res.returncode != 0:
        raise RuntimeError(f"compose up failed:\n{res.stderr.strip()}")
    return f"▶️ <b>{family_name}</b> started (compose up -d)."


async def rebuild_family(family_name: str) -> str:
    families = await asyncio.to_thread(list_families)
    members = families.get(family_name, [])
    if not members:
        raise ValueError(f"Family not found: {family_name}")

    working_dir = ""
    compose_file = ""
    for e in sorted(members, key=lambda x: x.is_ghost):
        if e.working_dir:
            working_dir = e.working_dir
            compose_file = e.compose_file
            break

    if not working_dir or not compose_file:
        raise ValueError(f"No compose file found for family {family_name}")

    res = await asyncio.to_thread(
        subprocess.run,
        ["docker", "compose", "-f", compose_file, "down"],
        capture_output=True, text=True, cwd=working_dir,
    )
    if res.returncode != 0:
        raise RuntimeError(f"compose down failed:\n{res.stderr.strip()}")

    build_lines: list[str] = []

    def _build() -> None:
        api = docker.APIClient(base_url="unix://var/run/docker.sock")
        for chunk in api.build(path=working_dir, rm=True, decode=True):
            if "stream" in chunk:
                build_lines.append(chunk["stream"])
            elif "error" in chunk:
                raise RuntimeError(f"Build failed: {chunk['error']}")

    await asyncio.to_thread(_build)

    res = await asyncio.to_thread(
        subprocess.run,
        ["docker", "compose", "-f", compose_file, "up", "-d"],
        capture_output=True, text=True, cwd=working_dir,
    )
    if res.returncode != 0:
        raise RuntimeError(f"compose up failed:\n{res.stderr.strip()}")

    build_tail = "".join(build_lines[-20:])
    return f"✅ <b>{family_name}</b> rebuilt.\n\n<code>{_escape(build_tail[:1500])}</code>"


async def family_merged_logs(family_name: str, tail: int = 50) -> str:
    families = await asyncio.to_thread(list_families)
    members = families.get(family_name, [])
    if not members:
        raise ValueError(f"Family not found: {family_name}")
    parts = []
    for e in members:
        if e.is_ghost:
            parts.append(f"=== {e.name} === (gone)")
            continue
        try:
            logs = await get_container_logs(e.name, tail=tail)
            parts.append(f"=== {e.name} ===\n{logs}")
        except Exception as exc:
            parts.append(f"=== {e.name} === (error: {exc})")
    return "\n".join(parts)


async def rebuild_container(name: str) -> tuple[str, str]:
    """
    Full rebuild of a single container's compose project via labels.
    """
    families = await asyncio.to_thread(list_families)
    entry: Entry | None = None
    for members in families.values():
        for e in members:
            if e.name == name:
                entry = e
                break
        if entry:
            break

    if entry is None or not entry.working_dir:
        raise ValueError(f"{name} has no compose working_dir — cannot rebuild.")

    compose_file = entry.compose_file
    working_dir = entry.working_dir

    res = await asyncio.to_thread(
        subprocess.run,
        ["docker", "compose", "-f", compose_file, "down"],
        capture_output=True, text=True, cwd=working_dir,
    )
    if res.returncode != 0:
        raise RuntimeError(f"compose down failed:\n{res.stderr.strip()}")

    build_lines: list[str] = []

    def _build() -> None:
        api = docker.APIClient(base_url="unix://var/run/docker.sock")
        for chunk in api.build(path=working_dir, rm=True, decode=True):
            if "stream" in chunk:
                build_lines.append(chunk["stream"])
            elif "error" in chunk:
                raise RuntimeError(f"Build failed: {chunk['error']}")

    await asyncio.to_thread(_build)

    res = await asyncio.to_thread(
        subprocess.run,
        ["docker", "compose", "-f", compose_file, "up", "-d"],
        capture_output=True, text=True, cwd=working_dir,
    )
    if res.returncode != 0:
        raise RuntimeError(f"compose up failed:\n{res.stderr.strip()}")

    await asyncio.sleep(3)
    try:
        run_logs = await get_container_logs(name, tail=100)
    except Exception:
        run_logs = "(container not yet running)"

    build_tail = "".join(build_lines[-30:])
    return build_tail, run_logs


async def test_alert_logloop(
    container_name: str,
    log_loop_manager,
    threshold: int = 25,
) -> None:
    fake_line = (
        "09:00:00.000 | [WARNING] Server closed the connection: "
        "[Errno 104] Connection reset by peer"
    )
    lines = [fake_line] * threshold
    await asyncio.to_thread(log_loop_manager.inject_test_lines, container_name, lines)


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
