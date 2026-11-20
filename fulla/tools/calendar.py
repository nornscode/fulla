"""What's on the calendar, and which calendars there are."""

# Module state is set by init() before any tool runs; it is None only before then.
# pyright: reportOptionalMemberAccess=false

from __future__ import annotations

from fulla.calendar import Calendars
from fulla.tooldef import tool

_calendars: Calendars | None = None


def init(*, calendars: Calendars):
    global _calendars
    _calendars = calendars


@tool
def calendar(days: int = 7) -> str:
    """Upcoming calendar events for the next `days` days (today included), grouped by day,
    in the owner's timezone. Read-only."""
    days = max(1, min(int(days), 60))
    return _calendars.render(days)


@tool(side_effect=True)
def connect_calendar(name: str, url: str) -> str:
    """Connect a calendar from an iCal feed URL (.ics or webcal://). `name` is a short label
    like "Personal" or "Work". The feed is fetched once to check it parses."""
    try:
        count = _calendars.add(name, url)
    except Exception as e:
        return f"Couldn't read that feed: {e}"
    return f"Connected '{name.strip()}': {count} event(s) in the next 30 days."


@tool(side_effect=True)
def disconnect_calendar(name: str) -> str:
    """Disconnect a calendar by name."""
    return f"Disconnected '{name}'." if _calendars.remove(name) else f"No calendar named '{name}'."


@tool
def list_calendars() -> str:
    """List connected calendars by name. URLs are never shown."""
    names = list(_calendars.feeds)
    return ", ".join(names) if names else "No calendars connected."
