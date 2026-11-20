"""The Mac worker answers every parked Mac task once when it connects."""

from dataclasses import dataclass, field

from fulla.mac.nudge import ANSWER, nudge
from fulla.norns_api import Agent, Run, WaitingFor

MAC_TASK = 3


def run(run_id, status, agent_id=MAC_TASK, waiting=True):
    return Run(run_id=run_id, status=status, output=None, agent_id=agent_id, trigger_type="message",
               inserted_at="2026-10-16T05:00:00Z",
               waiting_for=WaitingFor(question="Waiting for the Mac.", tool_call_id="t") if waiting else None)


@dataclass
class FakeApi:
    catalogs: list = field(default_factory=list)  # successive answers to tool_names()
    runs: list = field(default_factory=list)
    replies: list = field(default_factory=list)
    agent_calls: int = 0
    listed: list = field(default_factory=list)  # the filters each list_runs asked for

    def tool_names(self):
        return self.catalogs.pop(0) if len(self.catalogs) > 1 else self.catalogs[0]

    def get_agent(self, name):
        self.agent_calls += 1
        return Agent(id={"fulla-mac-task": MAC_TASK, "fulla": 1}[name], name=name)

    def list_runs(self, limit=50, status=None, agent_id=None):
        self.listed.append({"status": status, "agent_id": agent_id})
        return self.runs

    def reply(self, run_id, answer):
        if run_id == 66:
            raise RuntimeError("no longer waiting")
        self.replies.append((run_id, answer))


def test_answers_each_parked_mac_task_once_tools_are_visible():
    slept = []
    api = FakeApi(
        catalogs=[["now"], ["now"], ["now", "mac_things_add", "mac_notify"]],
        runs=[run(60, "waiting"), run(61, "waiting", agent_id=1), run(62, "completed"),
              run(63, "waiting", waiting=False), run(64, "waiting"), run(66, "waiting")],
    )
    n = nudge(api, ["mac_things_add", "mac_notify"], attempts=5, delay=2.0, sleep=slept.append)
    assert n == 2
    assert api.replies == [(60, ANSWER), (64, ANSWER)]
    assert slept == [2.0, 2.0]  # waited for the catalog, then stopped waiting
    # Norns is asked for the parked Mac tasks, not the newest N runs.
    assert api.listed == [{"status": "waiting", "agent_id": MAC_TASK}]


def test_gives_up_quietly_when_tools_never_appear():
    api = FakeApi(catalogs=[["now"]], runs=[run(60, "waiting")])
    assert nudge(api, ["mac_things_add"], attempts=3, delay=0, sleep=lambda s: None) == 0
    assert api.replies == [] and api.agent_calls == 0
