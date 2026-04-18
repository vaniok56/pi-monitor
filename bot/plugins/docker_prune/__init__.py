from __future__ import annotations

import asyncio
import logging
import re

import timez
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from plugins._ctx import PluginMeta

if TYPE_CHECKING:
    from plugins._ctx import PluginContext

logger = logging.getLogger(__name__)

META = PluginMeta(
    name="docker_prune",
    description="Periodic and manual Docker image/builder/volume pruning",
    default_config={"schedule": "0 3 * * 0", "aggressive": False},
)

_CB_REPORT = "p.docker_prune:report"
_CB_RUN = "p.docker_prune:run"
_CB_RUN_CONFIRM = "p.docker_prune:run_confirm"
_CB_AGGRESSIVE = "p.docker_prune:aggressive"
_CB_AGGRESSIVE_CONFIRM = "p.docker_prune:aggressive_confirm"
_CB_PLUGINS = "plugins_menu"


async def _do_prune(aggressive: bool = False) -> str:
    if aggressive:
        cmds = [["docker", "system", "prune", "-a", "--volumes", "-f"]]
    else:
        cmds = [
            ["docker", "image", "prune", "-f"],
            ["docker", "builder", "prune", "-f"],
            ["docker", "volume", "prune", "-f"],
        ]

    parts = []
    for cmd in cmds:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        out = (stdout + stderr).decode("utf-8", errors="replace").strip()
        if out:
            parts.append(out[-800:])

    return "\n\n".join(parts) or "No output."


def _parse_size_to_bytes(raw: str) -> int:
    token = raw.strip().split(" ", 1)[0]
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)([A-Za-z]+)$", token)
    if not m:
        return 0
    value = float(m.group(1))
    unit = m.group(2)
    factors = {
        "B": 1,
        "KB": 10**3,
        "MB": 10**6,
        "GB": 10**9,
        "TB": 10**12,
        "KiB": 2**10,
        "MiB": 2**20,
        "GiB": 2**30,
        "TiB": 2**40,
    }
    return int(value * factors.get(unit, 0))


def _human_bytes(num_bytes: int) -> str:
    value = float(max(num_bytes, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


async def _docker_df_report() -> tuple[dict[str, str], str | None]:
    usage = {
        "images": "n/a",
        "containers": "n/a",
        "volumes": "n/a",
        "build_cache": "n/a",
    }

    proc = await asyncio.create_subprocess_exec(
        "docker", "system", "df",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
    if proc.returncode != 0:
        err = (stderr or stdout).decode("utf-8", errors="replace").strip()
        return usage, err[:300] or "docker system df failed"

    reclaimable_total = 0
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        row = line.strip()
        if not row or row.lower().startswith("type"):
            continue
        cols = re.split(r"\s{2,}", row)
        if len(cols) < 5:
            continue

        kind = cols[0].lower()
        size_col = cols[3]
        reclaim_col = cols[4]

        if kind.startswith("images"):
            usage["images"] = _human_bytes(_parse_size_to_bytes(size_col))
        elif kind.startswith("containers"):
            usage["containers"] = _human_bytes(_parse_size_to_bytes(size_col))
        elif kind.startswith("local volumes"):
            usage["volumes"] = _human_bytes(_parse_size_to_bytes(size_col))
        elif kind.startswith("build cache"):
            usage["build_cache"] = _human_bytes(_parse_size_to_bytes(size_col))

        reclaimable_total += _parse_size_to_bytes(reclaim_col)

    usage["reclaimable"] = _human_bytes(reclaimable_total)
    return usage, None


def _next_cron_run(expr: str) -> str | None:
    try:
        nxt = timez.next_cron(expr)
        return nxt.strftime(f"%Y-%m-%d %H:%M:%S {timez.tz_label()}")
    except Exception:
        return None


def _report_keyboard(aggressive: bool) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton("🧹 Clean standard", callback_data=_CB_RUN)]]
    if aggressive:
        kb.append([InlineKeyboardButton("💣 Clean aggressive", callback_data=_CB_AGGRESSIVE)])
    kb.append([
        InlineKeyboardButton("🔄 Refresh", callback_data=_CB_REPORT),
        InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS),
    ])
    return InlineKeyboardMarkup(kb)


