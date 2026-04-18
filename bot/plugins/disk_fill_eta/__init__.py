from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone

import timez
from pathlib import Path
from typing import TYPE_CHECKING

import psutil
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from plugins._ctx import PluginMeta

if TYPE_CHECKING:
    from plugins._ctx import PluginContext

logger = logging.getLogger(__name__)

META = PluginMeta(
    name="disk_fill_eta",
    description="Predict disk-full date via linear regression on sampled usage",
    default_config={
        "path": "/",
        "threshold_days": 14,
        "schedule": "0 */4 * * *",
        "history_path": "/data/disk_history.json",
    },
)

_CB_MENU = "p.disk_fill_eta:menu"
_CB_SAMPLE = "p.disk_fill_eta:sample"
_CB_PLUGINS = "plugins_menu"

_MAX_SAMPLES = 168  # 7 days × 24 samples/day at 4h interval
_MIN_SAMPLES = 12   # minimum points for meaningful regression


def _load_history(history_path: str) -> list[dict]:
    try:
        with open(history_path) as f:
            return json.load(f).get("samples", [])
    except FileNotFoundError:
        return []
    except Exception as exc:
        logger.warning("disk_fill_eta: history load failed: %s", exc)
        return []


def _save_history(history_path: str, samples: list[dict]) -> None:
    p = Path(history_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump({"samples": samples}, f)
        os.replace(tmp, history_path)
    except Exception as exc:
        logger.error("disk_fill_eta: save failed: %s", exc)


def _linear_eta_days(samples: list[dict]) -> float | None:
    """OLS regression on samples; return days until full, or None."""
    if len(samples) < _MIN_SAMPLES:
        return None
    xs = [datetime.fromisoformat(s["ts"]).timestamp() for s in samples]
    ys = [s["used_bytes"] for s in samples]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    if slope <= 0:
        return None  # disk shrinking or flat — no ETA
    last_used = ys[-1]
    total = samples[-1]["total_bytes"]
    remaining = total - last_used
    eta_seconds = remaining / slope
    return eta_seconds / 86400


def _human_bytes(n: int) -> str:
    value = float(max(n, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def _collect_report(path: str, threshold_days: int, history_path: str, add_sample: bool) -> dict:
    try:
        usage = psutil.disk_usage(path)
    except Exception as exc:
        return {
            "path": path,
            "threshold_days": threshold_days,
            "history_path": history_path,
            "error": str(exc),
        }

    samples = _load_history(history_path)
    if add_sample:
        samples.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "used_bytes": usage.used,
            "total_bytes": usage.total,
        })
        samples = samples[-_MAX_SAMPLES:]
        _save_history(history_path, samples)

    eta = _linear_eta_days(samples)
    return {
        "path": path,
        "threshold_days": threshold_days,
        "history_path": history_path,
        "samples": len(samples),
        "eta_days": eta,
        "usage_pct": usage.percent,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "error": None,
    }


async def _run_sample(context) -> None:
    import asyncio
    path = _run_sample._path
    threshold_days = _run_sample._threshold_days
    history_path = _run_sample._history_path

    try:
        usage = await asyncio.to_thread(psutil.disk_usage, path)
    except Exception as exc:
        logger.error("disk_fill_eta: disk_usage failed: %s", exc)
        return

    samples = _load_history(history_path)
    samples.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "used_bytes": usage.used,
        "total_bytes": usage.total,
    })
    samples = samples[-_MAX_SAMPLES:]
    _save_history(history_path, samples)

    eta = _linear_eta_days(samples)
    if eta is None:
        return
    if eta < threshold_days:
        from alerts import AlertItem, AlertType
        from alerts.notifier import put_alert
        pct = usage.percent
        free_gb = usage.free / 1024 ** 3
        put_alert(AlertItem(
            type=AlertType.HOST_RESOURCE,
            title=f"📉 Disk filling fast: {path}",
            body=(
                f"ETA full: <b>{eta:.1f} days</b>\n"
                f"Current: {pct:.0f}% used, {free_gb:.1f} GB free\n"
                f"Threshold: {threshold_days} days"
            ),
            key=f"disk_fill_eta:{path}",
        ))
        logger.warning("disk_fill_eta: %s ETA %.1f days (threshold %d)", path, eta, threshold_days)


