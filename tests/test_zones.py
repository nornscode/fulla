"""A time said in another zone is scheduled in that zone, and the reply shows both."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from fulla import zones
from fulla.norns_api import MessageResult
from fulla.tools import clock
from fulla.tools import reminders as rem

TORONTO = ZoneInfo("America/Toronto")


@dataclass
class FakeClient:
    sent: list = field(default_factory=list)

    def send_message(self, agent, content, *, conversation_key=None):
        self.sent.append((agent, content, conversation_key))
        return MessageResult(run_id=220, status="accepted", output=None)


@pytest.fixture
def client(tmp_path):
    c = FakeClient()
    rem.init(client=c, data_dir=tmp_path, timezone="America/Toronto")
    return c


def test_eleven_pacific_is_scheduled_at_eleven_pacific(client, tmp_path):
    out = rem.schedule_reminder.handler("call Sam", "2026-10-16T11:00", "America/Vancouver")
    assert out == "Scheduled (run 220): call Sam at Fri Oct 16, 14:00 EDT (11:00 PDT)"
    assert client.sent[0][1] == "What: call Sam\nDue: 2026-10-16T14:00-04:00"
    assert json.loads((tmp_path / "reminders.json").read_text())["220"]["due_at"] == "2026-10-16T14:00-04:00"


def test_naive_time_without_tz_is_home_time(client):
    out = rem.schedule_reminder.handler("stretch", "2026-10-16T14:00")
    assert out == "Scheduled (run 220): stretch at Fri Oct 16, 14:00 EDT"
    assert client.sent[0][1].endswith("Due: 2026-10-16T14:00-04:00")


def test_offset_is_honoured_and_tz_only_labels_it(client):
    out = rem.schedule_reminder.handler("x", "2026-10-16T11:00-07:00", "America/Vancouver")
    assert out.endswith("at Fri Oct 16, 14:00 EDT (11:00 PDT)")
    out = rem.schedule_reminder.handler("y", "2026-10-16T18:00+00:00")
    assert out.endswith("at Fri Oct 16, 14:00 EDT")


def test_bad_zone_or_time_is_a_clear_error(client):
    assert "Unknown timezone 'PT'" in rem.schedule_reminder.handler("x", "2026-10-16T11:00", "PT")
    assert "ISO 8601" in rem.schedule_reminder.handler("x", "tomorrow")
    assert client.sent == []


def test_other_zone_shows_the_day_when_it_differs():
    dt = datetime(2026, 10, 17, 1, 30, tzinfo=TORONTO)
    assert zones.show(dt, "America/Vancouver") == "Sat Oct 17, 01:30 EDT (Fri Oct 16, 22:30 PDT)"
    assert zones.show(dt, "America/New_York") == "Sat Oct 17, 01:30 EDT"


def test_now_shows_the_named_zone_too(monkeypatch):
    clock.init(timezone="America/Toronto")
    monkeypatch.setattr(clock, "local_now", lambda: datetime(2026, 10, 16, 14, 0, tzinfo=TORONTO))
    assert clock.now.handler() == "2026-10-16T14:00:00-04:00 (Friday, America/Toronto)"
    assert clock.now.handler("America/Vancouver") == (
        "2026-10-16T14:00:00-04:00 (Friday, America/Toronto); "
        "in America/Vancouver: 2026-10-16T11:00:00-07:00 (Friday)"
    )
    assert "Unknown timezone" in clock.now.handler("Pacific")
