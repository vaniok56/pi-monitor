"""
Log-loop detector.

One daemon thread per running container streams its logs and watches for
repeated error patterns using fingerprinting + sliding-window counting.

Algorithm per line:
  1. Interest filter  — drop lines that don't match any configured regex.
  2. Ignore filter    — drop lines that match a configured ignore pattern.
  3. Traceback stitch — join indented continuation lines to the header.
  4. Fingerprint      — mask timestamps, IPs, IDs, numbers → stable signature.
  5. Sliding window   — count same-signature occurrences in a time window.
  6. Alert            — if count >= threshold, fire once (cooldown-guarded).
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections import defaultdict, deque
from typing import Optional

import docker
import yaml
from pathlib import Path

from alerts import AlertItem, AlertType
from alerts.notifier import put_alert

logger = logging.getLogger(__name__)

# ── Fingerprinting regexes ───────────────────────────────────────────────────
_FP_RULES: list[tuple[re.Pattern, str]] = [
    # Leading timestamps: HH:MM:SS.mmm, ISO 8601, syslog, Docker prefix
    (re.compile(
        r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:\d{2})?\s*"
        r"|^\d{2}:\d{2}:\d{2}[.,]\d+\s*\|\s*"
        r"|^\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+"
    ), ""),
    # IPv6
    (re.compile(r"[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4}){5,7}"), "<IP>"),
    # IPv4 with optional port
    (re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?"), "<IP>"),
    # Standalone port references like :8081 or port=9000
    (re.compile(r"(?:port[=: ]+|:)\d{2,5}\b"), "<PORT>"),
    # UUIDs
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "<ID>"),
    # Long hex strings (>= 8 chars)
    (re.compile(r"\b[0-9a-fA-F]{8,}\b"), "<ID>"),
    # File paths
    (re.compile(r'(?:^|[\s"])\/(?:[\w.\-]+\/)*[\w.\-]+'), " <PATH>"),
    # Quoted strings
    (re.compile(r'"[^"]{0,120}"'), '"<STR>"'),
    (re.compile(r"'[^']{0,120}'"), "'<STR>'"),
    # Pure numbers >= 4 digits
    (re.compile(r"\b\d{4,}\b"), "<NUM>"),
    # Collapse whitespace
    (re.compile(r"\s+"), " "),
]

# Lines whose indentation suggests they're continuation of a traceback
_TRACEBACK_CONT = re.compile(r"^\s{2,}(?:at |File \"|Traceback |\.|->)")


def _fingerprint(line: str) -> str:
    """Strip volatile parts of a log line to produce a stable signature."""
    sig = line
    for pattern, replacement in _FP_RULES:
        sig = pattern.sub(replacement, sig)
    return sig.strip()


def _sig_hash(sig: str) -> str:
    return hashlib.sha1(sig.encode()).hexdigest()[:12]


# ── Rule loading ─────────────────────────────────────────────────────────────

def _load_rules() -> dict:
    p = Path(__file__).parent.parent / "config" / "log_rules.yml"
    with open(p) as f:
        return yaml.safe_load(f)


def _compile_rules(raw: dict, container_name: str) -> dict:
    """Return merged rules for a specific container."""
    defaults = raw.get("defaults", {})
    overrides = raw.get("containers", {}).get(container_name, {})

    def _get(key, fallback):
        return overrides.get(key, defaults.get(key, fallback))

    interesting_pats = _get("interesting", ["ERROR", "WARN"])
    ignore_pats = _get("ignore", [])

    return {
        "interesting": re.compile(
            "|".join(f"(?:{p})" for p in interesting_pats), re.IGNORECASE
        ),
        "ignore": re.compile(
            "|".join(f"(?:{p})" for p in ignore_pats), re.IGNORECASE
        ) if ignore_pats else None,
        "window": int(_get("window_seconds", 60)),
        "threshold": int(_get("threshold", 20)),
        "cooldown": int(_get("cooldown_minutes", 10)) * 60,
    }


# ── Per-container tailer ─────────────────────────────────────────────────────

_MAX_LINES_PER_SECOND = 10_000
_FLOOD_WINDOW = 5  # seconds


class ContainerLogTailer:
    """Daemon thread that tails one container and feeds the log-loop detector."""

    def __init__(self, container_name: str, rules_raw: dict) -> None:
        self.name = container_name
        self._rules = _compile_rules(rules_raw, container_name)
        self._stop = threading.Event()
        self._cooldowns: dict[str, float] = {}  # sig_hash → last fire time
        self._windows: dict[str, deque] = defaultdict(deque)
        self._last_lines: deque = deque(maxlen=10)  # for alert payload
        self._thread = threading.Thread(
            target=self._run, name=f"log-{container_name}", daemon=True
        )
        # Traceback stitching
        self._pending_header: Optional[str] = None
        # Flood guard
        self._line_times: deque = deque()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        client = docker.from_env()
        while not self._stop.is_set():
            try:
                container = client.containers.get(self.name)
                log_stream = container.logs(stream=True, follow=True, tail=0)
                for raw_bytes in log_stream:
                    if self._stop.is_set():
                        break
                    self._process(raw_bytes.decode("utf-8", errors="replace").rstrip("\n"))
            except docker.errors.NotFound:
                break  # container gone; tailer exits
            except Exception as exc:
                logger.debug("Log tailer %s error: %s", self.name, exc)
                time.sleep(3)

    def _process(self, line: str) -> None:
        # ── Flood guard ───────────────────────────────────────────────────
        now = time.monotonic()
        self._line_times.append(now)
        while self._line_times and self._line_times[0] < now - _FLOOD_WINDOW:
            self._line_times.popleft()
        if len(self._line_times) > _MAX_LINES_PER_SECOND * _FLOOD_WINDOW:
            put_alert(AlertItem(
                type=AlertType.LOG_FLOOD,
                title=f"🌊 {self.name} log flood",
                body=(
                    f"Container <code>{self.name}</code> is emitting more than "
                    f"{_MAX_LINES_PER_SECOND:,} lines/s. Log monitoring paused."
                ),
                key=f"logflood:{self.name}",
                container=self.name,
                show_container_buttons=True,
            ))
            # Back off — let the container calm down
            time.sleep(30)
            self._line_times.clear()
            return

        # ── Traceback stitching ───────────────────────────────────────────
        if _TRACEBACK_CONT.match(line) and self._pending_header is not None:
            self._pending_header += " " + line.strip()
            return
        if self._pending_header is not None:
            self._emit(self._pending_header)
        self._pending_header = line

    def _emit(self, line: str) -> None:
        """Run the fingerprint + window logic on a completed (stitched) line."""
        rules = self._rules
        self._last_lines.append(line)

        # 1. Interest filter
        if not rules["interesting"].search(line):
            return

        # 2. Ignore filter
        if rules["ignore"] and rules["ignore"].search(line):
            return

        # 3. Fingerprint
        sig = _fingerprint(line)
        sh = _sig_hash(sig)

        # 4. Sliding window
        now = time.monotonic()
        window = self._windows[sh]
        window.append(now)
        while window and window[0] < now - rules["window"]:
            window.popleft()

        if len(window) < rules["threshold"]:
            return

        # 5. Cooldown
        last = self._cooldowns.get(sh, 0.0)
        if now - last < rules["cooldown"]:
            return
        self._cooldowns[sh] = now
        window.clear()  # reset so the next burst can fire after cooldown

        # 6. Fire alert
        last_lines = "\n".join(self._last_lines)
        put_alert(AlertItem(
            type=AlertType.LOG_LOOP,
            title=f"⚠️ {self.name} — log loop",
            body=(
                f"Signature repeated <b>{rules['threshold']}+</b> times "
                f"in {rules['window']}s:\n"
                f"<code>{_truncate(sig, 200)}</code>\n\n"
                f"<b>Last 10 lines:</b>\n<code>{_truncate(last_lines, 1500)}</code>"
            ),
            key=f"logloop:{self.name}:{sh}",
            container=self.name,
            show_container_buttons=True,
            sig_hash=sh,
        ))


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"


# ── Manager ──────────────────────────────────────────────────────────────────

class LogLoopManager:
    """
    Tracks which containers are running and keeps a ContainerLogTailer alive
    for each. Reacts to docker events via update_container().
    """

    def __init__(self) -> None:
        self._tailers: dict[str, ContainerLogTailer] = {}
        self._lock = threading.Lock()
        self._rules_raw: dict = {}

    def start(self) -> None:
        self._rules_raw = _load_rules()
        client = docker.from_env()
        for c in client.containers.list():
            self._spawn(c.name)
        logger.info("Log loop manager started with %d tailers", len(self._tailers))

    def update_container(self, name: str, action: str) -> None:
        """Called from docker events monitor on start/die/destroy."""
        if action in ("start",):
            self._spawn(name)
        elif action in ("die", "destroy", "stop"):
            self._reap(name)

    def _spawn(self, name: str) -> None:
        with self._lock:
            if name in self._tailers and self._tailers[name].is_alive():
                return
            tailer = ContainerLogTailer(name, self._rules_raw)
            tailer.start()
            self._tailers[name] = tailer
            logger.debug("Log tailer started: %s", name)

    def _reap(self, name: str) -> None:
        with self._lock:
            tailer = self._tailers.pop(name, None)
        if tailer:
            tailer.stop()
            logger.debug("Log tailer stopped: %s", name)

    def reload_rules(self) -> None:
        """Hot-reload log_rules.yml without restarting tailers."""
        self._rules_raw = _load_rules()
        with self._lock:
            for name, tailer in self._tailers.items():
                tailer._rules = _compile_rules(self._rules_raw, name)
        logger.info("Log rules reloaded")

    def inject_test_lines(self, container_name: str, lines: list[str]) -> None:
        """Used by /testalert to inject synthetic log lines."""
        with self._lock:
            tailer = self._tailers.get(container_name)
        if tailer:
            for line in lines:
                tailer._process(line)
