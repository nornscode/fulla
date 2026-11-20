"""The time, in the owner's timezone. Models are bad at knowing what day it is."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fulla import zones
from fulla.tooldef import tool

_tz = "UTC"


def init(*, timezone: str):
    global _tz
    _tz = timezone


def local_now() -> datetime:
    return datetime.now(ZoneInfo(_tz))


@tool
def now(tz: str = "") -> str:
    """The current date and time in the owner's timezone, ISO 8601 with weekday. When the
    owner talks about another zone ("PT", "Eastern", "London time"), pass it as `tz`, an
    IANA name like America/Vancouver, to see the time there too."""
    dt = local_now()
    out = f"{dt.isoformat(timespec='seconds')} ({dt.strftime('%A')}, {_tz})"
    if not tz:
        return out
    try:
        there = dt.astimezone(zones.zone(tz))
    except ValueError as e:
        return str(e)
    return f"{out}; in {tz}: {there.isoformat(timespec='seconds')} ({there.strftime('%A')})"
