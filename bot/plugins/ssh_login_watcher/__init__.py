from __future__ import annotations

import asyncio
import logging
import re
import shlex
from collections import defaultdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import timez
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from plugins._ctx import PluginMeta

if TYPE_CHECKING:
    from plugins._ctx import PluginContext

logger = logging.getLogger(__name__)

META = PluginMeta(
    name="ssh_login_watcher",
    description="Alert on SSH brute-force from auth.log; on-demand recent-failure view",
    default_config={
        "auth_log": "/var/log/auth.log",
        "threshold": 10,
        "window_minutes": 5,
        "interval_seconds": 120,
    },
)

_CB_MENU = "p.ssh_login_watcher:menu"
_CB_PLUGINS = "plugins_menu"

_HELPER_IMAGE = "debian:12-slim"

# Matches: "Jun  9 12:34:56 ... Failed password for [invalid user] <user> from <ip>"
_FAILED_RE = re.compile(
    r"(\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}).*?"
    r"Failed (?:password|publickey|none) for (?:invalid user )?(\S+) from ([\d.a-fA-F:]+)"
)
# Matches: "Jun  9 12:34:56 ... Invalid user <user> from <ip>"
_INVALID_RE = re.compile(
    r"(\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}).*?"
    r"Invalid user (\S+) from ([\d.a-fA-F:]+)"
)


def _parse_log(content: str, window_minutes: int) -> dict:
    """Count recent failures per IP within window_minutes from now."""
    now = datetime.now()
    year = now.year
    window = timedelta(minutes=window_minutes)
    ip_counts: dict[str, int] = defaultdict(int)
    ip_users: dict[str, set] = defaultdict(set)

    def _process(ts_str: str, user: str, ip: str) -> None:
        try:
            ts = datetime.strptime(f"{year} {ts_str}", "%Y %b %d %H:%M:%S")
            # Handle year-wrap (Dec 31 → Jan 1)
            if ts > now + timedelta(days=1):
                ts = ts.replace(year=year - 1)
            if now - ts <= window:
                ip_counts[ip] += 1
                ip_users[ip].add(user)
        except ValueError:
            pass

    for m in _FAILED_RE.finditer(content):
        _process(m.group(1), m.group(2), m.group(3))
    for m in _INVALID_RE.finditer(content):
        _process(m.group(1), m.group(2), m.group(3))

    return {
        "ip_counts": dict(ip_counts),
        "ip_users": {k: sorted(v) for k, v in ip_users.items()},
        "total": sum(ip_counts.values()),
    }


async def _read_log(log_path: str) -> tuple[int, str]:
    script = f"tail -n 3000 {shlex.quote(log_path)}"
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
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    out = (stdout + stderr).decode("utf-8", errors="replace")
    return proc.returncode, out


async def _handle_action(query, parts, ctx: "PluginContext") -> None:
    log_path = ctx.plugin_cfg.get("auth_log", "/var/log/auth.log")
    threshold = int(ctx.plugin_cfg.get("threshold", 10))
    window_minutes = int(ctx.plugin_cfg.get("window_minutes", 5))
    sub = parts[1] if len(parts) > 1 else "menu"
    if sub != "menu":
        return

    await query.edit_message_text("⏳ Scanning auth log…")
    try:
        rc, content = await _read_log(log_path)
    except asyncio.TimeoutError:
        rc, content = -1, "timeout reading log"

    if rc != 0 or "__NSENTER_MISSING__" in content:
        await query.edit_message_text(
            f"🔐 <b>SSH Login Watcher</b>\n\n❌ Cannot read log:\n<code>{content[:200]}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS),
            ]]),
        )
        return

    result = _parse_log(content, window_minutes)
    ip_counts = result["ip_counts"]

    if not ip_counts:
        text = (
            f"🔐 <b>SSH Login Watcher</b>\n\n"
            f"✅ No failed attempts in last {window_minutes} min.\n"
            f"<i>Log: <code>{log_path}</code></i>"
        )
    else:
        sorted_ips = sorted(ip_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        lines = []
        for ip, count in sorted_ips:
            users = ", ".join(result["ip_users"].get(ip, []))[:50] or "?"
            icon = "🔴" if count >= threshold else "⚠️"
            lines.append(f"{icon} <code>{ip}</code>  —  <b>{count}×</b>  ({users})")
        text = (
            f"🔐 <b>SSH Login Watcher</b>\n\n"
            f"Last {window_minutes} min — "
            f"<b>{result['total']}</b> failed attempts from <b>{len(ip_counts)}</b> IPs:\n\n"
            + "\n".join(lines)
            + f"\n\n<i>Alert threshold: {threshold} attempts / {window_minutes} min</i>"
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
    log_path = ctx.plugin_cfg.get("auth_log", "/var/log/auth.log")
    threshold = int(ctx.plugin_cfg.get("threshold", 10))
    window_minutes = int(ctx.plugin_cfg.get("window_minutes", 5))
    interval_seconds = int(ctx.plugin_cfg.get("interval_seconds", 120))

    ctx.actions.register("p.ssh_login_watcher", _handle_action)
    ctx.buttons.add("🔐 SSH Watcher", _CB_MENU, sort_key=50)

    async def _poll(context) -> None:
        try:
            rc, content = await _read_log(log_path)
        except Exception:
            return
        if rc != 0 or "__NSENTER_MISSING__" in content:
            return
        result = _parse_log(content, window_minutes)
        if not result["ip_counts"]:
            return
        top_count = max(result["ip_counts"].values())
        if top_count >= threshold:
            top_ip = max(result["ip_counts"], key=result["ip_counts"].get)
            from alerts import AlertItem, AlertType
            from alerts.notifier import put_alert
            put_alert(AlertItem(
                type=AlertType.HOST_RESOURCE,
                title=f"🔐 SSH brute-force: {top_ip} — {top_count}×",
                body=(
                    f"Top source: <code>{top_ip}</code>  —  <b>{top_count} attempts</b>\n"
                    f"Total: {result['total']} from {len(result['ip_counts'])} IPs\n"
                    f"Window: last {window_minutes} min"
                ),
                key=f"ssh_login_watcher:{top_ip}",
            ))

    ctx.scheduler.every(interval_seconds, _poll, "ssh_login_watcher.poll")
    logger.info(
        "ssh_login_watcher: %s every %ds — alert >=%d in %d min",
        log_path, interval_seconds, threshold, window_minutes,
    )
