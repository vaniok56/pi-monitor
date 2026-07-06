from __future__ import annotations

import asyncio
import logging
import socket
import ssl
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
    name="cert_expiry_watcher",
    description="Alert before TLS certificates expire on configured domains",
    default_config={
        "domains": [],
        "warn_days": 14,
        "schedule": "0 9 * * *",
    },
)

_CB_MENU = "p.cert_expiry_watcher:menu"
_CB_PLUGINS = "plugins_menu"


def _check_cert(domain_str: str) -> dict:
    """Parse optional port from 'domain:port', connect, return expiry info."""
    if ":" in domain_str:
        host, port_s = domain_str.rsplit(":", 1)
        try:
            port = int(port_s)
        except ValueError:
            host, port = domain_str, 443
    else:
        host, port = domain_str, 443

    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
        not_after = cert.get("notAfter", "")
        expiry = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days_left = (expiry - datetime.now(timezone.utc)).days
        return {"domain": domain_str, "expiry": expiry, "days_left": days_left, "error": None}
    except ssl.SSLCertVerificationError as exc:
        return {"domain": domain_str, "expiry": None, "days_left": -999, "error": f"SSL: {exc}"}
    except (OSError, socket.timeout) as exc:
        return {"domain": domain_str, "expiry": None, "days_left": -999, "error": f"Connect: {exc}"}
    except Exception as exc:
        return {"domain": domain_str, "expiry": None, "days_left": -999, "error": str(exc)[:120]}


async def _collect_all(domains: list[str]) -> list[dict]:
    results = await asyncio.gather(
        *[asyncio.to_thread(_check_cert, d) for d in domains],
        return_exceptions=True,
    )
    out = []
    for d, r in zip(domains, results):
        if isinstance(r, Exception):
            out.append({"domain": d, "expiry": None, "days_left": -999, "error": str(r)})
        else:
            out.append(r)
    return out


def _row(r: dict, warn_days: int) -> str:
    d = r["domain"]
    if r["error"]:
        return f"❌ <code>{d}</code>  —  {r['error'][:80]}"
    days = r["days_left"]
    expiry_s = r["expiry"].strftime("%Y-%m-%d") if r["expiry"] else "?"
    icon = "🔴" if days < 0 else ("⚠️" if days <= warn_days else "✅")
    return f"{icon} <code>{d}</code>  —  <b>{days}d</b>  ({expiry_s})"


async def _handle_action(query, parts, ctx: "PluginContext") -> None:
    domains = ctx.plugin_cfg.get("domains", [])
    warn_days = int(ctx.plugin_cfg.get("warn_days", 14))
    sub = parts[1] if len(parts) > 1 else "menu"
    if sub != "menu":
        return

    if not domains:
        await query.edit_message_text(
            "🔒 <b>Cert Expiry</b>\n\n❌ No domains configured.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS),
            ]]),
        )
        return

    await query.edit_message_text("⏳ Checking certificates…")
    results = await _collect_all(domains)
    lines = [_row(r, warn_days) for r in results]
    text = (
        "🔒 <b>Certificate Expiry</b>\n\n"
        + "\n".join(lines)
        + f"\n\n<i>Warn threshold: {warn_days} days</i>"
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
    domains = ctx.plugin_cfg.get("domains", [])
    warn_days = int(ctx.plugin_cfg.get("warn_days", 14))
    schedule = ctx.plugin_cfg.get("schedule")

    if not domains:
        logger.warning("cert_expiry_watcher: no domains configured — skipping")
        return

    ctx.actions.register("p.cert_expiry_watcher", _handle_action)
    ctx.buttons.add("🔒 Cert Expiry", _CB_MENU, sort_key=47)

    if schedule:
        async def _scheduled(context) -> None:
            results = await _collect_all(domains)
            from alerts import AlertItem, AlertType
            from alerts.notifier import put_alert
            for r in results:
                if r["error"]:
                    put_alert(AlertItem(
                        type=AlertType.HOST_RESOURCE,
                        title=f"🔒 Cert check failed: {r['domain']}",
                        body=r["error"][:200],
                        key=f"cert_expiry:{r['domain']}:err",
                    ))
                elif r["days_left"] <= warn_days:
                    put_alert(AlertItem(
                        type=AlertType.HOST_RESOURCE,
                        title=f"🔒 Cert expiring: {r['domain']} in {r['days_left']}d",
                        body=(
                            f"Domain: <code>{r['domain']}</code>\n"
                            f"Expires in: <b>{r['days_left']} days</b>\n"
                            f"Date: {r['expiry'].strftime('%Y-%m-%d') if r['expiry'] else '?'}"
                        ),
                        key=f"cert_expiry:{r['domain']}",
                    ))

        ctx.scheduler.cron(str(schedule), _scheduled, "cert_expiry_watcher.check")
        logger.info("cert_expiry_watcher: %d domains, warn<%dd, schedule=%s", len(domains), warn_days, schedule)
