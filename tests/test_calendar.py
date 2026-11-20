from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from fulla.calendar import Calendars, events_from_ics, parse_feeds

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VEVENT
UID:1
SUMMARY:Dentist
DTSTART:20261020T140000Z
DTEND:20261020T150000Z
LOCATION:Queen St
END:VEVENT
BEGIN:VEVENT
UID:2
SUMMARY:Gym
DTSTART;TZID=America/Toronto:20261019T070000
DTEND;TZID=America/Toronto:20261019T080000
RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR
END:VEVENT
BEGIN:VEVENT
UID:3
SUMMARY:Mum's birthday
DTSTART;VALUE=DATE:20261022
DTEND;VALUE=DATE:20261023
END:VEVENT
END:VCALENDAR
"""

TZ = ZoneInfo("America/Toronto")


def test_parse_feeds():
    assert parse_feeds("") == {}
    assert parse_feeds("Personal=https://a/b.ics, Work=webcal://c/d.ics") == {
        "Personal": "https://a/b.ics", "Work": "webcal://c/d.ics"}
    with pytest.raises(ValueError):
        parse_feeds("nourl")


def test_recurrence_expanded_and_all_day_flagged():
    start = datetime(2026, 10, 19, tzinfo=TZ)
    end = datetime(2026, 10, 26, tzinfo=TZ)
    events = events_from_ics("Personal", ICS, start, end)
    titles = sorted(e.title for e in events)
    assert titles == ["Dentist", "Gym", "Gym", "Gym", "Mum's birthday"]
    birthday = next(e for e in events if e.title == "Mum's birthday")
    assert birthday.all_day
    assert not next(e for e in events if e.title == "Dentist").all_day


def test_render_groups_by_day_in_local_time(monkeypatch):
    cals = Calendars({"Personal": "https://example/feed.ics"}, "America/Toronto")
    monkeypatch.setattr(cals, "_fetch", lambda name: ICS)
    monkeypatch.setattr(cals, "_now", lambda: datetime(2026, 10, 19, 6, 0, tzinfo=TZ))
    out = cals.render(4)
    assert out.splitlines()[0] == "Mon Oct 19 (today)"
    assert "  07:00-08:00  Gym  [Personal]" in out
    assert "  10:00-11:00  Dentist @ Queen St  [Personal]" in out  # 14:00Z is 10:00 EDT
    assert "  all day  Mum's birthday  [Personal]" in out


def test_no_feeds():
    assert Calendars({}, "UTC").render(7) == "No calendars connected."


def test_broken_feed_is_reported_not_fatal(monkeypatch):
    cals = Calendars({"Work": "https://example/x.ics"}, "UTC")

    def boom(name):
        raise RuntimeError("403")

    monkeypatch.setattr(cals, "_fetch", boom)
    assert "couldn't read this calendar" in cals.render(7)


def test_connect_persists_and_validates(tmp_path, monkeypatch):
    path = tmp_path / "calendars.json"
    cals = Calendars({"Seed": "https://seed/x.ics"}, "America/Toronto", path=path)

    class Resp:
        content = ICS

        def raise_for_status(self):
            pass

    monkeypatch.setattr(cals._http, "get", lambda url: Resp())
    monkeypatch.setattr(cals, "_now", lambda: datetime(2026, 10, 19, 6, 0, tzinfo=TZ))
    assert cals.add("Personal", "webcal://example/feed.ics") == 15  # 13 gym sessions + dentist + birthday in 30 days
    assert set(cals.feeds) == {"Seed", "Personal"}

    again = Calendars({"Seed": "https://seed/x.ics"}, "America/Toronto", path=path)
    assert again.feeds["Personal"] == "webcal://example/feed.ics"
    assert again.remove("Personal") is True
    assert again.remove("Seed") is False  # seeds come from the environment, not the file
    assert "Personal" not in Calendars({}, "UTC", path=path).feeds


def test_connect_rejects_garbage(tmp_path, monkeypatch):
    cals = Calendars({}, "UTC", path=tmp_path / "c.json")

    class Resp:
        content = b"<html>not a calendar</html>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(cals._http, "get", lambda url: Resp())
    with pytest.raises(ValueError):
        cals.add("Bad", "https://example/page")
    assert cals.feeds == {}
