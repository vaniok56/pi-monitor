"""
All Telegram command and callback handlers.

Inline keyboard callback_data format:
  menu                          → show family tree menu
  family:<name>                 → show family sub-list (or container detail for leaf)
  container:<name>              → show container detail
  qlogs:<name>                  → quick logs (30 lines, always inline)
  logs:<name>:<tail>            → logs N lines (inline or file)
  errors:<name>                 → errors-only filter on last 100 lines
  restart:<name>                → quick restart (SDK)
  rebuild:<name>                → rebuild confirmation
  rebuild_confirm:<name>        → execute rebuild
  stop:<name>                   → stop container
  start:<name>                  → start container (compose up -d <service> or SDK)
  family_restart:<name>         → restart all live members
  family_rebuild:<name>         → rebuild family via compose
  family_logs:<name>            → merged tail from all family members
  family_stop:<name>            → confirm screen before compose down
  family_stop_confirm:<name>    → execute compose down
  family_start:<name>           → compose up -d (whole family / all-ghost)
  forget:<name>                 → remove container from persistent registry
  last_alert                    → jump to last-alerted container detail
  host_status                   → /status refreshed in-place (one-shot)
  ignore_sig:<hash>             → permanently ignore a log-loop signature
"""
from __future__ import annotations

import asyncio
import io
import logging
from typing import Optional

import docker
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import docker_ops
from docker_ops import Entry
import registry as reg
from alerts.host import HostWatchdog, _get_docker_stats
from alerts.logloop import LogLoopManager
from alerts.notifier import Notifier

logger = logging.getLogger(__name__)


# ── Access control ───────────────────────────────────────────────────────────

def _allowed(update: Update, allowed_users: set[int]) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    return uid in allowed_users


# ── Keyboard builders ────────────────────────────────────────────────────────

def _main_menu_keyboard(families: dict) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for family_name, members in families.items():
        emoji = docker_ops.family_status_emoji(members)
        if docker_ops.is_leaf_family(family_name, members):
            label = f"{emoji} {family_name[:20]}"
            cb = f"container:{members[0].name}"
        else:
            running = sum(1 for e in members if e.status == "running")
            total = len(members)
            count_str = str(total) if running == total else f"{running}/{total}"
            label = f"{emoji} {family_name[:16]} ({count_str})"
            cb = f"family:{family_name}"
        row.append(InlineKeyboardButton(label, callback_data=cb))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton("📊 Host status", callback_data="host_status"),
        InlineKeyboardButton("🔔 Last alert", callback_data="last_alert"),
    ])
    return InlineKeyboardMarkup(buttons)


