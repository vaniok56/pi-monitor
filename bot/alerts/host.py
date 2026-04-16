"""
Host resource watchdog — runs as a daemon thread, checks system stats every
60 seconds and fires alerts when thresholds are crossed.

Monitored:
  • RAM usage %
  • Swap usage %
  • Disk usage % (root filesystem)
  • CPU 1-minute load average (normalised to number of cores)
  • SoC / CPU temperature (via psutil or /sys/class/thermal)
  • System uptime
  • Top-5 heaviest containers (CPU % + RAM via docker stats)
"""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import psutil

from alerts import AlertItem, AlertType
from alerts.notifier import put_alert

logger = logging.getLogger(__name__)


def _get_device_name() -> str:
    """Return device model from devicetree, falling back to platform info."""
    # Raspberry Pi and many ARM SBCs expose this
    try:
        model = Path("/sys/firmware/devicetree/base/model").read_text().rstrip("\x00").strip()
        if model:
            return model
    except Exception:
        pass
    return platform.machine() or "Host"


def _read_thermal_zone(zone: int = 0) -> Optional[float]:
    """Read temperature from /sys/class/thermal/thermal_zone<n>/temp (millidegrees)."""
    p = Path(f"/sys/class/thermal/thermal_zone{zone}/temp")
    try:
        return float(p.read_text().strip()) / 1000.0
    except Exception:
        return None


def _get_temperature() -> Optional[float]:
    """Best-effort SoC temperature reading."""
    # 1. psutil
    try:
        temps = psutil.sensors_temperatures()
        for key in ("cpu_thermal", "soc_thermal", "thermal_zone0", "cpu-thermal"):
            if key in temps and temps[key]:
                return temps[key][0].current
    except (AttributeError, Exception):
        pass

    # 2. Direct /sys read
    val = _read_thermal_zone(0)
    if val is not None:
        return val

    # 3. vcgencmd (Pi-specific)
    try:
        out = subprocess.run(
            ["vcgencmd", "measure_temp"],
            capture_output=True, text=True, timeout=2
        ).stdout
        if out:
            return float(out.split("=")[1].split("'")[0])
    except Exception:
        pass

    return None


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n = int(n / 1024)
    return f"{n:.0f} TB"


def _fmt_uptime(seconds: float) -> str:
    total = int(seconds)
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


_docker_stats_cache: tuple[float, list[dict]] | None = None
_DOCKER_STATS_TTL = 5.0  # seconds


async def _get_docker_stats() -> list[dict]:
    """
    Run `docker stats --no-stream --format '{{json .}}'` and return
    a list of dicts with keys: Name, CPUPerc, MemUsage.
    Results are cached for 5 seconds to avoid hammering the daemon on rapid taps.
    Returns empty list on any error (e.g. Docker not accessible).
    """
    global _docker_stats_cache
    now = time.monotonic()
    if _docker_stats_cache is not None and now - _docker_stats_cache[0] < _DOCKER_STATS_TTL:
        return _docker_stats_cache[1]
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "stats", "--no-stream", "--format", "{{json .}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        results = []
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        _docker_stats_cache = (now, results)
        return results
    except Exception as exc:
        logger.debug("docker stats failed: %s", exc)
        return []


def _parse_cpu_pct(s: str) -> float:
    """Parse '2.34%' → 2.34."""
    try:
        return float(s.rstrip("%"))
    except Exception:
        return 0.0


def _parse_mem_bytes(s: str) -> int:
    """Parse '112MiB / 3.82GiB' → bytes used."""
    try:
        used = s.split("/")[0].strip()
        return _parse_size(used)
    except Exception:
        return 0


