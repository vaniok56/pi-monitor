"""Singleton timezone helpers. Call init() once at startup from main.py."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_tz: ZoneInfo = ZoneInfo("UTC")
_tz_name: str = "UTC"


def init(tz: ZoneInfo, name: str) -> None:
    global _tz, _tz_name
    _tz = tz
    _tz_name = name


def now() -> datetime:
    """Current time, aware, in configured timezone."""
    return datetime.now(_tz)


def utcnow() -> datetime:
    """Current UTC time, aware. Use for storage timestamps."""
    return datetime.now(timezone.utc)


def fmt(dt: datetime, pat: str) -> str:
    """Convert dt to configured timezone, then strftime."""
    return dt.astimezone(_tz).strftime(pat)


def tz_label() -> str:
    """IANA timezone name, e.g. 'UTC' or 'Europe/Berlin'."""
    return _tz_name


def next_cron(expr: str) -> datetime:
    """Next occurrence of cron expr in configured timezone."""
    from croniter import croniter
    base = now()
    return croniter(expr, base).get_next(datetime)


def next_daily(hh_mm: str) -> datetime:
    """Next occurrence of HH:MM daily in configured timezone."""
    h, m = map(int, hh_mm.split(":"))
    base = now()
    nxt = base.replace(hour=h, minute=m, second=0, microsecond=0)
    if nxt <= base:
        nxt += timedelta(days=1)
    return nxt