def _family_keyboard(family_name: str, members: list[Entry]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for e in members:
        emoji = docker_ops.container_status_emoji(e.status)
        label = f"{emoji} {e.name[:18]}"
        row.append(InlineKeyboardButton(label, callback_data=f"container:{e.name}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    all_gone = all(e.is_ghost for e in members)
    any_live = any(not e.is_ghost for e in members)

    if all_gone:
        # All ghosts: only offer Start all + Back
        buttons.append([
            InlineKeyboardButton("▶️ Start all", callback_data=f"family_start:{family_name}"),
        ])
    else:
        buttons.append([
            InlineKeyboardButton("🔁 Restart all", callback_data=f"family_restart:{family_name}"),
            InlineKeyboardButton("🔨 Rebuild all", callback_data=f"family_rebuild:{family_name}"),
        ])
        buttons.append([
            InlineKeyboardButton("⏹ Stop all", callback_data=f"family_stop:{family_name}"),
            InlineKeyboardButton("📋 Merged logs", callback_data=f"family_logs:{family_name}"),
        ])

    buttons.append([InlineKeyboardButton("◀️ Back", callback_data="menu")])
    return InlineKeyboardMarkup(buttons)


def _container_keyboard(entry: Entry, family_name: Optional[str] = None) -> InlineKeyboardMarkup:
    name = entry.name
    rows: list[list[InlineKeyboardButton]] = []

    if entry.is_ghost:
        # Ghost: Start and Forget only
        rows.append([
            InlineKeyboardButton("▶️ Start", callback_data=f"start:{name}"),
            InlineKeyboardButton("🗑 Forget", callback_data=f"forget:{name}"),
        ])
    else:
        rows.append([
            InlineKeyboardButton("⚡ Quick logs", callback_data=f"qlogs:{name}"),
            InlineKeyboardButton("📋 Logs 100", callback_data=f"logs:{name}:100"),
        ])
        if entry.status == "running":
            action_row = [InlineKeyboardButton("🔄 Restart", callback_data=f"restart:{name}")]
            if docker_ops.is_rebuildable(entry):
                action_row.append(InlineKeyboardButton("🔨 Rebuild", callback_data=f"rebuild:{name}"))
            rows.append(action_row)
            rows.append([InlineKeyboardButton("⏹ Stop", callback_data=f"stop:{name}")])
        else:
            rows.append([InlineKeyboardButton("▶️ Start", callback_data=f"start:{name}")])
            if docker_ops.is_rebuildable(entry):
                rows.append([InlineKeyboardButton("🔨 Build", callback_data=f"rebuild:{name}")])

    back_target = f"family:{family_name}" if family_name else "menu"
    rows.append([InlineKeyboardButton("◀️ Back", callback_data=back_target)])
    return InlineKeyboardMarkup(rows)


def _logs_keyboard(name: str, tail: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔎 Errors only", callback_data=f"errors:{name}"),
        InlineKeyboardButton("🔄 Refresh", callback_data=f"logs:{name}:{tail}"),
        InlineKeyboardButton("◀️ Back", callback_data=f"container:{name}"),
    ]])


def _status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Refresh", callback_data="host_status"),
        InlineKeyboardButton("◀️ Menu", callback_data="menu"),
    ]])


# ── Command handlers ─────────────────────────────────────────────────────────

async def cmd_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    allowed_users: set[int],
) -> None:
    if not _allowed(update, allowed_users):
        return
    await _send_main_menu(update.message.reply_text)


async def cmd_status(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    allowed_users: set[int],
    watchdog: HostWatchdog,
) -> None:
    if not _allowed(update, allowed_users):
        return
    docker_stats = await _get_docker_stats()
    text = await asyncio.to_thread(watchdog.host_status_text, docker_stats)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_status_keyboard())


async def cmd_testalert(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    allowed_users: set[int],
    notifier: Notifier,
    log_loop_manager: LogLoopManager,
) -> None:
    if not _allowed(update, allowed_users):
        return
    args = context.args or []
    mode = args[0] if args else "crash"

    if mode == "crash":
        from alerts import AlertItem, AlertType
        from alerts.notifier import put_alert
        container_name = args[1] if len(args) > 1 else "test_container"
        put_alert(AlertItem(
            type=AlertType.CRASH,
            title="💥 TEST — crash alert",
            body=f"This is a <b>test crash alert</b> for <code>{container_name}</code>. No action needed.",
            key="test:crash:force_fire",
            container=container_name,
            show_container_buttons=False,
        ))
        notifier._last_fire.pop("test:crash:force_fire", None)
        await update.message.reply_text("✅ Test crash alert sent.")

    elif mode == "host":
        from alerts import AlertItem, AlertType
        from alerts.notifier import put_alert
        notifier._last_fire.pop("test:host:force_fire", None)
        put_alert(AlertItem(
            type=AlertType.HOST_RESOURCE,
            title="🔥 TEST — host resource alert",
            body="RAM: <b>95%</b>  (test, not real)",
            key="test:host:force_fire",
        ))
        await update.message.reply_text("✅ Test host alert sent.")

    elif mode == "logloop":
        # Default to the first running container if none specified
        if len(args) > 1:
            container_name = args[1]
        else:
            import docker as _docker
            try:
                _running = _docker.from_env().containers.list()
                container_name = _running[0].name if _running else "my-container"
            except Exception:
                container_name = "my-container"
        threshold = int(args[2]) if len(args) > 2 else 25
        await docker_ops.test_alert_logloop(container_name, log_loop_manager, threshold)
        await update.message.reply_text(
            f"✅ Injected {threshold} synthetic log lines into <code>{container_name}</code>.\n"
            "An alert should arrive shortly if threshold is met.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            "Usage: /testalert [crash [container]|host|logloop [container] [count]]"
        )