def _parse_size(s: str) -> int:
    """Parse '112MiB', '1.2GiB', '500kB', etc → bytes."""
    s = s.strip()
    for suffix, factor in (
        ("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10),
        ("GB", 10**9), ("MB", 10**6), ("KB", 10**3),
        ("B", 1),
    ):
        if s.endswith(suffix):
            try:
                return int(float(s[: -len(suffix)]) * factor)
            except Exception:
                return 0
    return 0


_host_stats_cache: tuple[float, dict] | None = None
_HOST_STATS_TTL = 1.0  # seconds


def get_host_stats_sync() -> dict:
    """Collect all host stats synchronously (for the watchdog thread and status display)."""
    global _host_stats_cache
    now = time.monotonic()
    if _host_stats_cache is not None and now - _host_stats_cache[0] < _HOST_STATS_TTL:
        return _host_stats_cache[1]
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    load1, load5, load15 = psutil.getloadavg()
    temp = _get_temperature()
    uptime_secs = time.time() - psutil.boot_time()
    result = {
        "mem": mem,
        "swap": swap,
        "disk": disk,
        "load": (load1, load5, load15),
        "temp": temp,
        "uptime_secs": uptime_secs,
    }
    _host_stats_cache = (now, result)
    return result


class HostWatchdog:
    def __init__(
        self,
        disk_pct: float,
        ram_pct: float,
        swap_pct: float,
        cpu_load: float,
        temp_c: float,
        interval: int = 60,
    ) -> None:
        self.disk_pct = disk_pct
        self.ram_pct = ram_pct
        self.swap_pct = swap_pct
        self.cpu_load = cpu_load
        self.temp_c = temp_c
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="host-watchdog", daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        logger.info("Host watchdog started (interval=%ds)", self.interval)

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(timeout=self.interval):
            try:
                self._check()
            except Exception:
                logger.exception("Host watchdog check failed")

    def _check(self) -> None:
        core_count = psutil.cpu_count(logical=True) or 1
        stats = get_host_stats_sync()
        mem = stats["mem"]
        swap = stats["swap"]
        disk = stats["disk"]
        load1, _, _ = stats["load"]
        temp = stats["temp"]

        if mem.percent >= self.ram_pct:
            put_alert(AlertItem(
                type=AlertType.HOST_RESOURCE,
                title="🧠 RAM critical",
                body=(
                    f"RAM usage: <b>{mem.percent:.1f}%</b> "
                    f"({_fmt_bytes(mem.used)} / {_fmt_bytes(mem.total)})"
                ),
                key="host:ram",
            ))

        if swap.total > 0 and swap.percent >= self.swap_pct:
            put_alert(AlertItem(
                type=AlertType.HOST_RESOURCE,
                title="💾 Swap high",
                body=(
                    f"Swap usage: <b>{swap.percent:.1f}%</b> "
                    f"({_fmt_bytes(swap.used)} / {_fmt_bytes(swap.total)})"
                ),
                key="host:swap",
            ))

        if disk.percent >= self.disk_pct:
            put_alert(AlertItem(
                type=AlertType.HOST_RESOURCE,
                title="💿 Disk critical",
                body=(
                    f"Disk (/) usage: <b>{disk.percent:.1f}%</b> "
                    f"({_fmt_bytes(disk.used)} / {_fmt_bytes(disk.total)}, "
                    f"free: {_fmt_bytes(disk.free)})"
                ),
                key="host:disk",
            ))

        load_per_core = load1 / core_count
        if load_per_core >= self.cpu_load:
            put_alert(AlertItem(
                type=AlertType.HOST_RESOURCE,
                title="🔥 CPU load high",
                body=(
                    f"1-min load avg: <b>{load1:.2f}</b> "
                    f"({load_per_core:.2f} per core, {core_count} cores)"
                ),
                key="host:cpu",
            ))

        if temp is not None and temp >= self.temp_c:
            put_alert(AlertItem(
                type=AlertType.HOST_RESOURCE,
                title="🌡 Temperature critical",
                body=f"SoC temperature: <b>{temp:.1f}°C</b>",
                key="host:temp",
            ))

    def host_status_text(self, docker_stats: Optional[list[dict]] = None) -> str:
        """
        Return a formatted host status string.

        docker_stats: output of _get_docker_stats() (top-5 heaviest).
        If None, the heaviest-containers section is omitted.
        """
        stats = get_host_stats_sync()
        mem = stats["mem"]
        swap = stats["swap"]
        disk = stats["disk"]
        load1, load5, load15 = stats["load"]
        temp = stats["temp"]
        uptime_secs = stats["uptime_secs"]

        lines = [
            f"🖥  <b>{_get_device_name()}</b> — up {_fmt_uptime(uptime_secs)}",
            "────────────────────────────",
            f"🧠  RAM:   {_fmt_bytes(mem.used)} / {_fmt_bytes(mem.total)}  ({mem.percent:.1f}%)",
        ]
        if swap.total > 0:
            lines.append(
                f"💤  Swap:  {_fmt_bytes(swap.used)} / {_fmt_bytes(swap.total)}  ({swap.percent:.1f}%)"
            )
        lines.append(
            f"💿  Disk:  {_fmt_bytes(disk.used)} / {_fmt_bytes(disk.total)}  ({disk.percent:.1f}%)"
        )
        lines.append(f"🔥  CPU:   {load1:.2f} / {load5:.2f} / {load15:.2f}  (load avg)")
        if temp is not None:
            lines.append(f"🌡  Temp:  {temp:.1f} °C")

        if docker_stats:
            # Sort by CPU%, take top 5
            def _cpu(r: dict) -> float:
                return _parse_cpu_pct(r.get("CPUPerc", "0%"))

            top5 = sorted(docker_stats, key=_cpu, reverse=True)[:5]
            lines.append("")
            lines.append("<b>Heaviest containers:</b>")
            for i, row in enumerate(top5, 1):
                name = row.get("Name", "?")[:20]
                cpu = row.get("CPUPerc", "0%")
                mem_raw = row.get("MemUsage", "0B / 0B")
                mem_used = _fmt_bytes(_parse_mem_bytes(mem_raw))
                lines.append(f"  {i}. {name:<22} {cpu:>6}  {mem_used}")

        return "\n".join(lines)
