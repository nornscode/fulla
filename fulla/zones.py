"""Times given in a zone other than home. The model must not convert between
zones itself: told "11am PT" with home in Toronto, it scheduled 12:00 Eastern,
which is 9am PT (run 220). So tools take the owner's zone by name, do the
arithmetic, and echo the result in both zones so a slip shows."""

from __future__ import annotations

from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def zone(name: str) -> ZoneInfo:
    """An IANA zone by name. A name that is a directory of zones ("Pacific",
    "America") raises IsADirectoryError from the tzdata package on Linux, not
    ZoneInfoNotFoundError, so OSError is caught too."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(
            f"Unknown timezone {name!r}: use an IANA name like America/Vancouver, "
            "America/New_York or Europe/London"
        ) from None


def parse(due_at: str, tz: str, home: tzinfo | None) -> datetime:
    """`due_at` as a moment, shown in `home`. A naive time is read in `tz` if
    given, else in `home`; an offset is honoured as given. `home=None` is the
    machine's own clock, and a naive time with no `tz` stays naive there."""
    where = zone(tz) if tz else None
    try:
        due = datetime.fromisoformat(due_at)
    except ValueError:
        raise ValueError(f"due_at must be ISO 8601, got {due_at!r}") from None
    if due.tzinfo is None:
        if where is None and home is None:
            return due
        due = due.replace(tzinfo=where or home)
    return due.astimezone(home)


def show(dt: datetime, tz: str = "") -> str:
    """`dt` in its own zone, then in `tz` too when that reads differently:
    "Fri Oct 16, 14:00 EDT (11:00 PDT)"."""
    here = dt.strftime("%a %b %-d, %H:%M %Z").rstrip()
    if not tz or dt.tzinfo is None:
        return here
    there = dt.astimezone(zone(tz))
    if there.strftime("%H:%M %Z") == dt.strftime("%H:%M %Z"):
        return here
    fmt = "%H:%M %Z" if there.date() == dt.date() else "%a %b %-d, %H:%M %Z"
    return f"{here} ({there.strftime(fmt)})"
