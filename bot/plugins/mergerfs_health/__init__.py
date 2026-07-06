from __future__ import annotations

import asyncio
import logging
import shlex
from typing import TYPE_CHECKING

import timez
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from plugins._ctx import PluginMeta

if TYPE_CHECKING:
    from plugins._ctx import PluginContext

logger = logging.getLogger(__name__)

META = PluginMeta(
    name="mergerfs_health",
    description="Per-mount disk usage on host; alert when any mount exceeds threshold",
    default_config={
        "mounts": ["/"],
        "alert_pct": 85.0,
        "schedule": "0 */6 * * *",
    },
)

_CB_MENU = "p.mergerfs_health:menu"
_CB_PLUGINS = "plugins_menu"

_HELPER_IMAGE = "debian:12-slim"


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _parse_df(output: str) -> list[dict]:
    """Parse POSIX `df -B1 -P` output. One row per mount point."""
    rows = []
    for line in output.strip().splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            rows.append({
                "fs": parts[0],
                "total": int(parts[1]),
                "used": int(parts[2]),
                "avail": int(parts[3]),
                "pct": float(parts[4].rstrip("%")),
                "mount": parts[5],
                "error": None,
            })
        except (ValueError, IndexError):
            continue
    return rows


async def _run_df(mounts: list[str]) -> tuple[int, str]:
    paths = " ".join(shlex.quote(m) for m in mounts)
    script = f"df -B1 -P {paths}"
    wrapped = (
        "if ! command -v nsenter >/dev/null 2>&1; then "
        "echo '__NSENTER_MISSING__' >&2; exit 127; fi; "
        f"nsenter -t 1 -m -u -i -n -p -- sh -lc {shlex.quote(script)}"
    )
    proc = await asyncio.create_subprocess_exec(
        "docker", "run", "--rm", "--pid=host", "--privileged", "--network=none",
        _HELPER_IMAGE, "sh", "-lc", wrapped,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    return proc.returncode, (stdout + stderr).decode("utf-8", errors="replace")


async def _get_report(mounts: list[str]) -> list[dict]:
    try:
        rc, output = await _run_df(mounts)
    except asyncio.TimeoutError:
        return [{"mount": m, "error": "timeout", "pct": 0.0} for m in mounts]
    if rc != 0 or "__NSENTER_MISSING__" in output:
        err = output.strip()[:120]
        return [{"mount": m, "error": err, "pct": 0.0} for m in mounts]
    rows = _parse_df(output)
    if not rows:
        return [{"mount": m, "error": "df returned no rows", "pct": 0.0} for m in mounts]
    return rows


def _bar(pct: float) -> str:
    filled = int(pct / 10)
    return "█" * filled + "░" * (10 - filled)


async def _handle_action(query, parts, ctx: "PluginContext") -> None:
    mounts = ctx.plugin_cfg.get("mounts", ["/"])
    alert_pct = float(ctx.plugin_cfg.get("alert_pct", 85))
    sub = parts[1] if len(parts) > 1 else "menu"
    if sub != "menu":
        return

    await query.edit_message_text("⏳ Checking disk usage…")
    results = await _get_report(mounts)

    lines = []
    for r in results:
        if r.get("error"):
            lines.append(f"❌ <code>{r['mount']}</code>  —  {r['error']}")
            continue
        pct = r["pct"]
        icon = "🔴" if pct >= alert_pct else ("⚠️" if pct >= alert_pct * 0.9 else "✅")
        lines.append(
            f"{icon} <b>{r['mount']}</b>\n"
            f"   <code>{_bar(pct)}</code>  {pct:.1f}%\n"
            f"   {_human(r['used'])} / {_human(r['total'])}  ·  {_human(r['avail'])} free"
        )

    await query.edit_message_text(
        "🗂 <b>Disk Health</b>\n\n" + "\n\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data=_CB_MENU)],
            [InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS)],
        ]),
    )


def register(ctx: "PluginContext") -> None:
    mounts = ctx.plugin_cfg.get("mounts", ["/"])
    alert_pct = float(ctx.plugin_cfg.get("alert_pct", 85))
    schedule = ctx.plugin_cfg.get("schedule")

    ctx.actions.register("p.mergerfs_health", _handle_action)
    ctx.buttons.add("🗂 Disk Health", _CB_MENU, sort_key=43)

    if schedule:
        async def _scheduled(context) -> None:
            results = await _get_report(mounts)
            from alerts import AlertItem, AlertType
            from alerts.notifier import put_alert
            for r in results:
                if r.get("error"):
                    continue
                if r["pct"] >= alert_pct:
                    put_alert(AlertItem(
                        type=AlertType.HOST_RESOURCE,
                        title=f"🗂 Disk full: {r['mount']} at {r['pct']:.0f}%",
                        body=(
                            f"Mount: <code>{r['mount']}</code>\n"
                            f"Usage: <b>{r['pct']:.1f}%</b>\n"
                            f"{_human(r['used'])} / {_human(r['total'])}  ·  {_human(r['avail'])} free"
                        ),
                        key=f"mergerfs_health:{r['mount']}",
                    ))

        ctx.scheduler.cron(str(schedule), _scheduled, "mergerfs_health.check")
        logger.info("mergerfs_health: %s alert>%.0f%% schedule=%s", mounts, alert_pct, schedule)
