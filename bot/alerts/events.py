"""
Docker events monitor — runs as a daemon thread, feeds AlertItems into the
notifier queue.

Detects:
  • Container crash   — "die" event with non-zero exit code
  • Restart loop      — >3 start/restart events within 2 minutes
  • Health check fail — health_status=unhealthy event

Crash alerts are enriched with:
  • Last 15 log lines (quick tail)
  • depends_on sibling health (from alerts/deps.py)
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque

import docker

from alerts import AlertItem, AlertType
from alerts.notifier import put_alert

logger = logging.getLogger(__name__)

# Containers that are allowed to exit with code 0 without triggering a crash alert.
# Add any one-shot or cron-style containers here (e.g. init tasks, backup jobs).
_EXPECTED_EXIT_ZERO = {"docker-prune"}

# Restart-loop tracking: container → deque of timestamps
_restart_times: dict[str, deque] = defaultdict(deque)
_restart_lock = threading.Lock()
_RESTART_WINDOW = 120   # seconds
_RESTART_THRESHOLD = 3  # restarts in window before alerting
_INTERNAL_HELPER_LABEL = "com.pi-monitor.internal_helper"


def _get_quick_tail(name: str, lines: int = 15) -> str:
    """Synchronous best-effort tail of last N log lines."""
    try:
        client = docker.from_env()
        container = client.containers.get(name)
        raw = container.logs(tail=lines, timestamps=False)
        return raw.decode("utf-8", errors="replace").strip()
    except Exception as exc:
        return f"(could not fetch logs: {exc})"


def _get_sibling_health(name: str) -> str:
    """Return formatted depends_on sibling health for the crash alert body."""
    try:
        client = docker.from_env()
        container = client.containers.get(name)
        from alerts.deps import dependencies_of
        deps = dependencies_of(container)
        if not deps:
            return ""
        lines = ["<b>Dependencies:</b>"]
        for sibling, emoji in deps:
            lines.append(f"  • {sibling}   {emoji}")
        return "\n".join(lines)
    except Exception as exc:
        logger.debug("sibling health check failed for %s: %s", name, exc)
        return ""


class DockerEventsMonitor:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="docker-events", daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        logger.info("Docker events monitor started")

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        client = docker.from_env()
        while not self._stop.is_set():
            try:
                for event in client.events(decode=True):
                    if self._stop.is_set():
                        break
                    self._handle(event)
            except Exception as exc:
                logger.warning("Docker events stream error (%s) — reconnecting in 5s", exc)
                time.sleep(5)

    def _handle(self, event: dict) -> None:
        if event.get("Type") != "container":
            return

        action = event.get("Action", "")
        attrs = event.get("Actor", {}).get("Attributes", {})
        name = attrs.get("name", "unknown")

        # Ignore short-lived internal helper containers spawned by plugins.
        if attrs.get(_INTERNAL_HELPER_LABEL) == "true":
            return

        # ── Crash ─────────────────────────────────────────────────────────
        if action == "die":
            exit_code = attrs.get("exitCode", "0")
            if exit_code != "0" and name not in _EXPECTED_EXIT_ZERO:
                # Enrich with tail + sibling health
                tail = _get_quick_tail(name, lines=15)
                siblings = _get_sibling_health(name)
                family = attrs.get("com.docker.compose.project", "")

                body_parts = [
                    f"Container <code>{name}</code> exited with code <b>{exit_code}</b>.",
                ]
                if family:
                    body_parts.append(f"Family:    {family}")
                if siblings:
                    body_parts.append(siblings)
                if tail:
                    body_parts.append(f"\n<b>Last 15 lines:</b>\n<code>{_escape(tail[-1500:])}</code>")

                put_alert(AlertItem(
                    type=AlertType.CRASH,
                    title=f"💥 {name} crashed",
                    body="\n".join(body_parts),
                    key=f"crash:{name}",
                    container=name,
                    family=family or None,
                    show_container_buttons=True,
                ))

        # ── Restart loop ──────────────────────────────────────────────────
        elif action in ("start", "restart"):
            with _restart_lock:
                times = _restart_times[name]
                now = time.monotonic()
                times.append(now)
                while times and times[0] < now - _RESTART_WINDOW:
                    times.popleft()
                if len(times) > _RESTART_THRESHOLD:
                    times.clear()
                    put_alert(AlertItem(
                        type=AlertType.RESTART_LOOP,
                        title=f"🔁 {name} restart loop",
                        body=(
                            f"Container <code>{name}</code> has restarted more than "
                            f"{_RESTART_THRESHOLD} times in the last "
                            f"{_RESTART_WINDOW // 60} minutes."
                        ),
                        key=f"restart_loop:{name}",
                        container=name,
                        family=attrs.get("com.docker.compose.project") or None,
                        show_container_buttons=True,
                    ))

        # ── Health check failure ──────────────────────────────────────────
        elif action == "health_status":
            status = attrs.get("health_status", "")
            if status == "unhealthy":
                put_alert(AlertItem(
                    type=AlertType.UNHEALTHY,
                    title=f"🏥 {name} unhealthy",
                    body=(
                        f"Container <code>{name}</code> health check is "
                        f"reporting <b>unhealthy</b>."
                    ),
                    key=f"unhealthy:{name}",
                    container=name,
                    family=attrs.get("com.docker.compose.project") or None,
                    show_container_buttons=True,
                ))


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