async def _render_report_text(schedule: str | None, result_note: str | None = None) -> str:
    usage, err = await _docker_df_report()
    if schedule:
        schedule_lines = [
            f"🤖 Auto prune: <b>enabled</b> (<code>{schedule}</code> {timez.tz_label()})",
        ]
        next_run = _next_cron_run(schedule)
        if next_run:
            schedule_lines.append(f"⏭ Next run: <b>{next_run}</b>")
        else:
            schedule_lines.append("⏭ Next run: <i>unavailable</i>")
    else:
        schedule_lines = ["🤖 Auto prune: <b>disabled</b> (manual-only mode)"]

    lines = [
        "📊 <b>Docker Report</b>",
        "",
        *schedule_lines,
        "",
        "🐳 <b>Docker Disk Usage</b>",
        "<code>────────────────────────────",
        f"🖼  Images:       {usage['images']}",
        f"📦  Containers:   {usage['containers']}",
        f"💾  Volumes:      {usage['volumes']}",
        f"🏗  Build cache:  {usage['build_cache']}",
        "────────────────────────────",
        f"♻️  Reclaimable:  {usage.get('reclaimable', 'n/a')}",
        "</code>",
        "",
        f"🕒 Updated: <code>{timez.fmt(timez.now(), '%Y-%m-%d %H:%M:%S.%f')} {timez.tz_label()}</code>",
    ]
    if result_note:
        lines.append(f"\n{result_note}")
    if err:
        lines.append(f"\n<i>Usage probe warning: {_escape_html(err)}</i>")
    return "\n".join(lines)


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


async def _handle_action(query, parts, ctx: "PluginContext") -> None:
    sub = parts[1] if len(parts) > 1 else "report"
    aggressive = bool(ctx.plugin_cfg.get("aggressive", False))
    schedule = ctx.plugin_cfg.get("schedule")

    if sub == "report":
        await query.edit_message_text(
            await _render_report_text(str(schedule) if schedule else None),
            parse_mode=ParseMode.HTML,
            reply_markup=_report_keyboard(aggressive),
        )

    elif sub == "run":
        await query.edit_message_text(
            "⚠️ <b>Run standard Docker prune now?</b>\n\n"
            "This will remove unused images/build cache/volumes.\n\n"
            "Are you sure?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, run prune", callback_data=_CB_RUN_CONFIRM),
                InlineKeyboardButton("❌ Cancel", callback_data=_CB_PLUGINS),
            ]]),
        )

    elif sub == "run_confirm":
        await query.edit_message_text("⏳ Pruning Docker images/builders/volumes…")
        try:
            result = await _do_prune(aggressive=False)
        except Exception as exc:
            result = f"Error: {exc}"
        await query.edit_message_text(
            await _render_report_text(
                str(schedule) if schedule else None,
                result_note=f"✅ <b>Last action:</b> standard prune\n<code>{_escape_html(result[-1200:])}</code>",
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=_report_keyboard(aggressive),
        )

    elif sub == "aggressive":
        await query.edit_message_text(
            "⚠️ <b>Aggressive prune</b>\n\n"
            "Runs <code>docker system prune -a --volumes -f</code>.\n"
            "Removes ALL unused images (not just dangling) and ALL unused volumes.\n\n"
            "Are you sure?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, prune everything", callback_data=_CB_AGGRESSIVE_CONFIRM),
                InlineKeyboardButton("❌ Cancel", callback_data=_CB_PLUGINS),
            ]]),
        )

    elif sub == "aggressive_confirm":
        await query.edit_message_text("⏳ Running aggressive prune…")
        try:
            result = await _do_prune(aggressive=True)
        except Exception as exc:
            result = f"Error: {exc}"
        await query.edit_message_text(
            await _render_report_text(
                str(schedule) if schedule else None,
                result_note=f"💣 <b>Last action:</b> aggressive prune\n<code>{_escape_html(result[-1200:])}</code>",
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=_report_keyboard(aggressive),
        )


async def _scheduled_prune(context) -> None:
    logger.info("Scheduled Docker prune starting")
    try:
        result = await _do_prune(aggressive=False)
        logger.info("Scheduled Docker prune done: %s", result[:200])
    except Exception as exc:
        logger.error("Scheduled Docker prune failed: %s", exc)


def register(ctx: "PluginContext") -> None:
    schedule = ctx.plugin_cfg.get("schedule")

    ctx.actions.register("p.docker_prune", _handle_action)
    ctx.buttons.add("🧹 Prune Docker", _CB_REPORT, sort_key=10)

    if schedule:
        ctx.scheduler.cron(str(schedule), _scheduled_prune, "docker_prune.scheduled")
    else:
        logger.info("docker_prune: no schedule configured — manual-only mode")