async def _handle_action(query, parts, ctx: "PluginContext") -> None:
    import asyncio

    path = ctx.plugin_cfg.get("path", "/")
    threshold_days = int(ctx.plugin_cfg.get("threshold_days", 14))
    history_path = ctx.plugin_cfg.get("history_path", "/data/disk_history.json")
    schedule = ctx.plugin_cfg.get("schedule")

    sub = parts[1] if len(parts) > 1 else "menu"
    add_sample = sub == "sample"
    if sub not in {"menu", "sample"}:
        return

    if add_sample:
        await query.edit_message_text("⏳ Recording sample and recalculating ETA…")

    report = await asyncio.to_thread(_collect_report, path, threshold_days, history_path, add_sample)
    if report["error"]:
        await query.edit_message_text(
            "📉 <b>Disk Fill ETA</b>\n\n"
            f"❌ Failed to read path <code>{path}</code>\n"
            f"<code>{report['error']}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS),
            ]]),
        )
        return

    auto_line = (
        f"🤖 Auto sample: <b>enabled</b> (<code>{schedule}</code> {timez.tz_label()})"
        if schedule
        else "🤖 Auto sample: <b>disabled</b> (manual-only mode)"
    )

    eta = report["eta_days"]
    if eta is None:
        if report["samples"] < _MIN_SAMPLES:
            eta_line = f"ℹ️ ETA not available yet — samples: {report['samples']}/{_MIN_SAMPLES}"
        else:
            eta_line = "ℹ️ ETA not available — usage trend is flat or decreasing"
        risk_line = ""
    else:
        eta_line = f"⏳ Predicted full: <b>{eta:.1f} days</b>"
        if eta < threshold_days:
            risk_line = f"\n⚠️ Below threshold (<b>{threshold_days} days</b>)"
        else:
            risk_line = f"\n✅ Above threshold (<b>{threshold_days} days</b>)"

    text = (
        "📉 <b>Disk Fill ETA</b>\n\n"
        f"Path: <code>{path}</code>\n"
        f"Usage: <b>{report['usage_pct']:.1f}%</b> "
        f"({_human_bytes(report['used_bytes'])} / {_human_bytes(report['total_bytes'])})\n"
        f"Free: <b>{_human_bytes(report['free_bytes'])}</b>\n"
        f"{auto_line}\n"
        f"{eta_line}{risk_line}"
    )

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Sample now", callback_data=_CB_SAMPLE)],
            [
                InlineKeyboardButton("🔄 Refresh", callback_data=_CB_MENU),
                InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS),
            ],
        ]),
    )


def register(ctx: "PluginContext") -> None:
    path = ctx.plugin_cfg.get("path", "/")
    threshold_days = int(ctx.plugin_cfg.get("threshold_days", 14))
    schedule = ctx.plugin_cfg.get("schedule")
    history_path = ctx.plugin_cfg.get("history_path", "/data/disk_history.json")

    _run_sample._path = path
    _run_sample._threshold_days = threshold_days
    _run_sample._history_path = history_path

    ctx.actions.register("p.disk_fill_eta", _handle_action)
    ctx.buttons.add("📉 Disk ETA", _CB_MENU, sort_key=42)

    if schedule:
        ctx.scheduler.cron(str(schedule), _run_sample, "disk_fill_eta.sample")
        logger.info("disk_fill_eta: monitoring %s (alert < %d days)", path, threshold_days)
    else:
        logger.info("disk_fill_eta: no schedule configured — manual-only mode")
