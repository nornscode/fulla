"""Reminders set from a friends' chat: a run of their own, indexed against
the run that set them."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fulla import runctx
from fulla.norns_api import MessageResult
from fulla.people import PeopleStore
from fulla.tools import group_reminders as gr


@dataclass
class FakeClient:
    messages: list = field(default_factory=list)
    cancelled: list = field(default_factory=list)

    def send_message(self, agent, content, *, conversation_key=None, wait=False, timeout=30, interject=False):
        self.messages.append((agent, content, conversation_key))
        return MessageResult(run_id=70, status="accepted", output=None)

    def cancel_run(self, run_id):
        self.cancelled.append(run_id)
        return True


def test_a_reminder_needs_to_know_the_run_it_was_set_from(tmp_path):
    client = FakeClient()
    gr.init(client=client, data_dir=tmp_path, timezone="America/Vancouver")
    assert gr.schedule_group_reminder.handler("leave", "2030-10-19T12:00").startswith("I can't tell which chat")
    assert client.messages == [] and gr.known(70) is None


def test_a_reminder_is_a_run_of_the_group_reminder_agent_indexed_by_its_parent(tmp_path):
    client = FakeClient()
    gr.init(client=client, data_dir=tmp_path, timezone="America/Vancouver")
    token = runctx.set_current_run(9)
    try:
        out = gr.schedule_group_reminder.handler("leave", "2030-10-19T12:00")
    finally:
        runctx.reset_current_run(token)
    assert out.startswith("Set (run 70): leave at ") and out.endswith("I'll post it here.")
    agent, content, key = client.messages[0]
    assert agent == "fulla-group-reminder" and content.startswith("What: leave\nDue: 2030-10-19T12:00")
    assert key.startswith("group-reminder:leave:")
    assert gr.known(70) == {"what": "leave", "due_at": "2030-10-19T12:00-07:00", "parent": 9, "asked_by": None}
    assert gr.drop_group_reminder.handler(70) == "Dropped: leave."
    assert client.cancelled == [70] and gr.known(70) is None
    assert gr.drop_group_reminder.handler(70).startswith("No reminder")


def test_the_run_context_is_set_and_cleared_around_a_tool_task():
    assert runctx.current_run_id() is None
    token = runctx.set_current_run("12")
    assert runctx.current_run_id() == 12
    runctx.reset_current_run(token)
    assert runctx.current_run_id() is None


def test_a_reminder_tags_the_one_speaker_or_the_named_one_of_several(tmp_path):
    client = FakeClient()
    gr.init(client=client, data_dir=tmp_path, timezone="America/Vancouver")
    sam = {"name": "Sam", "transport": "signal", "user_id": "+16045550111"}
    dana = {"name": "Dana", "transport": "signal", "user_id": "+16045550122"}
    speakers = {9: [sam], 10: [dana, sam]}
    runctx.speakers_from(lambda run_id: speakers.get(run_id, []))
    try:
        token = runctx.set_current_run(9)
        out = gr.schedule_group_reminder.handler("stretch", "2030-10-19T12:00")
        runctx.reset_current_run(token)
        assert out.endswith("I'll post it here, tagging Sam.") and gr.known(70)["asked_by"] == sam
        token = runctx.set_current_run(10)
        # Two people spoke to this run: no guess without a name, and only a name among them counts.
        assert gr.schedule_group_reminder.handler("stretch", "2030-10-19T12:00").endswith("I'll post it here.")
        assert gr.known(70)["asked_by"] is None
        assert gr.schedule_group_reminder.handler("stretch", "2030-10-19T12:00", asked_by="dana").endswith("tagging Dana.")
        assert gr.known(70)["asked_by"] == dana
        assert gr.schedule_group_reminder.handler("stretch", "2030-10-19T12:00", asked_by="Kim").endswith("here.")
        assert gr.known(70)["asked_by"] is None
        runctx.reset_current_run(token)
    finally:
        runctx.speakers_from(None)
    assert runctx.current_speakers() == []  # no run in context, nobody


def test_a_relative_reminder_needs_no_arithmetic_and_a_stale_clock_is_refused(tmp_path):
    client = FakeClient()
    gr.init(client=client, data_dir=tmp_path, timezone="America/Vancouver")
    token = runctx.set_current_run(9)
    try:
        assert gr.schedule_group_reminder.handler("crack a beer", in_minutes=3).startswith("Set (run 70): crack a beer at ")
        due = datetime.fromisoformat(gr.known(70)["due_at"])
        assert timedelta(minutes=2) <= due - datetime.now(due.tzinfo) <= timedelta(minutes=3)
        # Live, the model added three minutes to a clock it had read five minutes before.
        stale = (datetime.now(ZoneInfo("America/Vancouver")) - timedelta(minutes=2)).isoformat(timespec="minutes")
        out = gr.schedule_group_reminder.handler("beer", stale)
        assert "has already passed" in out and "Call `now`" in out
        assert gr.schedule_group_reminder.handler("beer").startswith("Say when:")
        assert len(client.messages) == 1
    finally:
        runctx.reset_current_run(token)


def test_a_reminder_for_someone_else_tags_them_if_they_have_spoken_in_the_group(tmp_path):
    client = FakeClient()
    people = PeopleStore(tmp_path / "people.json")
    people.remember_group("signal:group:abc", "Friends")
    people.allow_group("signal:group:abc")
    people.allow_person("Pat Lee", {"signal": ["+16045550111", "u-pat"]})
    people.saw_in_group("signal:group:abc", "signal", "u-pat", "Pat Lee")
    people.saw_in_group("signal:group:abc", "signal", "+16045550122", "Kim")  # heard, not listed
    gr.init(client=client, data_dir=tmp_path, timezone="America/Vancouver", people=people)
    owner = {"name": "Robin", "transport": "signal", "user_id": "+16045550100"}
    runctx.speakers_from(lambda run_id: [owner])
    runctx.origins_from({9: "signal:group:abc", 10: "signal:+16045550100"}.get)
    try:
        token = runctx.set_current_run(9)
        out = gr.schedule_group_reminder.handler("crack a beer", in_minutes=5, asked_by="pat lee")
        assert out.endswith("tagging Pat Lee.")
        assert gr.known(70)["asked_by"] == {"name": "Pat Lee", "transport": "signal", "user_id": "u-pat"}
        assert gr.schedule_group_reminder.handler("x", in_minutes=5, asked_by="Kim").endswith("tagging Kim.")
        assert gr.schedule_group_reminder.handler("x", in_minutes=5, asked_by="Zed").endswith("here.")  # nobody
        assert gr.schedule_group_reminder.handler("x", in_minutes=5).endswith("tagging Robin.")  # the asker still
        runctx.reset_current_run(token)
        token = runctx.set_current_run(10)  # a one-to-one chat: no group, no members
        assert gr.schedule_group_reminder.handler("x", in_minutes=5, asked_by="Pat Lee").endswith("here.")
        runctx.reset_current_run(token)
    finally:
        runctx.speakers_from(None)
        runctx.origins_from(None)
