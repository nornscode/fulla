"""A reminder is a run; the index says which are open, and for how long."""

from dataclasses import dataclass, field

import httpx

from fulla.norns_api import MessageResult, Run
from fulla.tools import manager_tools
from fulla.tools import reminders as rm


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://norns/api/v1/runs/1")
    return httpx.HTTPStatusError(str(code), request=request, response=httpx.Response(code, request=request))


@dataclass
class FakeClient:
    statuses: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)  # run id -> HTTP status Norns answers with
    cancelled: list = field(default_factory=list)
    next_run: int = 200

    def send_message(self, agent, content, *, conversation_key=None):
        self.next_run += 1
        return MessageResult(run_id=self.next_run, status="accepted", output=None)

    def get_run(self, run_id):
        if run_id in self.errors:
            raise _status_error(self.errors[run_id])
        return Run(run_id=run_id, status=self.statuses.get(run_id, "running"), output=None, agent_id=3,
                   trigger_type="message", inserted_at="2026-10-16T05:00:00Z")

    def cancel_run(self, run_id):
        if self.errors.get(run_id) == 500:
            raise _status_error(500)
        self.cancelled.append(run_id)
        return self.errors.get(run_id) != 404


def setup(tmp_path):
    client = FakeClient()
    rm.init(client=client, api=client, data_dir=tmp_path, timezone="America/Vancouver")
    return client


def test_a_reminder_whose_run_norns_does_not_have_is_said_lost_once_then_forgotten(tmp_path):
    """Run 220 was started on a tenant Fulla has since left. Norns answered 404
    for it, the list kept it as "status unknown", and the morning brief asked
    about a finished set of slides every day for three weeks. It is not dropped without
    a word either: a reminder that will never fire is worth one line."""
    client = setup(tmp_path)
    rm.schedule_reminder.handler("Finish the slides", "2026-10-16T09:00")
    rm.schedule_reminder.handler("Call the dentist", "2026-11-09T09:00")
    client.errors = {201: 404}
    out = rm.list_reminders.handler()
    assert "run 201: Finish the slides" in out and "LOST" in out.splitlines()[0]
    assert "run 202: Call the dentist" in out and "LOST" not in out.splitlines()[1]
    assert not rm.known(201) and rm.known(202)
    again = rm.list_reminders.handler()
    assert "Finish the slides" not in again and "run 202: Call the dentist" in again


def test_a_reminder_norns_cannot_answer_for_right_now_stays(tmp_path):
    client = setup(tmp_path)
    rm.schedule_reminder.handler("Call the dentist", "2026-11-09T09:00")
    client.errors = {201: 500}
    assert "run 201: Call the dentist" in rm.list_reminders.handler()
    assert "status unknown" in rm.list_reminders.handler() and rm.known(201)


def test_drop_cancels_the_run_and_forgets_it(tmp_path):
    client = setup(tmp_path)
    rm.schedule_reminder.handler("Finish the slides", "2026-10-16T09:00")
    assert rm.drop_reminder.handler(201) == "Closed: Finish the slides"
    assert client.cancelled == [201] and not rm.known(201)
    assert rm.drop_reminder.handler(201) == "No open reminder with run id 201."
    assert rm.list_reminders.handler() == "No open reminders."


def test_drop_forgets_a_run_norns_no_longer_has(tmp_path):
    client = setup(tmp_path)
    rm.schedule_reminder.handler("Finish the slides", "2026-10-16T09:00")
    client.errors = {201: 404}
    assert rm.drop_reminder.handler(201) == "Closed: Finish the slides"
    assert not rm.known(201)


def test_drop_keeps_a_reminder_it_could_not_cancel(tmp_path):
    client = setup(tmp_path)
    rm.schedule_reminder.handler("Call the dentist", "2026-11-09T09:00")
    client.errors = {201: 500}
    assert rm.drop_reminder.handler(201).startswith("Could not close run 201")
    assert rm.known(201)


def test_the_manager_holds_drop_reminder():
    assert "drop_reminder" in {t.name for t in manager_tools}
