"""Read-only calendars from iCal feeds: Google's secret iCal address, an iCloud
published calendar, anything that serves .ics. No OAuth, nothing to refresh.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events

logger = logging.getLogger("fulla.calendar")

CACHE_SECONDS = 600


@dataclass
class Event:
    calendar: str
    title: str
    start: datetime | date
    end: datetime | date | None
    location: str = ""

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)


def parse_feeds(spec: str) -> dict[str, str]:
    """'Name=url,Other=url' -> {name: url}. Empty spec -> {}."""
    feeds = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, url = part.partition("=")
        if not url:
            raise ValueError(f"calendar entry needs Name=url, got {part!r}")
        feeds[name.strip()] = url.strip()
    return feeds


def events_from_ics(name: str, ics: bytes, start: datetime, end: datetime) -> list[Event]:
    cal = icalendar.Calendar.from_ical(ics)
    out = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        dtstart = ev.get("DTSTART")
        if dtstart is None:
            continue
        dtend = ev.get("DTEND")
        out.append(
            Event(
                calendar=name,
                title=str(ev.get("SUMMARY", "(untitled)")),
                start=dtstart.dt,
                end=dtend.dt if dtend is not None else None,
                location=str(ev.get("LOCATION", "") or ""),
            )
        )
    return out


class Calendars:
    """Feeds come from two places: the environment (seed, read-only) and a JSON
    file the owner fills from chat via connect_calendar. The file wins on a
    name clash."""

    def __init__(self, feeds: dict[str, str], timezone: str, path: Path | None = None):
        self._seed = dict(feeds)
        self._stored: dict[str, str] = {}
        self.path = path
        if path and path.exists():
            self._stored = json.loads(path.read_text() or "{}")
        self.tz = ZoneInfo(timezone)
        self._http = httpx.Client(timeout=30, follow_redirects=True)
        self._cache: dict[str, tuple[float, bytes]] = {}

    @property
    def feeds(self) -> dict[str, str]:
        return {**self._seed, **self._stored}

    def _save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._stored, indent=2))
        tmp.replace(self.path)

    def add(self, name: str, url: str) -> int:
        """Store a feed after proving it parses. Returns how many events it holds
        in the next 30 days. Raises on a bad URL or non-iCal content."""
        name, url = name.strip(), url.strip()
        if not name or not url:
            raise ValueError("a calendar needs a name and a URL")
        resp = self._http.get(url.replace("webcal://", "https://"))
        resp.raise_for_status()
        now = self._now()
        events = events_from_ics(name, resp.content, now, now + timedelta(days=30))
        self._stored[name] = url
        self._cache[name] = (time.monotonic(), resp.content)
        self._save()
        return len(events)

    def remove(self, name: str) -> bool:
        if name in self._stored:
            del self._stored[name]
            self._cache.pop(name, None)
            self._save()
            return True
        return False

    def _fetch(self, name: str) -> bytes:
        url = self.feeds[name]
        cached = self._cache.get(name)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        resp = self._http.get(url.replace("webcal://", "https://"))
        resp.raise_for_status()
        self._cache[name] = (time.monotonic(), resp.content)
        return resp.content

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def upcoming(self, days: int) -> list[Event]:
        now = self._now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=days)
        events: list[Event] = []
        for name in self.feeds:
            try:
                events.extend(events_from_ics(name, self._fetch(name), start, end))
            except Exception as e:
                logger.warning(f"calendar {name!r} failed: {e}")
                events.append(Event(calendar=name, title=f"(couldn't read this calendar: {e})",
                                    start=start.date(), end=None))
        return sorted(events, key=lambda e: self._sort_key(e))

    def _sort_key(self, e: Event) -> datetime:
        if isinstance(e.start, datetime):
            return e.start.astimezone(self.tz)
        return datetime.combine(e.start, datetime.min.time(), tzinfo=self.tz)

    def render(self, days: int) -> str:
        if not self.feeds:
            return "No calendars connected."
        events = self.upcoming(days)
        if not events:
            return f"Nothing on the calendar in the next {days} days."
        by_day: dict[date, list[Event]] = {}
        for e in events:
            d = self._sort_key(e).date()
            by_day.setdefault(d, []).append(e)
        today = self._now().date()
        lines = []
        for d in sorted(by_day):
            label = d.strftime("%a %b %-d")
            if d == today:
                label += " (today)"
            elif d == today + timedelta(days=1):
                label += " (tomorrow)"
            lines.append(label)
            for e in by_day[d]:
                when = "all day" if e.all_day else self._span(e)
                where = f" @ {e.location}" if e.location else ""
                lines.append(f"  {when}  {e.title}{where}  [{e.calendar}]")
        return "\n".join(lines)

    def _span(self, e: Event) -> str:
        s = e.start.astimezone(self.tz).strftime("%H:%M")  # pyright: ignore[reportAttributeAccessIssue]
        if isinstance(e.end, datetime):
            return f"{s}-{e.end.astimezone(self.tz).strftime('%H:%M')}"
        return s
