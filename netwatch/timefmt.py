"""Formatting helpers for durations and local timestamps.

Persistence uses UTC epoch seconds; everything here converts to local time
for display only.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional


def format_duration(seconds: Optional[float], precise: bool = False) -> str:
    """Compact duration like ``2d 4h``, ``3h 22m``, ``32m``, ``45s``."""
    if seconds is None:
        return "—"
    seconds = max(0, int(round(seconds)))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h" + (f" {minutes}m" if precise and minutes else "")
    if hours:
        return f"{hours}h {minutes}m" if minutes or not precise else f"{hours}h"
    if minutes:
        return f"{minutes}m" + (f" {secs}s" if precise and secs else "")
    return f"{secs}s"


def format_duration_long(seconds: Optional[float]) -> str:
    """Duration like ``149d 2h`` or ``0h 12m`` used in coverage lines."""
    if seconds is None:
        return "—"
    seconds = max(0, int(round(seconds)))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def _uses_12h() -> bool:
    try:
        return bool(time.strftime("%p", time.localtime(0)).strip())
    except ValueError:
        return False


_TWELVE_HOUR = _uses_12h()


def format_clock(ts: float, seconds: bool = False) -> str:
    dt = datetime.fromtimestamp(ts)
    if _TWELVE_HOUR:
        fmt = "%I:%M:%S %p" if seconds else "%I:%M %p"
        return dt.strftime(fmt).lstrip("0")
    return dt.strftime("%H:%M:%S" if seconds else "%H:%M")


def format_day(ts: float, now: Optional[float] = None) -> str:
    d = datetime.fromtimestamp(ts).date()
    today = datetime.fromtimestamp(now if now is not None else time.time()).date()
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    if d.year == today.year:
        return f"{d.strftime('%b')} {d.day}"
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def format_timestamp(ts: Optional[float], now: Optional[float] = None, seconds: bool = False) -> str:
    """``Today, 4:50 PM`` / ``Mar 27, 8:39 AM``."""
    if ts is None:
        return "now"
    return f"{format_day(ts, now)}, {format_clock(ts, seconds)}"


def format_iso_local(ts: Optional[float]) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def format_iso_utc(ts: Optional[float]) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def local_day_start(ts: float) -> float:
    """Epoch of local midnight for the day containing ``ts`` (DST safe)."""
    d = datetime.fromtimestamp(ts).date()
    return day_start_of(d)


def day_start_of(d: date) -> float:
    return datetime(d.year, d.month, d.day).timestamp()


def next_day_start(ts: float) -> float:
    d = datetime.fromtimestamp(ts).date() + timedelta(days=1)
    return day_start_of(d)


def format_axis_label(ts: float, span: float) -> str:
    dt = datetime.fromtimestamp(ts)
    if span <= 2 * 3600:
        return format_clock(ts)
    if span <= 2 * 86400:
        return format_clock(ts)
    return f"{dt.strftime('%b')} {dt.day}"


def format_percent(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "—"
    if value >= 100 or value <= 0:
        return f"{value:.0f}%"
    return f"{value:.{digits}f}%"
