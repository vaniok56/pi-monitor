from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import timez
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from plugins._ctx import PluginMeta

if TYPE_CHECKING:
    from plugins._ctx import PluginContext

logger = logging.getLogger(__name__)

META = PluginMeta(
    name="container_weekly_digest",
    description="Weekly summary of container CPU/RAM usage and restart counts",
    default_config={
        "schedule": "0 8 * * 1",
        "top_n": 5,
    },
)

_CB_MENU = "p.container_weekly_digest:menu"
_CB_PLUGINS = "plugins_menu"


def _parse_pct(s: str) -> float:
    try:
        return float(s.rstrip("%"))
    except (ValueError, AttributeError):
        return 0.0


def _parse_mem(s: str) -> tuple[float, str]:
    """Parse '123MiB / 16GiB' → (used_bytes, label)."""
    try:
        used_s = s.split("/")[0].strip()
        return _to_bytes(used_s), used_s
    except Exception:
        return 0.0, s.split("/")[0].strip() if "/" in s else s


def _to_bytes(s: str) -> float:
    s = s.strip()
    for suffix, mult in [("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024),
                         ("GB", 1e9), ("MB", 1e6), ("KB", 1e3), ("B", 1)]:
        if s.endswith(suffix):
            try:
                return float(s[:-len(suffix)]) * mult
            except ValueError:
                pass
    try:
        return float(s)
    except ValueError:
        return 0.0


async def _docker_stats() -> list[dict]:
    """One-shot docker stats snapshot. Returns list of parsed dicts."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "stats", "--no-stream", "--format", "{{json .}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        logger.warning("container_weekly_digest: docker stats timed out")
        return []
    rows = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


async def _docker_restart_counts() -> dict[str, int]:
    """Return {container_name: restart_count} for all containers."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-a",
            "--format", "{{.Names}}\t{{.Status}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        return {}

    names = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split("\t", 1)
        if parts:
            names.append(parts[0].strip())

    if not names:
        return {}

    # Batch inspect: docker inspect returns a JSON array
    try:
        proc2 = await asyncio.create_subprocess_exec(
            "docker", "inspect", "--format",
            "{{.Name}}\t{{.RestartCount}}",
            *names,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=30)
    except asyncio.TimeoutError:
        return {}

    counts: dict[str, int] = {}
    for line in stdout2.decode("utf-8", errors="replace").splitlines():
        parts = line.strip().split("\t", 1)
        if len(parts) == 2:
            cname = parts[0].lstrip("/")
            try:
                counts[cname] = int(parts[1])
            except ValueError:
                counts[cname] = 0
    return counts


def _build_text(stats_rows: list[dict], restart_counts: dict[str, int], top_n: int) -> str:
    now_s = timez.now().strftime(f"%Y-%m-%d %H:%M {timez.tz_label()}")

    # Top by CPU
    by_cpu = sorted(stats_rows, key=lambda r: _parse_pct(r.get("CPUPerc", "0%")), reverse=True)
    # Top by MEM
    by_mem = sorted(stats_rows, key=lambda r: _to_bytes(r.get("MemUsage", "0B").split("/")[0]), reverse=True)

    cpu_lines = []
    for r in by_cpu[:top_n]:
        name = r.get("Name", r.get("Container", "?"))
        cpu = r.get("CPUPerc", "?")
        mem = r.get("MemUsage", "?")
        cpu_lines.append(f"  <code>{name:<22}</code>  CPU {cpu:>6}  MEM {mem.split('/')[0].strip()}")

    mem_lines = []
    for r in by_mem[:top_n]:
        name = r.get("Name", r.get("Container", "?"))
        mem = r.get("MemUsage", "?")
        pct = r.get("MemPerc", "?")
        mem_lines.append(f"  <code>{name:<22}</code>  {mem.split('/')[0].strip():>8}  ({pct})")

    restart_lines = []
    for name, count in sorted(restart_counts.items(), key=lambda x: x[1], reverse=True):
        if count > 0:
            restart_lines.append(f"  ⚠️ <code>{name}</code>  —  {count}× restarts")

    parts = [f"📊 <b>Container Weekly Digest</b>\n<i>{now_s}</i>"]

    parts.append(f"\n<b>🔥 Top {top_n} by CPU:</b>")
    parts.extend(cpu_lines or ["  (no data)"])

    parts.append(f"\n<b>💾 Top {top_n} by Memory:</b>")
    parts.extend(mem_lines or ["  (no data)"])

    if restart_lines:
        parts.append("\n<b>🔁 Containers with restarts:</b>")
        parts.extend(restart_lines)
    else:
        parts.append("\n✅ No container restarts recorded.")

    running = len([r for r in stats_rows if r])
    total = len(restart_counts)
    parts.append(f"\n<i>{running} running / {total} total containers</i>")

    return "\n".join(parts)


async def _collect_and_send(ctx: "PluginContext", top_n: int) -> str:
    stats_rows, restart_counts = await asyncio.gather(
        _docker_stats(),
        _docker_restart_counts(),
    )
    return _build_text(stats_rows, restart_counts, top_n)


async def _handle_action(query, parts, ctx: "PluginContext") -> None:
    top_n = int(ctx.plugin_cfg.get("top_n", 5))
    sub = parts[1] if len(parts) > 1 else "menu"
    if sub != "menu":
        return

    await query.edit_message_text("⏳ Collecting container stats…")
    text = await _collect_and_send(ctx, top_n)

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data=_CB_MENU)],
            [InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS)],
        ]),
    )


def register(ctx: "PluginContext") -> None:
    schedule = ctx.plugin_cfg.get("schedule", "0 8 * * 1")
    top_n = int(ctx.plugin_cfg.get("top_n", 5))

    ctx.actions.register("p.container_weekly_digest", _handle_action)
    ctx.buttons.add("📊 Fleet Digest", _CB_MENU, sort_key=55)

    async def _scheduled(context) -> None:
        text = await _collect_and_send(ctx, top_n)
        for uid in ctx.cfg.allowed_users:
            try:
                await ctx.app.bot.send_message(uid, text=text, parse_mode=ParseMode.HTML)
            except Exception as exc:
                logger.warning("container_weekly_digest: send to %d failed: %s", uid, exc)

    ctx.scheduler.cron(str(schedule), _scheduled, "container_weekly_digest.send")
    logger.info("container_weekly_digest: top_n=%d schedule=%s", top_n, schedule)
