"""Fulla's schedule: her triggers in Norns, hers to make and move.

A trigger is Norns's timer: a cron, a message, an agent and a conversation
key. Fulla's used to be made by hand with `nornsctl` and re-made by hand when
the messenger changed. Now the owner says when ("brief at 7", "check in every
two hours on weekdays", "every Friday at 4 ask me about the week") and the
manager's tools (`tools/schedule.py`) create, change and remove them with
the Norns key she already holds. Nothing else does: no sync at start, nothing
in CI. Triggers are Norns data, and `nornsctl triggers list` is always the
truth.

What is fixed in code, not an argument: a trigger of hers starts the manager
in the standing conversation (`config.conversation_key`), since that is the
one conversation the channel relays. Times are the owner's: the tool takes
local times and days and this module builds the UTC cron Norns wants with
the zone's offset today, so the model never converts a zone. A fixed UTC
cron shifts by an hour at a clock change; `list_triggers` shows each one in
local time, so the owner sees it and says "move it back".

`TURNS` are the scheduled messages the manager's prompt knows what to do
with (the brief, the check-in, the nightly review, the shipping check, the
personal question);
any other message is one the owner wrote for the timer to say.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

# The scheduled turns the prompt describes, by trigger name: the message each
# must carry for the manager to recognise it, and the default time in docs/using.md.
TURNS = {
    "morning-brief": ("[scheduled] morning brief", "every day at 08:00"),
    "check-in": ("[scheduled] check-in", "every day at 09:00, 11:00, 13:00, 15:00, 17:00, 19:00"),
    "nightly-review": ("[scheduled] nightly review", "every day at 00:00"),
    "shipping-check": ("[scheduled] shipping check", "every day at 00:30"),
    "question": ("[scheduled] question", "tue, fri at 19:30"),
}

# What the manager's prompt looks for to know a message came from a timer and
# not from the owner: it then never asks a question (which would park the
# standing conversation and swallow the owner's next message as the answer),
# and writes to the owner or stays silent. Every timer's message carries it,
# the owner's own included.
MARKER = "[scheduled]"


def marked(message: str) -> str:
    """`message` as a timer must say it: behind the marker, once."""
    text = message.strip()
    if text.lower().startswith(MARKER):
        return MARKER + text[len(MARKER):]
    return f"{MARKER} {text}"


DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_NUMBER = {name: (i + 1) % 7 for i, name in enumerate(DAY_NAMES)}  # cron: 0 is Sunday
DAY_WORDS = {"": "*", "daily": "*", "every day": "*", "everyday": "*", "all": "*", "*": "*",
             "weekdays": "1-5", "weekday": "1-5", "weekends": "0,6", "weekend": "0,6"}


class BadSpec(ValueError):
    """A time or day that cannot become a schedule; `str(e)` says why, for the owner."""


_TIME_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*$", re.IGNORECASE)


def parse_time(text: str) -> str:
    """"7", "7am", "07:30", "7:30 pm", "noon", "midnight" to HH:MM, or BadSpec."""
    low = text.strip().lower()
    if low in ("noon", "midday"):
        return "12:00"
    if low == "midnight":
        return "00:00"
    m = _TIME_RE.match(low)
    if not m:
        raise BadSpec(f"I don't understand the time {text.strip()!r}.")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    suffix = (m.group(3) or "").replace(".", "")
    if suffix == "pm" and hour < 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise BadSpec(f"{text.strip()!r} is not a time of day.")
    return f"{hour:02d}:{minute:02d}"


def parse_times(text: str) -> tuple[str, ...]:
    """A list of local times, "7:30" or "9, 11 and 1pm", sorted; one minute for all."""
    parts = [p for p in re.split(r"\s*(?:,|;|\band\b|\n)\s*", text.strip()) if p]
    if not parts:
        raise BadSpec("Say at least one time.")
    times = sorted({parse_time(p) for p in parts})
    if len({t[3:] for t in times}) > 1:
        raise BadSpec("The times have to share a minute (7:00 and 9:00, not 7:30 and 9:00).")
    return tuple(times)


def parse_days(text: str) -> str:
    """"", "daily", "weekdays", "weekends" or day names ("mon, wed, fri") to a cron
    day-of-week field, or BadSpec."""
    low = text.strip().lower()
    if low in DAY_WORDS:
        return DAY_WORDS[low]
    numbers = set()
    for part in re.split(r"\s*(?:,|;|\band\b)\s*", low):
        key = part.strip()[:3]
        if key not in _DAY_NUMBER:
            raise BadSpec(f"I don't understand the days {text.strip()!r}.")
        numbers.add(_DAY_NUMBER[key])
    return "*" if len(numbers) == 7 else ",".join(str(n) for n in sorted(numbers))


def _field(text: str, top: int) -> list[int]:
    """The values a cron field names: lists, ranges, steps, `*`."""
    out = []
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            step = int(step_text)
        if part == "*":
            lo, hi = 0, top
        elif "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
        else:
            lo = hi = int(part)
        out.extend(range(lo, hi + 1, step))
    return sorted(set(out))


def _today(tz: str, now: datetime | None) -> datetime:
    zone = ZoneInfo(tz)
    return (now or datetime.now(zone)).astimezone(zone)


def cron_for(times: tuple[str, ...], days: str, tz: str, now: datetime | None = None) -> str:
    """A UTC cron for local `times` on `days` (a cron day-of-week field), with
    the zone's offset at `now`. A time that falls on the previous or next UTC
    day takes restricted days with it; times that would move them differently
    are refused."""
    today = _today(tz, now).date()
    zone = ZoneInfo(tz)
    minute = int(times[0][3:])
    hours, shifts = set(), set()
    for t in times:
        local = datetime(today.year, today.month, today.day, int(t[:2]), int(t[3:]), tzinfo=zone)
        utc = local.astimezone(ZoneInfo("UTC"))
        hours.add(utc.hour)
        shifts.add((utc.date() - today).days)
    hour_field = ",".join(str(h) for h in sorted(hours))
    if days == "*":
        return f"{minute} {hour_field} * * *"
    if len(shifts) > 1:
        raise BadSpec("Those times straddle midnight UTC on chosen days; pick times closer "
                      "together, or every day.")
    shift = shifts.pop()
    moved = sorted((d + shift) % 7 for d in _field(days, 6))
    return f"{minute} {hour_field} * * {','.join(map(str, moved))}"


def describe_days(days: str) -> str:
    if days == "*":
        return "every day"
    numbers = _field(days, 6)
    if numbers == [1, 2, 3, 4, 5]:
        return "weekdays"
    if numbers == [0, 6]:
        return "weekends"
    return ", ".join(DAY_NAMES[(n - 1) % 7].capitalize() for n in numbers)


def describe_cron(cron: str, tz: str, now: datetime | None = None) -> str:
    """A UTC cron in the owner's own terms ("weekdays at 09:00, 11:00"), or the
    cron itself marked UTC when it says something this module cannot (an
    alias, a day of the month, several minutes)."""
    fields = cron.split()
    try:
        if len(fields) != 5 or fields[2] != "*" or fields[3] != "*":
            raise BadSpec(cron)
        minutes = _field(fields[0], 59)
        if len(minutes) != 1:
            raise BadSpec(cron)
        today = _today(tz, now).date()
        zone = ZoneInfo(tz)
        times, shifts = [], set()
        for hour in _field(fields[1], 23):
            utc = datetime(today.year, today.month, today.day, hour, minutes[0], tzinfo=ZoneInfo("UTC"))
            local = utc.astimezone(zone)
            times.append(f"{local.hour:02d}:{local.minute:02d}")
            shifts.add((local.date() - today).days)
        when = ", ".join(sorted(times))
        if fields[4] == "*":
            return f"every day at {when}"
        if len(shifts) > 1:
            raise BadSpec(cron)
        shift = shifts.pop()
        days = ",".join(str(d) for d in sorted((n + shift) % 7 for n in _field(fields[4], 6)))
        return f"{describe_days(days)} at {when}"
    except (BadSpec, ValueError):
        return f"cron {cron} (UTC)"


@dataclass
class Trigger:
    """A trigger as Norns renders it; `norns_api.NornsApi.list_triggers` builds these."""
    id: int
    agent_id: int
    name: str
    cron: str
    message: str
    conversation_key: str | None
    enabled: bool
    last_fired_at: str | None = None


def render(triggers: list[Trigger], tz: str, conversation_key: str, now: datetime | None = None) -> str:
    if not triggers:
        return "No triggers."
    lines = []
    for t in sorted(triggers, key=lambda t: t.name):
        state = "" if t.enabled else " [off]"
        where = "" if t.conversation_key == conversation_key else " [not in your conversation; set it again to move it]"
        turn = " (a scheduled turn of yours)" if any(t.message == m for m, _ in TURNS.values()) else ""
        lines.append(f"{t.name}{state}: {describe_cron(t.cron, tz, now)}{where}")
        lines.append(f"    says: {t.message}{turn}")
        if t.last_fired_at:
            lines.append(f"    last ran {t.last_fired_at[:16].replace('T', ' ')} UTC")
    return "\n".join(lines)


def valid_name(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", name))