async def cmd_help(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    allowed_users: set[int],
) -> None:
    if not _allowed(update, allowed_users):
        return
    text = (
        "<b>Pi Control Bot</b>\n\n"
        "/start — container family tree\n"
        "/status — host stats (RAM, disk, CPU, temp, top containers)\n"
        "/testalert [crash [container]|host|logloop] — send a test alert\n"
        "/help — this message\n\n"
        "<b>Container actions:</b> tap any entry in /start\n"
        "<b>Ghost entries</b> (⚫) = container was removed via compose down;\n"
        "tap Start to bring it back, Forget to clear it."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ── Callback handler ─────────────────────────────────────────────────────────

async def handle_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    allowed_users: set[int],
    notifier: Notifier,
    watchdog: HostWatchdog,
    log_loop_manager: LogLoopManager,
) -> None:
    query = update.callback_query
    if not query:
        return
    if not _allowed(update, allowed_users):
        await query.answer("Access denied.")
        return

    await query.answer()
    data = query.data or ""
    parts = data.split(":", 2)
    action = parts[0]

    try:
        if action == "menu":
            await _edit_main_menu(query)

        elif action == "family":
            family_name = parts[1]
            await _show_family_view(query, family_name)

        elif action == "container":
            name = parts[1]
            await _show_container_detail(query, name)

        elif action == "qlogs":
            name = parts[1]
            await _show_quick_logs(query, name)

        elif action == "logs":
            name, tail = parts[1], int(parts[2])
            await _show_logs(query, name, tail)

        elif action == "errors":
            name = parts[1]
            await _show_errors_only(query, name)

        elif action == "restart":
            name = parts[1]
            await query.edit_message_text(
                f"⏳ Restarting <code>{name}</code>…", parse_mode=ParseMode.HTML
            )
            result = await docker_ops.restart_container(name)
            await query.edit_message_text(
                result, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Back", callback_data=f"container:{name}"),
                    InlineKeyboardButton("📋 Logs", callback_data=f"logs:{name}:100"),
                ]]),
            )

        elif action == "stop":
            name = parts[1]
            await query.edit_message_text(
                f"⏳ Stopping <code>{name}</code>…", parse_mode=ParseMode.HTML
            )
            result = await docker_ops.stop_container(name)
            await query.edit_message_text(
                result, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Back", callback_data=f"container:{name}"),
                ]]),
            )

        elif action == "start":
            name = parts[1]
            await query.edit_message_text(
                f"⏳ Starting <code>{name}</code>…", parse_mode=ParseMode.HTML
            )
            result = await docker_ops.start_container(name)
            await query.edit_message_text(
                result, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Back", callback_data=f"container:{name}"),
                    InlineKeyboardButton("📋 Logs", callback_data=f"logs:{name}:100"),
                ]]),
            )

        elif action == "rebuild":
            name = parts[1]
            entry = await asyncio.to_thread(_find_entry, name)
            rebuildable = docker_ops.is_rebuildable(entry) if entry else False
            if not rebuildable:
                await query.edit_message_text(
                    f"<code>{name}</code> has no local Dockerfile — cannot rebuild from source.\n"
                    "Use Restart to restart from current image.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀️ Back", callback_data=f"container:{name}"),
                    ]]),
                )
                return
            confirm_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, Rebuild", callback_data=f"rebuild_confirm:{name}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"container:{name}"),
            ]])
            await query.edit_message_text(
                f"<b>Rebuild {name}?</b>\n\n"
                "1. <code>docker compose down</code> (whole project stack)\n"
                "2. <code>docker build</code> from source\n"
                "3. <code>docker compose up -d</code>\n\n"
                "May take a few minutes on the Pi.",
                parse_mode=ParseMode.HTML,
                reply_markup=confirm_kb,
            )

        elif action == "rebuild_confirm":
            name = parts[1]
            await query.edit_message_text(
                f"⏳ Rebuilding <code>{name}</code>…\nStep 1/3: compose down",
                parse_mode=ParseMode.HTML,
            )
            try:
                build_tail, run_logs = await docker_ops.rebuild_container(name)
            except Exception as exc:
                await query.edit_message_text(
                    f"❌ <b>Rebuild failed</b>:\n<code>{_escape(str(exc)[:1500])}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀️ Back", callback_data=f"container:{name}"),
                    ]]),
                )
                return
            body = (
                f"✅ <b>{name} rebuilt</b>\n\n"
                f"<b>Build (last lines):</b>\n<code>{_escape(build_tail[-1500:])}</code>\n\n"
                f"<b>Container logs (last 100):</b>\n<code>{_escape(run_logs[:2000])}</code>"
            )
            back_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Back", callback_data=f"container:{name}"),
            ]])
            if len(body) > 4000:
                doc = io.BytesIO((build_tail + "\n---\n" + run_logs).encode())
                doc.name = f"{name}_rebuild.txt"
                await query.edit_message_text(
                    f"✅ <b>{name}</b> rebuilt. Full output below.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=back_kb,
                )
                await query.message.reply_document(document=doc, caption=f"{name}_rebuild.txt")
            else:
                await query.edit_message_text(body, parse_mode=ParseMode.HTML, reply_markup=back_kb)

        elif action == "family_restart":
            family_name = parts[1]
            await query.edit_message_text(
                f"⏳ Restarting all in <b>{family_name}</b>…", parse_mode=ParseMode.HTML
            )
            result = await docker_ops.restart_family(family_name)
            await query.edit_message_text(
                result, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Back", callback_data=f"family:{family_name}"),
                ]]),
            )

        elif action == "family_rebuild":
            family_name = parts[1]
            await query.edit_message_text(
                f"⏳ Rebuilding <b>{family_name}</b>…", parse_mode=ParseMode.HTML
            )
            try:
                result = await docker_ops.rebuild_family(family_name)
            except Exception as exc:
                result = f"❌ <b>Rebuild failed</b>:\n<code>{_escape(str(exc)[:1500])}</code>"
            await query.edit_message_text(
                result, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Back", callback_data=f"family:{family_name}"),
                ]]),
            )

        elif action == "family_logs":
            family_name = parts[1]
            await query.edit_message_text(
                f"⏳ Fetching merged logs for <b>{family_name}</b>…", parse_mode=ParseMode.HTML
            )
            try:
                logs = await docker_ops.family_merged_logs(family_name)
            except Exception as exc:
                logs = f"Error: {exc}"
            back_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Back", callback_data=f"family:{family_name}"),
            ]])
            full = f"<b>{family_name}</b> — merged logs:\n\n<code>{_escape(logs)}</code>"
            if len(full) > 4000:
                doc = io.BytesIO(logs.encode())
                doc.name = f"{family_name}_merged_logs.txt"
                await query.edit_message_text(
                    f"📎 <b>{family_name}</b> — merged logs sent as file below.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=back_kb,
                )
                await query.message.reply_document(document=doc, caption=f"{family_name}_merged_logs.txt")
            else:
                await query.edit_message_text(full, parse_mode=ParseMode.HTML, reply_markup=back_kb)

        elif action == "family_stop":
            family_name = parts[1]
            confirm_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, Stop all", callback_data=f"family_stop_confirm:{family_name}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"family:{family_name}"),
            ]])
            await query.edit_message_text(
                f"<b>Stop all — {family_name}?</b>\n\n"
                "Runs <code>docker compose down</code>.\n"
                "Containers will be <b>removed</b> (not just stopped).\n"
                "They remain visible in the bot as ⚫ ghosts and can be\n"
                "restarted with <b>Start all</b>.",
                parse_mode=ParseMode.HTML,
                reply_markup=confirm_kb,
            )

        elif action == "family_stop_confirm":
            family_name = parts[1]
            await query.edit_message_text(
                f"⏳ Running compose down for <b>{family_name}</b>…", parse_mode=ParseMode.HTML
            )
            try:
                result = await docker_ops.stop_family(family_name)
            except Exception as exc:
                result = f"❌ <b>Stop failed</b>:\n<code>{_escape(str(exc)[:1500])}</code>"
            await query.edit_message_text(
                result, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Back", callback_data=f"family:{family_name}"),
                ]]),
            )

        elif action == "family_start":
            family_name = parts[1]
            await query.edit_message_text(
                f"⏳ Starting <b>{family_name}</b>…", parse_mode=ParseMode.HTML
            )
            try:
                result = await docker_ops.start_family(family_name)
            except Exception as exc:
                result = f"❌ <b>Start failed</b>:\n<code>{_escape(str(exc)[:1500])}</code>"
            await query.edit_message_text(
                result, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Back", callback_data=f"family:{family_name}"),
                ]]),
            )

        elif action == "forget":
            name = parts[1]
            reg.forget(name)
            # Navigate back to family or menu
            entry = await asyncio.to_thread(_find_entry, name)
            back_cb = f"family:{entry.family}" if entry and entry.family != name else "menu"
            await query.edit_message_text(
                f"🗑 <code>{name}</code> removed from registry.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Back", callback_data=back_cb),
                ]]),
            )

        elif action == "last_alert":
            last = notifier.last_alert
            if last is None:
                await query.edit_message_text(
                    "No alerts yet.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀️ Menu", callback_data="menu"),
                    ]]),
                )
            else:
                container_name, ts = last
                await query.edit_message_text(
                    f"🔔 Last alert: <code>{container_name}</code>  ({ts})\n\nNavigating…",
                    parse_mode=ParseMode.HTML,
                )
                await _show_container_detail(query, container_name)

        elif action == "host_status":
            docker_stats = await _get_docker_stats()
            text = await asyncio.to_thread(watchdog.host_status_text, docker_stats)
            await query.edit_message_text(
                text, parse_mode=ParseMode.HTML,
                reply_markup=_status_keyboard(),
            )

        elif action == "ignore_sig":
            sig_hash = parts[1]
            notifier.ignore_signature(sig_hash)
            log_loop_manager.reload_rules()
            await query.edit_message_text(
                f"🚫 Signature <code>{sig_hash}</code> will be ignored.",
                parse_mode=ParseMode.HTML,
            )

    except Exception as exc:
        logger.exception("Callback error for action=%s", action)
        try:
            await query.edit_message_text(
                f"❌ Error: <code>{_escape(str(exc)[:500])}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Menu", callback_data="menu"),
                ]]),
            )
        except Exception:
            pass


