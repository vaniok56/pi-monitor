"""power_report — daily & weekly electricity reports from Intel RAPL / psys.

Measures host power the same way **vigil-tui** does
(https://github.com/GIN-SYSTEMS/vigil-tui): it reads the kernel's RAPL
energy counters under ``/sys/class/powercap`` and derives instantaneous
watts from the counter delta over elapsed time.

Strategy waterfall (best -> fallback), auto-detected at startup:

    1. psys        — intel-rapl:1 "psys" platform domain. Whole-SoC power
                     (package + DRAM + other on-package rails). Closest proxy
                     to real electricity draw of the compute.
    2. package     — intel-rapl:0 "package-0" + the "dram" subzone if present.
    3. estimate    — CPU% x configured TDP. Always works, needs no sysfs.

Because RAPL exposes a *cumulative energy* counter (microjoules), energy over
any period is computed exactly by summing counter deltas — not by integrating
sampled wattage. min/max wattage are the extremes of the per-sample average
power (one sample = one ``sample_interval_seconds`` window).

Two scheduled reports are broadcast to all allowed users:
  • daily  — last 24 h  (default 09:00 local)
  • weekly — last 7 days (default Mon 09:00 local, with per-day breakdown
             and an extrapolated monthly estimate)

Cost is priced at ``mdl_per_kwh`` (default 3.59 MDL/kWh) and converted to USD
via ``mdl_per_usd`` (default 17.63 MDL per USD).

NOTE: RAPL/psys measures the SoC, not the wall socket — it excludes the
display, USB peripherals (external drives / hubs), and PSU/charger losses, so
the cost is a lower bound on true wall draw. Set ``overhead_watts`` to add a
flat always-on adder and calibrate toward a real meter reading.

Requires the host RAPL tree bind-mounted read-only into the container:
    - /sys/devices/virtual/powercap/intel-rapl:/host/powercap:ro
mounted OUTSIDE /sys because runc masks /sys/devices/virtual/powercap by
default (PLATYPUS / CVE-2020-8694 mitigation). ``powercap_root`` then points at
/host/powercap. The container runs as root, so the 0400 root-owned energy_uj
files are readable. Without the mount the plugin transparently falls back to
estimate.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import psutil
import timez
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from plugins._ctx import PluginMeta

if TYPE_CHECKING:
    from plugins._ctx import PluginContext

logger = logging.getLogger(__name__)

META = PluginMeta(
    name="power_report",
    description="Daily & weekly electricity reports (min/max watts + cost in MDL/USD) via Intel RAPL/psys",
    default_config={
        "sample_interval_seconds": 20,   # how often to poll the energy counter
        "mdl_per_kwh": 3.59,             # electricity tariff
        "mdl_per_usd": 17.63,            # FX rate for the USD figure
        "source": "auto",               # auto | psys | package | estimate
        "powercap_root": "/sys/class/powercap",
        "cpu_tdp_watts": 15.0,           # estimate-fallback ceiling (i7-7600U = 15 W)
        "overhead_watts": 0.0,           # flat adder for draw RAPL can't see
        "daily_schedule": "0 9 * * *",   # cron, configured TZ
        "weekly_schedule": "0 9 * * 1",  # cron, configured TZ (Mon 09:00)
        "history_path": "/data/power_history.json",
        "retention_days": 9,             # keep a little over a week of buckets
        "gap_reset_seconds": 120,        # dt above this => re-baseline (skip sample)
        "flush_interval_seconds": 300,   # how often to persist buckets to disk
    },
)

_CB_MENU = "p.power_report:menu"
_CB_TEST = "p.power_report:test"
_CB_PLUGINS = "plugins_menu"

_UJ_PER_KWH = 3.6e12  # microjoules in one kilowatt-hour (1e6 J/MJ.. -> J then /3.6e6)

# Module-global runtime state, populated by register().
_G: dict = {}


# ── power sources ────────────────────────────────────────────────────────────

class _RaplZone:
    """One RAPL energy domain. sample() -> (delta_uj, dt) or None on baseline."""

    def __init__(self, energy_path: Path, max_uj: Optional[int]) -> None:
        self.path = energy_path
        self.max_uj = max_uj
        self._last: Optional[int] = None
        self._last_t: float = 0.0

    def read_raw(self) -> int:
        return int(self.path.read_text().strip())

    def sample(self) -> Optional[tuple[int, float]]:
        now = time.monotonic()
        cur = self.read_raw()
        if self._last is None:
            self._last, self._last_t = cur, now
            return None
        dt = now - self._last_t
        if dt < 0.05:
            return None
        delta = cur - self._last
        if delta < 0 and self.max_uj is not None:
            delta += self.max_uj  # single counter wrap
        self._last, self._last_t = cur, now
        return max(0, delta), dt


class _RaplSource:
    """Sums one or more RAPL zones (e.g. package + dram) into one reading."""

    def __init__(self, zones: list[_RaplZone], label: str) -> None:
        self.zones = zones
        self.label = label

    def sample(self) -> Optional[tuple[float, float, float]]:
        total_uj = 0
        dt = None
        for z in self.zones:
            r = z.sample()
            if r is None:
                return None  # a zone is still baselining
            d, zdt = r
            total_uj += d
            dt = zdt
        if not dt or dt <= 0:
            return None
        watts = (total_uj / 1_000_000.0) / dt
        return watts, float(total_uj), dt


class _EstimateSource:
    """Fallback: watts = CPU% x TDP. Energy derived from watts x dt."""

    label = "estimate (CPU load x TDP)"

    def __init__(self, tdp_watts: float) -> None:
        self.tdp = tdp_watts
        self._last_t: Optional[float] = None

    def sample(self) -> Optional[tuple[float, float, float]]:
        now = time.monotonic()
        pct = psutil.cpu_percent(interval=None)
        if self._last_t is None:
            self._last_t = now
            return None
        dt = now - self._last_t
        self._last_t = now
        if dt <= 0:
            return None
        watts = (pct / 100.0) * self.tdp
        return watts, watts * dt * 1_000_000.0, dt


# ── source discovery ─────────────────────────────────────────────────────────

def _scan_zones(root: Path) -> dict[str, _RaplZone]:
    """Map RAPL zone name -> _RaplZone for every readable domain under root.

    Walks ``intel-rapl*`` entries recursively so it works for both layouts:

      • ``/sys/class/powercap`` — every domain is a flat *symlink* child
        (package-0, psys, and the core/uncore/dram subzones all side by side).
      • ``/sys/devices/virtual/powercap/intel-rapl`` — the real tree, where the
        dram/core/uncore subzones are nested inside ``intel-rapl:0/``. This is
        the path used inside Docker, bind-mounted to escape runc's default
        RAPL mask on ``/sys/devices/virtual/powercap``.

    ``intel-rapl-mmio*`` mirror domains are skipped (duplicate names, and their
    energy counters may not advance).
    """
    zones: dict[str, _RaplZone] = {}
    root = Path(root)
    if not root.exists():
        return zones

    seen: set = set()
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            real = d.resolve()
        except OSError:
            continue
        if real in seen or depth > 4:
            continue
        seen.add(real)

        name_f = d / "name"
        energy_f = d / "energy_uj"
        if name_f.exists() and energy_f.exists():
            try:
                name = name_f.read_text().strip()
                energy_f.read_text()  # permission / readability probe
                max_uj: Optional[int] = None
                try:
                    max_uj = int((d / "max_energy_range_uj").read_text().strip())
                except (OSError, ValueError):
                    pass
                if name and name not in zones:
                    zones[name] = _RaplZone(energy_f, max_uj)
            except OSError:
                pass

        try:
            children = sorted(d.iterdir())
        except OSError:
            continue
        for child in children:
            base = child.name
            if not base.startswith("intel-rapl") or "mmio" in base:
                continue
            try:
                if child.is_dir():  # follows symlinks
                    stack.append((child, depth + 1))
            except OSError:
                continue
    return zones


def _increments(zone: _RaplZone, probe_s: float = 0.4) -> bool:
    """True if the counter advances — guards against present-but-dead domains."""
    try:
        a = zone.read_raw()
        time.sleep(probe_s)
        b = zone.read_raw()
    except OSError:
        return False
    return (b - a) != 0  # wrap or advance; a real domain moves in <1s


def _discover_source(cfg: dict):
    """Pick the best available power source per cfg['source'] preference."""
    prefer = str(cfg.get("source", "auto")).lower()
    root = Path(cfg.get("powercap_root", "/sys/class/powercap"))
    tdp = float(cfg.get("cpu_tdp_watts", 15.0))

    if prefer == "estimate":
        return _EstimateSource(tdp)

    zones: dict[str, _RaplZone] = {}
    try:
        zones = _scan_zones(root)
    except Exception as exc:
        logger.warning("power_report: RAPL scan failed: %s", exc)

    if prefer in ("auto", "psys") and "psys" in zones:
        if _increments(zones["psys"]):
            logger.info("power_report: source = psys (platform)")
            return _RaplSource([zones["psys"]], "psys (platform, RAPL)")
        logger.info("power_report: psys present but not counting; trying package")

    if prefer in ("auto", "package") and "package-0" in zones:
        if _increments(zones["package-0"]):
            picked = [zones["package-0"]]
            label = "package-0 (RAPL)"
            if "dram" in zones:
                picked.append(zones["dram"])
                label = "package-0 + dram (RAPL)"
            logger.info("power_report: source = %s", label)
            return _RaplSource(picked, label)

    # Nothing usable (no mount / no perms / prefer mismatch) -> estimate.
    logger.info(
        "power_report: no RAPL domain available (scanned %s) — using estimate",
        root,
    )
    return _EstimateSource(tdp)


# ── bucket store ─────────────────────────────────────────────────────────────
#
# Energy is aggregated into hourly buckets keyed by UTC hour ("YYYY-MM-DDThh").
# Each bucket: e_uj (summed counter deltas), wmin/wmax (extreme sample watts),
# n (sample count), dur (summed elapsed seconds), t0/t1 (first/last sample ISO).

def _load_store(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("power_report: history load failed: %s", exc)
        return {}
    if isinstance(data, dict) and isinstance(data.get("buckets"), dict):
        return data["buckets"]
    return {}


def _save_store(path: str, buckets: dict, source_label: str) -> None:
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump({"version": 1, "source": source_label, "buckets": buckets}, f)
        os.replace(tmp, path)
    except Exception as exc:
        logger.error("power_report: save failed: %s", exc)


def _trim(buckets: dict, retention_days: int) -> None:
    cutoff = (timez.utcnow() - timedelta(days=retention_days)).isoformat()
    for key in [k for k, b in buckets.items() if b.get("t1", "") < cutoff]:
        del buckets[key]


def _record(buckets: dict, watts: float, delta_uj: float, dt: float) -> None:
    now = timez.utcnow()
    key = now.strftime("%Y-%m-%dT%H")
    iso = now.isoformat()
    b = buckets.get(key)
    if b is None:
        b = {"e_uj": 0.0, "wmin": watts, "wmax": watts, "n": 0, "dur": 0.0,
             "t0": iso, "t1": iso}
        buckets[key] = b
    b["e_uj"] += delta_uj
    b["wmin"] = min(b["wmin"], watts)
    b["wmax"] = max(b["wmax"], watts)
    b["n"] += 1
    b["dur"] += dt
    b["t1"] = iso


# ── aggregation & formatting ─────────────────────────────────────────────────

def _aggregate(buckets: dict, window_hours: float) -> dict:
    cutoff = (timez.utcnow() - timedelta(hours=window_hours)).isoformat()
    e_uj = 0.0
    dur = 0.0
    n = 0
    wmin = None
    wmax = None
    for b in buckets.values():
        if b.get("t1", "") < cutoff:
            continue
        e_uj += b["e_uj"]
        dur += b["dur"]
        n += b["n"]
        if b["n"] > 0:
            wmin = b["wmin"] if wmin is None else min(wmin, b["wmin"])
            wmax = b["wmax"] if wmax is None else max(wmax, b["wmax"])
    kwh = e_uj / _UJ_PER_KWH
    avg_w = (e_uj / 1_000_000.0) / dur if dur > 0 else 0.0
    coverage = dur / (window_hours * 3600.0) if window_hours > 0 else 0.0
    return {
        "kwh": kwh, "avg_w": avg_w, "wmin": wmin, "wmax": wmax,
        "n": n, "dur_h": dur / 3600.0, "coverage": coverage,
    }


def _per_day(buckets: dict, days: int, mdl_per_kwh: float) -> list[tuple[str, float, float]]:
    """(local-date label, kWh, MDL) for the last `days` local calendar days."""
    from datetime import datetime

    tz = timez._tz
    by_date: dict = {}
    for b in buckets.values():
        try:
            d = datetime.fromisoformat(b["t1"]).astimezone(tz).date()
        except Exception:
            continue
        by_date[d] = by_date.get(d, 0.0) + b["e_uj"]
    today = timez.now().date()
    out: list[tuple[str, float, float]] = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        kwh = by_date.get(d, 0.0) / _UJ_PER_KWH
        out.append((d.strftime("%a %m-%d"), kwh, kwh * mdl_per_kwh))
    return out


def _money(kwh: float, mdl_per_kwh: float, mdl_per_usd: float) -> str:
    mdl = kwh * mdl_per_kwh
    usd = mdl / mdl_per_usd if mdl_per_usd else 0.0
    return f"<b>{mdl:.2f} MDL</b>  ·  <b>${usd:.3f}</b>"


def _watt(v) -> str:
    return f"{v:.1f} W" if v is not None else "n/a"


def _report_text(title: str, window_h: float, cfg: dict, buckets: dict) -> str:
    mdl_kwh = float(cfg["mdl_per_kwh"])
    mdl_usd = float(cfg["mdl_per_usd"])
    a = _aggregate(buckets, window_h)
    label = _G.get("source_label", "?")
    host = _G.get("host_label") or "host"

    if a["n"] == 0:
        return (
            f"⚡ <b>Power Report — {title}</b>\n"
            f"Host: <b>{host}</b>\n\n"
            f"No samples collected yet for this window.\n"
            f"Source: <code>{label}</code>"
        )

    lines = [
        f"⚡ <b>Power Report — {title}</b>",
        f"Host: <b>{host}</b>  ·  Source: <code>{label}</code>",
        "",
        f"🔋 Energy: <b>{a['kwh']:.3f} kWh</b>",
        f"💵 Cost: {_money(a['kwh'], mdl_kwh, mdl_usd)}",
        f"    <i>@ {mdl_kwh:.2f} MDL/kWh · {mdl_usd:.2f} MDL/USD</i>",
        "",
        f"📊 Power: avg <b>{a['avg_w']:.1f} W</b>  ·  "
        f"min <b>{_watt(a['wmin'])}</b>  ·  max <b>{_watt(a['wmax'])}</b>",
        f"🕒 {a['n']} samples over {a['dur_h']:.1f} h "
        f"({a['coverage'] * 100:.0f}% coverage)",
    ]

    if window_h >= 168:  # weekly extras
        per_day = _per_day(buckets, 7, mdl_kwh)
        lines += ["", "<b>Per day</b>"]
        for lbl, kwh, mdl in per_day:
            lines.append(f"  <code>{lbl}</code>  {kwh:.3f} kWh  ·  {mdl:.2f} MDL")
        # extrapolate a monthly bill from the observed daily average
        daily_kwh = a["kwh"] / max(a["dur_h"] / 24.0, 1e-9)
        month_kwh = daily_kwh * 30.0
        lines += [
            "",
            f"📅 Est. monthly (30 d @ this rate): "
            f"<b>{month_kwh:.1f} kWh</b> → {_money(month_kwh, mdl_kwh, mdl_usd)}",
        ]

    if float(cfg.get("overhead_watts", 0.0)) > 0:
        lines.append(f"\n<i>Includes +{float(cfg['overhead_watts']):.1f} W flat overhead.</i>")

    return "\n".join(lines)


# ── broadcasting ─────────────────────────────────────────────────────────────

async def _broadcast(bot, text: str) -> None:
    prefix = f"[{_G.get('host_label')}] " if _G.get("host_label") else ""
    for uid in _G.get("allowed", ()):  # allowed user ids
        try:
            await bot.send_message(uid, text=prefix + text, parse_mode=ParseMode.HTML)
        except Exception as exc:
            logger.error("power_report: send to %s failed: %s", uid, exc)


# ── jobs ─────────────────────────────────────────────────────────────────────

def _do_sample() -> None:
    """Poll the energy counter once and fold it into the current bucket."""
    src = _G.get("source")
    if src is None:
        return
    cfg = _G["cfg"]
    try:
        r = src.sample()
    except OSError as exc:  # sysfs vanished (unmount) — degrade to estimate
        logger.warning("power_report: sensor read failed (%s); switching to estimate", exc)
        _G["source"] = _EstimateSource(float(cfg.get("cpu_tdp_watts", 15.0)))
        _G["source_label"] = _G["source"].label
        return
    if r is None:
        return
    watts, delta_uj, dt = r

    if dt > float(cfg.get("gap_reset_seconds", 120)):
        # Long gap (restart / suspend): counter delta may span multiple wraps
        # and the averaged watts would be meaningless — drop this one sample.
        logger.debug("power_report: %.0fs gap — re-baselining", dt)
        return

    overhead = float(cfg.get("overhead_watts", 0.0))
    if overhead > 0:
        watts += overhead
        delta_uj += overhead * dt * 1_000_000.0

    _G["last_w"] = watts
    buckets = _G["buckets"]
    _record(buckets, watts, delta_uj, dt)


def _maybe_flush(force: bool = False) -> None:
    cfg = _G["cfg"]
    now = time.monotonic()
    due = now - _G.get("last_flush", 0.0) >= float(cfg.get("flush_interval_seconds", 300))
    if force or due:
        _trim(_G["buckets"], int(cfg.get("retention_days", 9)))
        _save_store(cfg["history_path"], _G["buckets"], _G.get("source_label", ""))
        _G["last_flush"] = now


async def _run_sample(context) -> None:
    import asyncio
    try:
        await asyncio.to_thread(_do_sample)
        _maybe_flush()
    except Exception:
        logger.exception("power_report: sample job failed")


async def _run_daily(context) -> None:
    _maybe_flush(force=True)
    text = _report_text("Daily (last 24 h)", 24, _G["cfg"], _G["buckets"])
    await _broadcast(context.bot, text)


async def _run_weekly(context) -> None:
    _maybe_flush(force=True)
    text = _report_text("Weekly (last 7 days)", 168, _G["cfg"], _G["buckets"])
    await _broadcast(context.bot, text)


# ── interactive menu ─────────────────────────────────────────────────────────

async def _handle_action(query, parts, ctx: "PluginContext") -> None:
    sub = parts[1] if len(parts) > 1 else "menu"
    cfg = _G["cfg"]

    if sub == "test":
        await query.edit_message_text("📤 Sending a test daily report…")
        _maybe_flush(force=True)
        text = _report_text("Daily (last 24 h)", 24, cfg, _G["buckets"])
        await _broadcast(ctx.app.bot, text)
        # fall through to redraw the menu below

    _maybe_flush(force=True)
    day = _aggregate(_G["buckets"], 24)
    week = _aggregate(_G["buckets"], 168)
    mdl_kwh = float(cfg["mdl_per_kwh"])
    mdl_usd = float(cfg["mdl_per_usd"])
    last_w = _G.get("last_w")

    daily_sched = cfg.get("daily_schedule")
    weekly_sched = cfg.get("weekly_schedule")
    tzl = timez.tz_label()

    lines = [
        "⚡ <b>Power &amp; Electricity</b>",
        f"Source: <code>{_G.get('source_label', '?')}</code>",
        f"Now: <b>{_watt(last_w)}</b>" if last_w is not None else "Now: <i>warming up…</i>",
        "",
        f"<b>Last 24 h</b> — {day['kwh']:.3f} kWh · {_money(day['kwh'], mdl_kwh, mdl_usd)}",
        f"   min {_watt(day['wmin'])} · avg {day['avg_w']:.1f} W · max {_watt(day['wmax'])}",
        f"<b>Last 7 d</b> — {week['kwh']:.3f} kWh · {_money(week['kwh'], mdl_kwh, mdl_usd)}",
        f"   min {_watt(week['wmin'])} · avg {week['avg_w']:.1f} W · max {_watt(week['wmax'])}",
        "",
        f"💡 Tariff: <b>{mdl_kwh:.2f} MDL/kWh</b> (FX {mdl_usd:.2f} MDL/USD)",
        f"🗓 Daily: <code>{daily_sched or 'off'}</code> · "
        f"Weekly: <code>{weekly_sched or 'off'}</code> ({tzl})",
    ]

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Send report now", callback_data=_CB_TEST)],
            [
                InlineKeyboardButton("🔄 Refresh", callback_data=_CB_MENU),
                InlineKeyboardButton("◀️ Plugins", callback_data=_CB_PLUGINS),
            ],
        ]),
    )


# ── registration ─────────────────────────────────────────────────────────────

def register(ctx: "PluginContext") -> None:
    cfg = {**META.default_config, **dict(ctx.plugin_cfg)}

    source = _discover_source(cfg)
    buckets = _load_store(cfg["history_path"])

    _G.clear()
    _G.update({
        "cfg": cfg,
        "source": source,
        "source_label": source.label,
        "buckets": buckets,
        "allowed": set(ctx.cfg.allowed_users),
        "host_label": ctx.host_label,
        "last_flush": 0.0,
        "last_w": None,
    })

    ctx.actions.register("p.power_report", _handle_action)
    ctx.buttons.add("⚡ Power", _CB_MENU, sort_key=40)

    interval = int(cfg.get("sample_interval_seconds", 20))
    ctx.scheduler.every(interval, _run_sample, "power_report.sample")

    if cfg.get("daily_schedule"):
        ctx.scheduler.cron(str(cfg["daily_schedule"]), _run_daily, "power_report.daily")
    if cfg.get("weekly_schedule"):
        ctx.scheduler.cron(str(cfg["weekly_schedule"]), _run_weekly, "power_report.weekly")

    logger.info(
        "power_report: source=%s, sample=%ds, daily=%s, weekly=%s, tariff=%.2f MDL/kWh",
        source.label, interval, cfg.get("daily_schedule"),
        cfg.get("weekly_schedule"), float(cfg["mdl_per_kwh"]),
    )
