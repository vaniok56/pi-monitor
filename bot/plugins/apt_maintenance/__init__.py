from __future__ import annotations

import asyncio
import logging
import re
import shlex
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from plugins._ctx import PluginMeta

if TYPE_CHECKING:
    from plugins._ctx import PluginContext

logger = logging.getLogger(__name__)

META = PluginMeta(
    name="apt_maintenance",
    description="Fast APT menu with separate update and cleanup actions",
    default_config={
        "max_listed_updates": 20,
        "helper_image": "alpine:3.20",
    },
)

_CB_MENU = "p.apt_maintenance:menu"
_CB_UPDATE_PREVIEW = "p.apt_maintenance:update_preview"
_CB_UPDATE_RUN = "p.apt_maintenance:update_run"
_CB_UPDATE_RUN_FORCE = "p.apt_maintenance:update_run_force"
_CB_CLEANUP_CONFIRM = "p.apt_maintenance:cleanup_confirm"
_CB_CLEANUP_RUN = "p.apt_maintenance:cleanup_run"
_CB_PLUGINS = "plugins_menu"

_DOCKER_KEYWORDS = (
    "docker",
    "containerd",
    "runc",
    "moby",
    "compose",
)


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _truncate(text: str, limit: int = 3900) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 64] + "\n\n<i>Output truncated for Telegram.</i>"


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬆️ Update", callback_data=_CB_UPDATE_PREVIEW),
            InlineKeyboardButton("🧹 Cleanup", callback_data=_CB_CLEANUP_CONFIRM),
        ],
        [InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS)],
    ])


def _update_preview_keyboard(can_run: bool) -> InlineKeyboardMarkup:
    if not can_run:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Back", callback_data=_CB_MENU),
        ]])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Run update + upgrade", callback_data=_CB_UPDATE_RUN)],
        [
            InlineKeyboardButton("🔄 Recheck", callback_data=_CB_UPDATE_PREVIEW),
            InlineKeyboardButton("❌ Cancel", callback_data=_CB_MENU),
        ],
    ])


def _cleanup_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Run cleanup", callback_data=_CB_CLEANUP_RUN),
        InlineKeyboardButton("❌ Cancel", callback_data=_CB_MENU),
    ]])


def _menu_text(result_note: str | None = None) -> str:
    lines = [
        "📦 <b>APT Maintenance</b>",
        "",
        "Choose an action:",
        "• <b>Update</b>: apt-get update + preview + confirm + apt-get upgrade -y",
        "• <b>Cleanup</b>: apt-get autoremove -y + apt-get clean",
    ]
    if result_note:
        lines.extend(["", result_note])
    return _truncate("\n".join(lines), limit=3500)


async def _run_host_shell(script: str, helper_image: str, timeout: int = 900) -> tuple[int, str]:
    # Run inside a privileged helper container, then enter host namespaces.
    wrapped = (
        "set -e; "
        "apk add --no-cache util-linux >/dev/null 2>&1; "
        f"nsenter -t 1 -m -u -i -n -p -- sh -lc {shlex.quote(script)}"
    )

    proc = await asyncio.create_subprocess_exec(
        "docker",
        "run",
        "--rm",
        "--pid=host",
        "--privileged",
        "--network=host",
        helper_image,
        "sh",
        "-lc",
        wrapped,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    output = (stdout + stderr).decode("utf-8", errors="replace")
    return proc.returncode, output


def _parse_upgradable_packages(simulation_output: str) -> list[str]:
    packages: list[str] = []
    for raw in simulation_output.splitlines():
        line = raw.strip()
        m = re.match(r"^Inst\s+([^\s:]+)", line)
        if m:
            packages.append(m.group(1))
    return packages


def _extract_upgrade_counts(text: str) -> tuple[int, int, int, int] | None:
    m = re.search(
        r"(\d+)\s+upgraded,\s+(\d+)\s+newly installed,\s+(\d+)\s+to remove and\s+(\d+)\s+not upgraded\.",
        text,
    )
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))


def _docker_related_packages(packages: list[str]) -> list[str]:
    hits: list[str] = []
    for pkg in packages:
        p = pkg.lower()
        if any(token in p for token in _DOCKER_KEYWORDS):
            hits.append(pkg)
    return hits