# ── Helpers ──────────────────────────────────────────────────────────────────

def _find_entry(name: str, families: dict | None = None) -> Optional[Entry]:
    """Look up a container Entry by name across all families."""
    if families is None:
        families = docker_ops.list_families()
    for members in families.values():
        for e in members:
            if e.name == name:
                return e
    return None


async def _send_main_menu(send_fn) -> None:
    families = await asyncio.to_thread(docker_ops.list_families)
    running = sum(1 for m in families.values() for e in m if e.status == "running")
    total = sum(len(m) for m in families.values())
    text = f"<b>Containers</b>  ({running}/{total} running)"
    await send_fn(text, parse_mode=ParseMode.HTML, reply_markup=_main_menu_keyboard(families))


async def _edit_main_menu(query) -> None:
    families = await asyncio.to_thread(docker_ops.list_families)
    running = sum(1 for m in families.values() for e in m if e.status == "running")
    total = sum(len(m) for m in families.values())
    text = f"<b>Containers</b>  ({running}/{total} running)"
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_main_menu_keyboard(families))


async def _show_family_view(query, family_name: str) -> None:
    families = await asyncio.to_thread(docker_ops.list_families)
    members = families.get(family_name)
    if not members:
        await query.edit_message_text(
            f"Family <code>{family_name}</code> not found.", parse_mode=ParseMode.HTML
        )
        return
    running = sum(1 for e in members if e.status == "running")
    ghosts = sum(1 for e in members if e.is_ghost)
    status_line = f"{running}/{len(members)} running"
    if ghosts:
        status_line += f", {ghosts} gone"
    text = f"<b>{family_name}</b>  ({status_line})"
    await query.edit_message_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=_family_keyboard(family_name, members),
    )


