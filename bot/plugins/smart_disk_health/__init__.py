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
    name="smart_disk_health",
    description="Daily S.M.A.R.T. health check for configured block devices",
    default_config={
        "devices": ["/dev/sda"],
        "schedule": "0 6 * * *",
        "allow_on_pi": False,
    },
)

_CB_MENU = "p.smart_disk_health:menu"
_CB_PLUGINS = "plugins_menu"


async def _run_smartctl(device: str) -> tuple[int, str]:
    """Run smartctl in a privileged alpine container with /dev mounted."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "run", "--rm", "--privileged", "--network=none",
        "-v", "/dev:/dev",
        "alpine:latest",
        "sh", "-c", f"apk add --quiet smartmontools 2>/dev/null && smartctl -H -A {device}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    return proc.returncode, (stdout + stderr).decode("utf-8", errors="replace")


def _parse_smartctl(output: str) -> dict:
    result = {
        "passed": True,
        "reallocated": 0,
        "pending": 0,
        "temperature": None,
    }
    for line in output.splitlines():
        if "SMART overall-health" in line:
            result["passed"] = "PASSED" in line
        m = re.search(r"^\s*5\s+Reallocated_Sector_Ct.*?\s+(\d+)\s*$", line)
        if m:
            result["reallocated"] = int(m.group(1))
        m = re.search(r"^\s*197\s+Current_Pending_Sector.*?\s+(\d+)\s*$", line)
        if m:
            result["pending"] = int(m.group(1))
        m = re.search(r"^\s*190\s+Airflow_Temperature_Cel.*?\s+(\d+)\s*$", line)
        if m:
            result["temperature"] = int(m.group(1))
        m = re.search(r"^\s*194\s+Temperature_Celsius.*?\s+(\d+)\s*$", line)
        if m:
            result["temperature"] = int(m.group(1))
    return result


async def _check_device(device: str) -> None:
    status = await _collect_device_status(device)
    if status["error"]:
        logger.error("smart_disk_health: failed for %s: %s", device, status["error"])
        return

    if status["healthy"]:
        logger.info("smart_disk_health: %s OK (temp=%s°C)", device, status["temperature"])
        return

    from alerts import AlertItem, AlertType
    from alerts.notifier import put_alert
    temp = status["temperature"]
    temp_str = f" | Temp: {temp}°C" if temp is not None else ""
    issues = status["issues"]
    put_alert(AlertItem(
        type=AlertType.HOST_RESOURCE,
        title=f"💾 SMART warning: {device}",
        body="\n".join(issues) + temp_str,
        key=f"smart:{device}",
    ))


async def _collect_device_status(device: str) -> dict:
    status = {
        "device": device,
        "healthy": False,
        "temperature": None,
        "issues": [],
        "error": None,
    }
    try:
        rc, output = await _run_smartctl(device)
    except asyncio.TimeoutError:
        status["error"] = "smartctl timed out"
        return status
    except Exception as exc:
        status["error"] = str(exc)
        return status

    parsed = _parse_smartctl(output)
    issues: list[str] = []
    if not parsed["passed"]:
        issues.append("🔴 Health: FAILED")
    if parsed["reallocated"] > 0:
        issues.append(f"⚠️ Reallocated sectors: {parsed['reallocated']}")
    if parsed["pending"] > 0:
        issues.append(f"⚠️ Pending sectors: {parsed['pending']}")

    if rc != 0 and not issues:
        issues.append(f"⚠️ smartctl exit code: {rc}")

    status["temperature"] = parsed["temperature"]
    status["issues"] = issues
    status["healthy"] = not issues
    return status


def _render_status_line(status: dict) -> str:
    device = status["device"]
    if status["error"]:
        return f"❌ <code>{device}</code> — {status['error']}"

    if status["healthy"]:
        temp = status["temperature"]
        temp_text = f" (temp {temp}°C)" if temp is not None else ""
        return f"✅ <code>{device}</code> — healthy{temp_text}"

    temp = status["temperature"]
    temp_text = f" | temp {temp}°C" if temp is not None else ""
    issue_text = "; ".join(status["issues"])
    return f"⚠️ <code>{device}</code> — {issue_text}{temp_text}"

async def _scheduled_check(context) -> None:
    devices = _scheduled_check._devices
    for device in devices:
        await _check_device(device)


async def _handle_action(query, parts, ctx: "PluginContext") -> None:
    sub = parts[1] if len(parts) > 1 else "menu"
    if sub != "menu":
        return

    devices = ctx.plugin_cfg.get("devices", ["/dev/sda"])
    if not devices:
        await query.edit_message_text(
            "💾 <b>SMART Report</b>\n\nNo devices configured.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS),
            ]]),
        )
        return

    await query.edit_message_text("⏳ Running S.M.A.R.T. checks…")
    statuses = await asyncio.gather(*(_collect_device_status(d) for d in devices))

    schedule = ctx.plugin_cfg.get("schedule")
    auto_line = (
        f"🤖 Auto check: <b>enabled</b> (<code>{schedule}</code> {timez.tz_label()})"
        if schedule
        else "🤖 Auto check: <b>disabled</b> (manual-only mode)"
    )

    lines = ["💾 <b>SMART Report</b>", "", auto_line, ""]
    lines.extend(_render_status_line(s) for s in statuses)

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Recheck", callback_data=_CB_MENU),
            InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS),
        ]]),
    )


def register(ctx: "PluginContext") -> None:
    if not ctx.host_capabilities.get("smartctl") and ctx.host_class != "rpi":
        logger.warning("smart_disk_health: smartctl not found — skipping")
        return

    if ctx.host_class == "rpi" and not ctx.plugin_cfg.get("allow_on_pi", False):
        logger.info("smart_disk_health: auto-disabled on Pi (set allow_on_pi: true to override)")
        return

    devices = ctx.plugin_cfg.get("devices", ["/dev/sda"])
    schedule = ctx.plugin_cfg.get("schedule")

    if not devices:
        logger.warning("smart_disk_health: no devices configured — skipping")
        return

    ctx.actions.register("p.smart_disk_health", _handle_action)
    ctx.buttons.add("💾 SMART health", _CB_MENU, sort_key=41)

    _scheduled_check._devices = devices
    if schedule:
        ctx.scheduler.cron(str(schedule), _scheduled_check, "smart_disk_health.scheduled")
        logger.info("smart_disk_health: monitoring %s on schedule %s", devices, schedule)
    else:
        logger.info("smart_disk_health: no schedule configured — manual-only mode")