async def _collect_preview(max_listed_updates: int, helper_image: str) -> dict:
    script = """
set -e
export DEBIAN_FRONTEND=noninteractive
if ! command -v apt-get >/dev/null 2>&1; then
  echo __NO_APT__
  exit 0
fi
apt-get update
apt-get -s upgrade
""".strip()

    rc, output = await _run_host_shell(script, helper_image=helper_image, timeout=1200)

    if "__NO_APT__" in output:
        return {
            "unsupported": True,
            "error": None,
            "output_tail": output[-1200:],
        }

    if rc != 0:
        return {
            "unsupported": False,
            "error": f"Preview failed (exit {rc})",
            "output_tail": output[-1800:],
        }

    packages = _parse_upgradable_packages(output)
    listed_packages = packages[: max(max_listed_updates, 1)]
    docker_related = _docker_related_packages(packages)

    return {
        "unsupported": False,
        "error": None,
        "counts": _extract_upgrade_counts(output),
        "packages": packages,
        "listed_packages": listed_packages,
        "remaining_count": max(len(packages) - len(listed_packages), 0),
        "docker_related": docker_related,
    }


def _render_update_preview_text(preview: dict, result_note: str | None = None) -> str:
    if preview.get("unsupported"):
        lines = [
            "⬆️ <b>APT Update Preview</b>",
            "",
            "⚠️ This host does not expose <code>apt-get</code> in the host namespace.",
            "This plugin only works on Debian/Ubuntu-family hosts.",
        ]
        if result_note:
            lines.append(f"\n{result_note}")
        return "\n".join(lines)

    if preview.get("error"):
        lines = [
            "⬆️ <b>APT Update Preview</b>",
            "",
            f"❌ {_escape_html(preview['error'])}",
            "",
            "<b>Output tail</b>",
            f"<code>{_escape_html(preview.get('output_tail', ''))}</code>",
        ]
        if result_note:
            lines.append(f"\n{result_note}")
        return _truncate("\n".join(lines))

    packages = preview.get("packages", [])
    listed = preview.get("listed_packages", [])
    counts = preview.get("counts")
    docker_related = preview.get("docker_related", [])

    lines = [
        "⬆️ <b>APT Update Preview</b>",
        "",
        f"Available updates: <b>{len(packages)}</b>",
    ]

    if counts:
        upgraded, newly_installed, to_remove, not_upgraded = counts
        lines.extend([
            "",
            "Simulation summary:",
            f"• {upgraded} upgraded",
            f"• {newly_installed} newly installed",
            f"• {to_remove} to remove",
            f"• {not_upgraded} held back",
        ])

    if listed:
        lines.extend([
            "",
            f"Upgradable packages (showing {len(listed)}):",
            "<code>" + _escape_html("\n".join(listed)) + "</code>",
        ])

    if preview.get("remaining_count", 0) > 0:
        lines.append(f"…and {preview['remaining_count']} more")

    if docker_related:
        shown = ", ".join(docker_related[:8])
        extra = len(docker_related) - min(len(docker_related), 8)
        suffix = f" (+{extra} more)" if extra > 0 else ""
        lines.extend([
            "",
            f"⚠️ <b>Docker-related updates detected:</b> <code>{_escape_html(shown)}{suffix}</code>",
            "Extra confirmation is required before running upgrade.",
        ])

    if result_note:
        lines.extend(["", result_note])

    lines.extend([
        "",
        "Proceed with <b>Run update + upgrade</b> when ready.",
    ])
    return _truncate("\n".join(lines))


async def _execute_update_flow(helper_image: str) -> tuple[int, str]:
    script = """
set -e
export DEBIAN_FRONTEND=noninteractive
if ! command -v apt-get >/dev/null 2>&1; then
  echo __NO_APT__
  exit 0
fi
echo __STEP_UPDATE__
apt-get update
echo __STEP_UPGRADE__
apt-get upgrade -y
""".strip()

    return await _run_host_shell(script, helper_image=helper_image, timeout=3600)


async def _execute_cleanup_flow(helper_image: str) -> tuple[int, str]:
    script = """
set -e
export DEBIAN_FRONTEND=noninteractive
if ! command -v apt-get >/dev/null 2>&1; then
  echo __NO_APT__
  exit 0
fi
echo __STEP_AUTOREMOVE__
apt-get autoremove -y
echo __STEP_CLEAN__
apt-get clean
""".strip()

    return await _run_host_shell(script, helper_image=helper_image, timeout=2400)


def _summarize_update_execution(output: str, rc: int) -> str:
    if "__NO_APT__" in output:
        return "⚠️ Last run: host has no apt-get; nothing was changed."

    counts = _extract_upgrade_counts(output)

    lines = []
    if rc == 0:
        lines.append("✅ <b>Last run summary</b>: apt-get update + apt-get upgrade -y completed.")
    else:
        lines.append(f"❌ <b>Last run summary</b>: failed with exit code {rc}.")

    if counts:
        upgraded, newly_installed, to_remove, not_upgraded = counts
        lines.append(
            f"• Upgrade result: {upgraded} upgraded, {newly_installed} newly installed, {to_remove} to remove, {not_upgraded} not upgraded"
        )

    if rc != 0:
        lines.append("\n<code>" + _escape_html(output[-1000:]) + "</code>")

    return _truncate("\n".join(lines), limit=1600)


