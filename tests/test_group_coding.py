"""The "coding" entry of a group's catalogue: Bubbo's sandbox tools on the
group's allowlist, held to the group's own repositories and sandboxes in
the worker's LLM path, and a check-back that comes back into the group."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from norns import Norns

from fulla import runctx
from fulla.coding import (
    A_GROUPS,
    CODING_TOOLS,
    NOT_HERE,
    NOT_YOURS,
    REPO_NOT_HERE,
    CodingGuard,
    Sandboxes,
)
from fulla.config import load_config
from fulla.groups import CATALOGUE, GroupStore, group_agent_name
from fulla.people import PeopleStore
from fulla.tools import group_reminders as gr
from fulla.wake import ServingNorns
from fulla.worker import group_agent
from tests.test_channel import GROUP_REMINDER, SOCIAL, FakeClient, event, in_group, make_group

TESTING = "signal:group:abc"
OTHER = "signal:group:xyz"


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("FULLA_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FULLA_TELEGRAM_USER_ID", "7")
    monkeypatch.setenv("NORNS_API_KEY", "k")
    return load_config()


def stores(tmp_path):
    people = PeopleStore(tmp_path / "people.json")
    for address, name in ((TESTING, "Build Testing"), (OTHER, "Other")):
        people.remember_group(address, name)
        people.allow_group(address)
    store = GroupStore(tmp_path / "groups")
    testing, other = sorted(store.sync(people), key=lambda r: r.name)
    store.add_source(testing.id, "github_repo", "acme/widgets", "Widgets")
    store.add_source(other.id, "github_repo", "nornscode/fulla")
    store.set_tools(testing.id, ["coding"])
    store.set_tools(other.id, ["coding"])
    return store, store.get(testing.id), store.get(other.id)


def call(name, cid="c1", **arguments):
    return {"id": cid, "name": name, "arguments": arguments}


def asked(*calls):
    return {"status": "ok", "content": "", "finish_reason": "tool_call", "tool_calls": list(calls)}


def answered(cid, content):
    """The messages a turn carries after a call `cid` returned `content`."""
    return [{"role": "user", "content": "Fulla, fix the typo"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": cid, "name": CALLS[cid], "arguments": {}}]},
            {"role": "tool", "tool_call_id": cid, "content": content}]


CALLS = {"p1": "provision_environment", "p2": "provision_environment", "s1": "start_coding_agent"}


# --- the catalogue and the allowlist ------------------------------------------


def test_coding_is_in_the_catalogue(tmp_path):
    assert "coding" in CATALOGUE
    _store, testing, _ = stores(tmp_path)
    assert testing.tools == ["coding"]


def test_without_coding_a_group_holds_no_coding_tool(tmp_path, config):
    store, testing, _ = stores(tmp_path)
    record = store.set_tools(testing.id, ["web", "memory", "github"])
    agent = group_agent(config, record)
    assert not set(CODING_TOOLS) & set(agent.allowed_tools)
    assert "schedule_group_followup" not in agent.allowed_tools
    assert "provision_environment" not in agent.system_prompt


def test_with_coding_a_group_holds_the_coding_tools_and_is_told_how(tmp_path, config):
    _store, testing, _ = stores(tmp_path)
    agent = group_agent(config, testing)
    assert set(CODING_TOOLS) <= set(agent.allowed_tools)
    assert "schedule_group_followup" in agent.allowed_tools
    assert "wait" not in agent.allowed_tools
    # Served by Bubbo, never by this worker.
    assert not set(CODING_TOOLS) & {t.name for t in agent.tools}
    prompt = agent.system_prompt
    assert "acme/widgets" in prompt and "nornscode/fulla" not in prompt
    for step in ("provision_environment", "schedule_group_followup", "start_coding_agent", "get_coding_task",
                 "open_pull_request", "destroy_environment"):
        assert step in prompt


def test_coding_with_no_repository_says_so(tmp_path, config):
    store, testing, _ = stores(tmp_path)
    store.drop_source(testing.id, "widgets")
    prompt = group_agent(config, store.get(testing.id)).system_prompt
    assert "cannot open a coding sandbox here" in prompt and "`start_coding_agent`" not in prompt


# --- the guard: a group's repositories and its own sandboxes -------------------


def guard(tmp_path):
    store, testing, other = stores(tmp_path)
    return CodingGuard(store, Sandboxes(tmp_path / "sandboxes.json")), testing, other


def test_a_group_may_not_provision_a_repository_outside_its_sources(tmp_path):
    g, testing, _ = guard(tmp_path)
    for repo in ("nornscode/fulla", "nornscode/bubbo", "acme/other", ""):
        out = g.check(group_agent_name(testing), [], asked(call("provision_environment", repo=repo)))
        assert out["content"] == REPO_NOT_HERE and out["final_output"] == REPO_NOT_HERE
        assert "tool_calls" not in out and out["finish_reason"] == "stop"
    # One bad call refuses the whole step, the good one with it.
    out = g.check(group_agent_name(testing), [], asked(call("get_environment", "c0", environment_id="x"),
                                                       call("provision_environment", repo="nornscode/fulla")))
    assert "tool_calls" not in out


def test_a_group_may_provision_its_own_repository(tmp_path):
    g, testing, _ = guard(tmp_path)
    result = asked(call("provision_environment", repo="Acme/Widgets"))
    assert g.check(group_agent_name(testing), [], result) is result
    assert result["tool_calls"][0]["arguments"]["repo"] == "acme/widgets"
    by_label = asked(call("provision_environment", repo="Widgets", pull_request=12))
    assert g.check(group_agent_name(testing), [], by_label)["tool_calls"][0]["arguments"] == {
        "repo": "acme/widgets", "pull_request": 12}


def test_a_group_without_coding_on_and_any_other_agent_are_refused(tmp_path):
    g, testing, _ = guard(tmp_path)
    g.groups.set_tools(testing.id, ["github"])
    provision = asked(call("provision_environment", repo="acme/widgets"))
    assert g.check(group_agent_name(testing), [], provision)["content"] == NOT_HERE
    assert g.check("fulla-social", [], provision)["content"] == NOT_HERE
    assert g.check(None, [], provision)["content"] == NOT_HERE
    plain = {"status": "ok", "content": "hi", "finish_reason": "stop"}
    assert g.check(None, [], plain) is plain  # nothing to hold


def test_a_sandbox_and_its_jobs_are_the_groups_that_made_them(tmp_path):
    g, testing, other = guard(tmp_path)
    mine, theirs = group_agent_name(testing), group_agent_name(other)
    # Unknown to anyone yet: a group may not guess its way into one.
    assert g.check(mine, [], asked(call("get_environment", environment_id="env_1")))["content"] == NOT_YOURS
    # The provision's result, read from the group's own history, makes it the group's.
    history = answered("p1", '{"environment_id": "env_1", "state": "provisioning"}')
    start = asked(call("start_coding_agent", environment_id="env_1", task="fix the typo"))
    assert g.check(mine, history, start) is start
    history += [{"role": "assistant", "content": "", "tool_calls": [{"id": "s1", "name": "start_coding_agent",
                                                                     "arguments": {"environment_id": "env_1"}}]},
                {"role": "tool", "tool_call_id": "s1", "content": {"job_id": "job_7", "state": "running"}}]
    for c in (call("get_coding_task", job_id="job_7"), call("open_pull_request", environment_id="env_1",
                                                             title="Fix the typo")):
        assert "tool_calls" in g.check(mine, history, asked(c))
    # Another group, and the owner, cannot drive it, whatever their own history says.
    forged = answered("p2", '{"environment_id": "env_1"}')
    for c in (call("get_environment", environment_id="env_1"), call("cancel_coding_task", job_id="job_7"),
              call("destroy_environment", environment_id="env_1")):
        assert g.check(theirs, forged, asked(c))["content"] == NOT_YOURS
        assert g.check("fulla", [], asked(c))["content"] == A_GROUPS
    assert json.loads((tmp_path / "sandboxes.json").read_text()) == {
        "environments": {"env_1": testing.id}, "jobs": {"job_7": testing.id}}


def test_the_owner_keeps_their_own_sandboxes_and_any_repository(tmp_path):
    g, testing, _ = guard(tmp_path)
    anything = asked(call("provision_environment", repo="nornscode/bubbo"))
    assert g.check("fulla", [], anything) is anything
    history = answered("p1", '{"environment_id": "env_9", "state": "provisioning"}')
    old = asked(call("get_environment", environment_id="env_from_before"))
    assert g.check("fulla", history, old) is old  # from before the lock: nobody's, so the owner's
    assert g.check(group_agent_name(testing), [], asked(call("destroy_environment",
                                                             environment_id="env_9")))["content"] == NOT_YOURS


def test_the_worker_holds_the_models_calls_to_the_group(tmp_path, monkeypatch):
    g, testing, _ = guard(tmp_path)

    async def fake(self, task):
        return asked(call("provision_environment", repo="nornscode/bubbo"))

    monkeypatch.setattr(Norns, "_handle_llm_task", fake)
    monkeypatch.setattr(ServingNorns, "_list_agents",
                        lambda self: [SimpleNamespace(id=1, name="fulla"),
                                      SimpleNamespace(id=12, name=group_agent_name(testing))])
    worker = ServingNorns("http://unused", api_key="k", coding=g)
    task = {"agent_id": 12, "system_prompt": "You are Fulla.", "tools": [], "messages": []}
    assert asyncio.run(worker._handle_llm_task(task))["final_output"] == REPO_NOT_HERE
    assert "tool_calls" in asyncio.run(worker._handle_llm_task(dict(task, agent_id=1)))  # the owner's
    # An agent the server does not know is no scope.
    assert asyncio.run(worker._handle_llm_task(dict(task, agent_id=99)))["final_output"] == NOT_HERE


# --- the check-back -----------------------------------------------------------


class Client:
    def __init__(self):
        self.messages = []

    def send_message(self, agent, content, *, conversation_key=None, **_):
        self.messages.append((agent, content, conversation_key))
        return SimpleNamespace(run_id=70)


def test_a_check_back_is_indexed_in_the_groups_directory(tmp_path):
    store, testing, _ = stores(tmp_path)
    client = Client()
    gr.init(client=client, data_dir=tmp_path, timezone="America/Vancouver", groups=store)
    runctx.origins_from({9: TESTING}.get)
    try:
        assert gr.schedule_group_followup.handler("job_7 in env_1", 5).startswith("I can only")  # no run
        token = runctx.set_current_run(9)
        out = gr.schedule_group_followup.handler("job_7 in env_1: report it", 500)
        runctx.reset_current_run(token)
    finally:
        runctx.origins_from(None)
    assert out.startswith("Check-back set (run 70)")
    agent, content, key = client.messages[0]
    assert agent == "fulla-group-reminder" and key.startswith("group-followup:")
    assert content.startswith("What: check back: job_7 in env_1: report it\nDue: ")
    entry = json.loads(store.reminders_path(testing.id).read_text())["70"]
    assert entry["followup"] is True and entry["parent"] == 9 and entry["what"] == "job_7 in env_1: report it"


def test_a_check_back_comes_back_into_the_groups_conversation_and_is_not_posted(tmp_path):
    ch, sg, client, index = make_group(tmp_path, client=FakeClient())
    ch.handle(in_group("+16045550111", "Fulla, fix the typo in widgets", 1))  # run 9 at the group
    ch.handle_event(event("run.completed", 9, agent_id=SOCIAL, conversation_id=77, output="Starting a sandbox."))
    sent = len(sg.sent)
    index[70] = {"what": "env_1: start the job", "due_at": "2026-11-17T12:00", "parent": 9, "followup": True}
    client.next_run = 11
    ch.handle_event(event("run.completed", 70, agent_id=GROUP_REMINDER, conversation_id=88, output="Reminder: x"))
    assert len(sg.sent) == sent  # nothing posted by the check-back itself
    agent, content, key, _ = client.messages[-1]
    assert agent == "fulla-social" and content == "[scheduled] follow-up you set: env_1: start the job" and key == f"social:{TESTING}"
    assert ch.social_origin_of(11) == TESTING  # so the next check-back, and the guard's group, find the chat
    ch.handle_event(event("run.completed", 11, agent_id=SOCIAL, conversation_id=77, output="[silent]"))
    assert len(sg.sent) == sent


def test_the_watcher_reads_the_owners_sandboxes_and_changes_nothing(tmp_path):
    g, testing, _ = guard(tmp_path)
    for c in (call("get_environment", environment_id="env_old"), call("get_coding_task", job_id="job_old")):
        result = asked(c)
        assert g.check("fulla-watch", [], result) is result
    for c in (call("start_coding_agent", environment_id="env_old", task="x"),
              call("cancel_coding_task", job_id="job_old"),
              call("provision_environment", repo="nornscode/fulla")):
        assert g.check("fulla-watch", [], asked(c))["content"] == NOT_HERE
    # A group's sandbox is not the manager's, so not the watcher's either.
    history = answered("p1", '{"environment_id": "env_g", "state": "provisioning"}')
    g.check(group_agent_name(testing), history, asked(call("get_environment", environment_id="env_g")))
    assert g.check("fulla-watch", [], asked(call("get_environment", environment_id="env_g")))["content"] == A_GROUPS
    # It claims nothing from its own history.
    g.check("fulla-watch", answered("p2", '{"environment_id": "env_w"}'),
            asked(call("get_environment", environment_id="env_w")))
    assert "env_w" not in json.loads((tmp_path / "sandboxes.json").read_text())["environments"]

