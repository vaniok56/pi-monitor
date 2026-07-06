from __future__ import annotations

import asyncio
import json
import logging
import os
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
    name="restic_backup_watcher",
    description="Alert if restic backup is stale; show recent snapshots on demand",
    default_config={
        "repo": "/mnt/backup-ssd/restic-repo",
        "password_env": "RESTIC_PASSWORD",
        "max_age_hours": 26,
        "schedule": "0 8 * * *",
    },
)

_CB_MENU = "p.restic_backup_watcher:menu"
_CB_PLUGINS = "plugins_menu"


def _get_password(cfg: dict) -> str | None:
    if "password" in cfg:
        return str(cfg["password"])
    env_key = cfg.get("password_env", "RESTIC_PASSWORD")
    val = os.environ.get(env_key)
    if val:
        return val
    pw_file = cfg.get("password_file")
    if pw_file:
        try:
            return open(pw_file).read().strip()
        except OSError:
            pass
    return None


async def _run_restic(repo: str, password: str, args: list[str]) -> tuple[int, str]:
    env = {**os.environ, "RESTIC_REPOSITORY": repo, "RESTIC_PASSWORD": password}
    try:
        proc = await asyncio.create_subprocess_exec(
            "restic", *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
        combined = (stdout + stderr).decode("utf-8", errors="replace")
        return proc.returncode, combined
    except FileNotFoundError:
        return 127, "restic binary not found — add it to Dockerfile or PATH"
    except asyncio.TimeoutError:
        return -1, "restic timed out after 90s"


def _parse_snapshots(rc: int, output: str) -> tuple[list[dict], str | None]:
    if rc == 127:
        return [], output.strip()
    if rc != 0:
        # restic exits non-zero for repo errors; strip JSON prefix if present
        return [], output.strip()[:300]
    try:
        data = json.loads(output.strip())
        if isinstance(data, list):
            return data, None
    except Exception:
        pass
    return [], f"unexpected output: {output[:100]}"


def _snapshot_age_hours(snap: dict) -> float:
    ts_str = snap.get("time", "")
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except Exception:
        return float("inf")


def _fmt_snapshot(snap: dict, first: bool) -> str:
    ts_str = snap.get("time", "?")
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        local_ts = timez.fmt(ts, "%Y-%m-%d %H:%M")
    except Exception:
        local_ts = ts_str[:16]
    tags = ", ".join(snap.get("tags") or []) or "—"
    short_id = snap.get("short_id") or (snap.get("id") or "?")[:8]
    prefix = "🟢" if first else "  "
    return f"{prefix} <code>{short_id}</code>  {local_ts} {timez.tz_label()}  ·  {tags}"


async def _check_and_alert(repo: str, password: str, max_age_hours: int) -> None:
    rc, output = await _run_restic(repo, password, ["snapshots", "--json", "--last", "5"])
    snaps, err = _parse_snapshots(rc, output)
    from alerts import AlertItem, AlertType
    from alerts.notifier import put_alert
    if err:
        put_alert(AlertItem(
            type=AlertType.HOST_RESOURCE,
            title="💾 Restic: cannot read snapshots",
            body=f"Repo: <code>{repo}</code>\n<code>{err}</code>",
            key="restic_backup_watcher:error",
        ))
        return
    if not snaps:
        put_alert(AlertItem(
            type=AlertType.HOST_RESOURCE,
            title="💾 Restic: no snapshots found",
            body=f"Repo: <code>{repo}</code>",
            key="restic_backup_watcher:no_snaps",
        ))
        return
    age_h = _snapshot_age_hours(snaps[0])
    if age_h > max_age_hours:
        put_alert(AlertItem(
            type=AlertType.HOST_RESOURCE,
            title=f"💾 Restic backup stale — {age_h:.1f}h old",
            body=(
                f"Last snapshot: <b>{age_h:.1f}h ago</b>\n"
                f"Threshold: {max_age_hours}h\n"
                f"Repo: <code>{repo}</code>"
            ),
            key="restic_backup_watcher:stale",
        ))


async def _handle_action(query, parts, ctx: "PluginContext") -> None:
    repo = ctx.plugin_cfg.get("repo", "/mnt/backup-ssd/restic-repo")
    password = _get_password(ctx.plugin_cfg)
    max_age_hours = int(ctx.plugin_cfg.get("max_age_hours", 26))
    sub = parts[1] if len(parts) > 1 else "menu"
    if sub != "menu":
        return

    if not password:
        await query.edit_message_text(
            "💾 <b>Restic Backup</b>\n\n"
            "❌ No password configured.\n"
            "Set <code>RESTIC_PASSWORD</code> env var or <code>password_env</code> / "
            "<code>password_file</code> in plugin config.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS),
            ]]),
        )
        return

    await query.edit_message_text("⏳ Fetching snapshots…")
    rc, output = await _run_restic(repo, password, ["snapshots", "--json", "--last", "5"])
    snaps, err = _parse_snapshots(rc, output)

    if err:
        await query.edit_message_text(
            f"💾 <b>Restic Backup</b>\n\n❌ Error:\n<code>{err}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data=_CB_MENU)],
                [InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS)],
            ]),
        )
        return

    if not snaps:
        text = f"💾 <b>Restic Backup</b>\n\n⚠️ No snapshots found.\nRepo: <code>{repo}</code>"
    else:
        age_h = _snapshot_age_hours(snaps[0])
        status = "✅ Fresh" if age_h <= max_age_hours else f"⚠️ Stale — {age_h:.1f}h old"
        snap_lines = "\n".join(_fmt_snapshot(s, i == 0) for i, s in enumerate(snaps))
        text = (
            f"💾 <b>Restic Backup</b>\n\n"
            f"Repo: <code>{repo}</code>\n"
            f"Status: <b>{status}</b>  (threshold: {max_age_hours}h)\n\n"
            f"<b>Last {len(snaps)} snapshots:</b>\n{snap_lines}"
        )

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data=_CB_MENU)],
            [InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS)],
        ]),
    )


def register(ctx: "PluginContext") -> None:
    repo = ctx.plugin_cfg.get("repo", "/mnt/backup-ssd/restic-repo")
    password = _get_password(ctx.plugin_cfg)
    max_age_hours = int(ctx.plugin_cfg.get("max_age_hours", 26))
    schedule = ctx.plugin_cfg.get("schedule")

    if not password:
        logger.warning("restic_backup_watcher: no password configured — on-demand menu only")

    ctx.actions.register("p.restic_backup_watcher", _handle_action)
    ctx.buttons.add("💾 Restic Backup", _CB_MENU, sort_key=45)

    if schedule and password:
        async def _scheduled(context) -> None:
            await _check_and_alert(repo, password, max_age_hours)

        ctx.scheduler.cron(str(schedule), _scheduled, "restic_backup_watcher.check")
        logger.info("restic_backup_watcher: repo=%s alert>%dh schedule=%s", repo, max_age_hours, schedule)
    elif schedule and not password:
        logger.warning("restic_backup_watcher: schedule set but no password — skipping auto-check")