def _summarize_cleanup_execution(output: str, rc: int) -> str:
    if "__NO_APT__" in output:
        return "⚠️ Last cleanup: host has no apt-get; nothing was changed."

    removed_pkgs = re.findall(r"^Removing\s+([^\s]+)", output, flags=re.MULTILINE)
    freed_match = re.search(
        r"After this operation,\s+([^\n]+)\s+disk space will be freed\.",
        output,
    )

    lines = []
    if rc == 0:
        lines.append("✅ <b>Last cleanup summary</b>: apt-get autoremove -y + apt-get clean completed.")
    else:
        lines.append(f"❌ <b>Last cleanup summary</b>: failed with exit code {rc}.")

    if removed_pkgs:
        lines.append(f"• Removed packages: {len(removed_pkgs)}")

    if freed_match:
        lines.append(f"• Space freed: {freed_match.group(1)}")

    if rc != 0:
        lines.append("\n<code>" + _escape_html(output[-1000:]) + "</code>")

    return _truncate("\n".join(lines), limit=1600)


async def _handle_action(query, parts, ctx: "PluginContext") -> None:
    sub = parts[1] if len(parts) > 1 else "menu"
    max_listed_updates = int(ctx.plugin_cfg.get("max_listed_updates", 20))
    helper_image = str(ctx.plugin_cfg.get("helper_image", "alpine:3.20"))

    if sub in {"menu", "report"}:
        await query.edit_message_text(
            _menu_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=_menu_keyboard(),
        )

    elif sub in {"update_preview", "confirm"}:
        preview = await _collect_preview(max_listed_updates, helper_image)
        can_run = not preview.get("unsupported") and not preview.get("error")
        await query.edit_message_text(
            _render_update_preview_text(preview),
            parse_mode=ParseMode.HTML,
            reply_markup=_update_preview_keyboard(can_run),
        )

    elif sub in {"update_run", "run", "update_run_force"}:
        preview = await _collect_preview(max_listed_updates, helper_image)
        if preview.get("unsupported") or preview.get("error"):
            await query.edit_message_text(
                _render_update_preview_text(preview),
                parse_mode=ParseMode.HTML,
                reply_markup=_update_preview_keyboard(False),
            )
            return

        docker_related = preview.get("docker_related", [])
        if docker_related and sub != "update_run_force":
            shown = ", ".join(docker_related[:8])
            extra = len(docker_related) - min(len(docker_related), 8)
            suffix = f" (+{extra} more)" if extra > 0 else ""
            await query.edit_message_text(
                "⚠️ <b>Docker-related updates detected</b>\n\n"
                f"<code>{_escape_html(shown)}{suffix}</code>\n\n"
                "Confirming update will run:\n"
                "• <code>apt-get update</code>\n"
                "• <code>apt-get upgrade -y</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Yes, run update", callback_data=_CB_UPDATE_RUN_FORCE),
                    InlineKeyboardButton("❌ Cancel", callback_data=_CB_MENU),
                ]]),
            )
            return

        await query.edit_message_text("⏳ Running apt update + upgrade…")
        rc, output = await _execute_update_flow(helper_image)
        summary = _summarize_update_execution(output, rc)
        await query.edit_message_text(
            _menu_text(summary),
            parse_mode=ParseMode.HTML,
            reply_markup=_menu_keyboard(),
        )

    elif sub == "cleanup_confirm":
        await query.edit_message_text(
            "⚠️ <b>Run cleanup now?</b>\n\n"
            "This will execute:\n"
            "• <code>apt-get autoremove -y</code>\n"
            "• <code>apt-get clean</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=_cleanup_confirm_keyboard(),
        )

    elif sub == "cleanup_run":
        await query.edit_message_text("⏳ Running apt cleanup…")
        rc, output = await _execute_cleanup_flow(helper_image)
        summary = _summarize_cleanup_execution(output, rc)
        await query.edit_message_text(
            _menu_text(summary),
            parse_mode=ParseMode.HTML,
            reply_markup=_menu_keyboard(),
        )


def register(ctx: "PluginContext") -> None:
    ctx.actions.register("p.apt_maintenance", _handle_action)
    ctx.buttons.add("📦 APT maintenance", _CB_MENU, sort_key=23)
