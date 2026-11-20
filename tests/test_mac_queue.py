"""Queueing a Mac task starts a run; the index says which runs are Fulla's."""

from dataclasses import dataclass, field

import httpx

from fulla.norns_api import MessageResult, Run
from fulla.tools import clock
from fulla.tools import mac_queue as mq


@dataclass
class FakeClient:
    sent: list = field(default_factory=list)
    statuses: dict = field(default_factory=dict)
    replies: list = field(default_factory=list)
    next_run: int = 300

    def reply(self, run_id, answer):
        self.replies.append((run_id, answer))

    def send_message(self, agent, content, *, conversation_key=None):
        self.sent.append((agent, content, conversation_key))
        self.next_run += 1
        return MessageResult(run_id=self.next_run, status="accepted", output=None)

    def get_run(self, run_id):
        if run_id not in self.statuses:
            raise RuntimeError("gone")
        return Run(run_id=run_id, status=self.statuses[run_id], output=None, agent_id=3,
                   trigger_type="message", inserted_at="2026-10-16T05:00:00Z")


def setup(tmp_path):
    clock.init(timezone="America/Toronto")
    client = FakeClient()
    mq.init(client=client, data_dir=tmp_path)
    return client


def test_queue_starts_a_mac_task_run_and_records_it(tmp_path):
    client = setup(tmp_path)
    out = mq.queue_for_mac.handler("add 'buy flour' to my Things inbox")
    assert out == "Queued for the Mac (run 301): add 'buy flour' to my Things inbox"
    agent, content, key = client.sent[0]
    assert agent == "fulla-mac-task"
    assert content.startswith("Task: add 'buy flour' to my Things inbox\nQueued: 20")
    assert key.startswith("mac:add-buy-flour-to-my-things-inbox:")
    assert mq.known(301) and not mq.known(302)


def test_list_prunes_finished_tasks(tmp_path):
    client = setup(tmp_path)
    mq.queue_for_mac.handler("first")
    mq.queue_for_mac.handler("second")
    mq.queue_for_mac.handler("third")
    client.statuses = {301: "completed", 302: "running"}  # 303: unknown to the client
    out = mq.list_mac_queue.handler()
    assert "run 301" not in out
    assert "run 302: second (queued" in out
    assert "run 303: third (queued" in out and "status unknown" in out
    assert not mq.known(301) and mq.known(302) and mq.known(303)
    client.statuses = {302: "completed", 303: "failed"}
    assert mq.list_mac_queue.handler() == "Nothing queued for the Mac."


def test_a_task_whose_run_norns_does_not_have_is_said_lost_once_then_forgotten(tmp_path):
    client = setup(tmp_path)
    mq.queue_for_mac.handler("first")
    request = httpx.Request("GET", "http://norns/api/v1/runs/301")
    gone = httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))
    client.get_run = lambda run_id: (_ for _ in ()).throw(gone)
    out = mq.list_mac_queue.handler()
    assert "run 301: first" in out and "LOST" in out and not mq.known(301)
    assert mq.list_mac_queue.handler() == "Nothing queued for the Mac."


def test_drop_answers_the_parked_run_and_forgets_it(tmp_path):
    client = setup(tmp_path)
    mq.queue_for_mac.handler("text Alex I'm late")
    assert mq.drop_mac_task.handler(301) == "Dropped: text Alex I'm late"
    assert client.replies == [(301, "drop")] and not mq.known(301)
    assert mq.drop_mac_task.handler(301) == "No queued Mac task with run id 301."