async def _show_container_detail(query, name: str) -> None:
    families = await asyncio.to_thread(docker_ops.list_families)
    entry = _find_entry(name, families)

    if entry is None:
        # Not in live Docker nor registry
        await query.edit_message_text(
            f"Container <code>{name}</code> not found in Docker or registry.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Menu", callback_data="menu"),
            ]]),
        )
        return

    text = docker_ops.container_detail_text(entry)

    # Determine back target
    family_name = entry.family
    members = families.get(family_name, [])
    back_family = None if docker_ops.is_leaf_family(family_name, members) else family_name

    keyboard = _container_keyboard(entry, back_family)
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def _show_quick_logs(query, name: str) -> None:
    await query.edit_message_text(
        f"⏳ Fetching quick logs for <code>{name}</code>…", parse_mode=ParseMode.HTML
    )
    try:
        logs = await docker_ops.quick_logs(name)
    except Exception as exc:
        logs = f"Error: {exc}"

    back_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Refresh", callback_data=f"qlogs:{name}"),
        InlineKeyboardButton("◀️ Back", callback_data=f"container:{name}"),
    ]])
    header = f"<b>{name}</b> — last 30 lines:\n\n"
    body = f"<code>{_escape(logs)}</code>"
    text = header + body
    if len(text) > 4096:
        text = header + f"<code>{_escape(logs[-(4096 - len(header) - 20):])}</code>"
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb)


async def _show_logs(query, name: str, tail: int) -> None:
    await query.edit_message_text(
        f"⏳ Fetching last {tail} lines of <code>{name}</code>…",
        parse_mode=ParseMode.HTML,
    )
    try:
        logs = await docker_ops.get_container_logs(name, tail=tail)
    except Exception as exc:
        logs = f"Error: {exc}"

    back_kb = _logs_keyboard(name, tail)
    header = f"<b>{name}</b> — last {tail} lines:\n\n"
    body = f"<code>{_escape(logs)}</code>"
    full = header + body

    if len(full) > 4000:
        doc = io.BytesIO(logs.encode())
        doc.name = f"{name}_logs.txt"
        await query.edit_message_text(
            f"📎 <b>{name}</b> — last {tail} lines sent as file below.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb,
        )
        await query.message.reply_document(document=doc, caption=f"{name}_logs.txt")
    else:
        await query.edit_message_text(full, parse_mode=ParseMode.HTML, reply_markup=back_kb)


async def _show_errors_only(query, name: str) -> None:
    await query.edit_message_text(
        f"⏳ Fetching last 100 lines of <code>{name}</code> (errors only)…",
        parse_mode=ParseMode.HTML,
    )
    try:
        logs = await docker_ops.get_container_logs(name, tail=100)
        filtered = docker_ops.filter_error_lines(logs)
    except Exception as exc:
        filtered = f"Error: {exc}"

    back_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 All 100", callback_data=f"logs:{name}:100"),
        InlineKeyboardButton("◀️ Back", callback_data=f"container:{name}"),
    ]])

    if not filtered.strip():
        await query.edit_message_text(
            f"<b>{name}</b> — no error-like lines in the last 100.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb,
        )
        return

    header = f"<b>{name}</b> — errors only (last 100):\n\n"
    body = f"<code>{_escape(filtered)}</code>"
    full = header + body
    if len(full) > 4000:
        full = header + f"<code>{_escape(filtered[-(4000 - len(header)):])}</code>"
    await query.edit_message_text(full, parse_mode=ParseMode.HTML, reply_markup=back_kb)


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
