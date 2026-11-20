"""The per-turn note the worker appends to the manager's prompt."""

import asyncio
from types import SimpleNamespace

from norns import Norns

from fulla.wake import ServingNorns, in_stable_order, with_turn_note
from fulla.worker import turn_note

MANAGER_PROMPT = "You are Fulla. ... `mac_things_add` for Things ..."
TOOLS = [{"name": "now"}, {"name": "mac_things_add"}, {"name": "ask_human"}, {"name": "mac_notify"}]


def test_note_names_the_mac_tools_present():
    note = turn_note(["now", "mac_notify", "mac_things_add", "wait"])
    assert note.startswith("# This turn\n")
    assert "mac_notify, mac_things_add" in note and "wait" not in note
    assert "The Mac is connected on this turn, whatever earlier turns said." in note


def test_note_says_off_when_none_present():
    note = turn_note(["now", "remember"])
    assert "No `mac_` tool" in note and "the Mac is off on this turn" in note


def test_with_turn_note_appends_to_the_manager_prompt_only():
    task = {"system_prompt": MANAGER_PROMPT, "tools": TOOLS, "messages": []}
    out = with_turn_note(task)
    assert out["system_prompt"].endswith(turn_note(["mac_things_add", "mac_notify"]))
    assert task["system_prompt"] == MANAGER_PROMPT  # the task core sent is untouched

    # Mac off but the manager: it still gets a note, saying so.
    off = with_turn_note({"system_prompt": MANAGER_PROMPT, "tools": [{"name": "now"}], "messages": []})
    assert "the Mac is off" in off["system_prompt"]

    # The reminder agent: no mac_ tools, no mention of them; left alone.
    reminder = {"system_prompt": "You are one commitment.", "tools": [{"name": "now"}], "messages": []}
    assert with_turn_note(reminder) is reminder
    # Compaction calls are summaries, not turns.
    compact = {"system_prompt": MANAGER_PROMPT, "tools": TOOLS, "purpose": "compact", "messages": []}
    assert with_turn_note(compact) is compact


def test_serving_norns_passes_the_noted_task_to_the_sdk(monkeypatch):
    seen = {}

    async def fake(self, task):
        seen["task"] = task
        return {"status": "ok"}

    monkeypatch.setattr(Norns, "_handle_llm_task", fake)
    worker = ServingNorns("http://unused", api_key="k")
    task = {"system_prompt": MANAGER_PROMPT, "tools": TOOLS, "messages": []}
    assert asyncio.run(worker._handle_llm_task(task)) == {"status": "ok"}
    assert "# This turn" in seen["task"]["system_prompt"]


def test_serving_norns_withholds_builtins_from_the_reader_and_followups(monkeypatch):
    seen = []

    async def fake(self, task):
        seen.append([t["name"] for t in task["tools"]])
        return {"status": "ok"}

    lookups = []

    def list_agents(self):
        lookups.append(1)
        return [SimpleNamespace(id=1, name="fulla"), SimpleNamespace(id=4, name="fulla-followup"),
                SimpleNamespace(id=5, name="fulla-reader")]

    monkeypatch.setattr(Norns, "_handle_llm_task", fake)
    monkeypatch.setattr(ServingNorns, "_list_agents", list_agents)
    worker = ServingNorns("http://unused", api_key="k")
    builtins = [{"name": n} for n in ("wait", "ask_human", "launch_agent", "list_agents")]

    reader = {"agent_id": 5, "system_prompt": "You read the web.", "messages": [],
              "tools": builtins + [{"name": "now"}, {"name": "web_search"}, {"name": "fetch_url"}]}
    asyncio.run(worker._handle_llm_task(reader))
    assert seen[-1] == ["fetch_url", "now", "web_search"]
    assert len(reader["tools"]) == 7  # the task core sent is untouched

    asyncio.run(worker._handle_llm_task(dict(reader, agent_id=4, tools=builtins + [{"name": "now"}])))
    assert seen[-1] == ["now", "wait"]

    asyncio.run(worker._handle_llm_task({"agent_id": 1, "system_prompt": MANAGER_PROMPT,
                                         "tools": builtins, "messages": []}))
    assert seen[-1] == ["ask_human", "launch_agent", "list_agents"]  # the manager never waits
    assert len(lookups) == 1  # resolved once per incarnation


def test_the_manager_is_not_offered_ask_human_on_a_scheduled_turn(monkeypatch):
    # Runs 679 and 703: a check-in parked on ask_human, and the next timer was
    # read as the answer. Parked, it would have read the owner's next message so.
    seen = []

    async def fake(self, task):
        seen.append({t["name"] for t in task["tools"]})
        return {"status": "ok"}

    def list_agents(self):
        return [SimpleNamespace(id=1, name="fulla"), SimpleNamespace(id=2, name="fulla-reminder")]

    monkeypatch.setattr(Norns, "_handle_llm_task", fake)
    monkeypatch.setattr(ServingNorns, "_list_agents", list_agents)
    worker = ServingNorns("http://unused", api_key="k")
    tools = [{"name": n} for n in ("wait", "ask_human", "launch_agent", "now")]

    def offered(*messages, agent_id=1):
        asyncio.run(worker._handle_llm_task({"agent_id": agent_id, "system_prompt": MANAGER_PROMPT,
                                             "tools": tools, "messages": list(messages)}))
        return seen[-1]

    def user(text):
        return {"role": "user", "content": text}

    step = [{"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "name": "now", "arguments": {}}]},
            {"role": "tool", "tool_call_id": "t1", "name": "now", "content": "15:00"}]
    assert offered(user("[scheduled] check-in")) == {"launch_agent", "now"}
    assert offered(user("hi"), user("[scheduled] check-in"), *step) == {"launch_agent", "now"}  # every step of it
    assert offered(user("[scheduled] follow-up: check the build")) == {"launch_agent", "now"}

    # The owner's own turn keeps it, after a scheduled one too.
    assert offered(user("which dentist was it?")) == {"ask_human", "launch_agent", "now"}
    after = offered(user("[scheduled] check-in"), {"role": "assistant", "content": "[silent]"}, user("thanks"))
    assert after == {"ask_human", "launch_agent", "now"}

    # A reminder is one run per commitment and asks by design, whatever it is told.
    assert "ask_human" in offered(user("[scheduled] check-in"), agent_id=2)
    assert len(tools) == 4  # the task core sent is untouched


def test_serving_norns_withholds_nothing_when_agents_cannot_be_resolved(monkeypatch):
    seen = []

    async def fake(self, task):
        seen.append(task["tools"])
        return {"status": "ok"}

    def list_agents(self):
        raise RuntimeError("norns down")

    monkeypatch.setattr(Norns, "_handle_llm_task", fake)
    monkeypatch.setattr(ServingNorns, "_list_agents", list_agents)
    worker = ServingNorns("http://unused", api_key="k")
    tools = [{"name": "ask_human"}, {"name": "now"}]
    assert asyncio.run(worker._handle_llm_task({"agent_id": 5, "tools": tools, "messages": []})) == {"status": "ok"}
    assert seen == [tools]


def test_tools_reach_the_model_in_one_order_however_core_lists_them(monkeypatch):
    # Core lists the Mac's tools first on one call and last on the next. The
    # tool list leads the cached prefix, so its order must not follow core's.
    seen = []

    async def fake(self, task):
        seen.append([t["name"] for t in task["tools"]])
        return {"status": "ok"}

    monkeypatch.setattr(Norns, "_handle_llm_task", fake)
    worker = ServingNorns("http://unused", api_key="k")
    mac_first = [{"name": "mac_notify"}, {"name": "mac_things_add"}, {"name": "now"}, {"name": "ask_human"}]
    mac_last = [{"name": "now"}, {"name": "ask_human"}, {"name": "mac_things_add"}, {"name": "mac_notify"}]
    for tools in (mac_first, mac_last):
        asyncio.run(worker._handle_llm_task({"system_prompt": MANAGER_PROMPT, "tools": tools, "messages": []}))
    assert seen[0] == seen[1] == ["ask_human", "mac_notify", "mac_things_add", "now"]
    assert mac_first[0]["name"] == "mac_notify"  # the task core sent is untouched


def test_in_stable_order_leaves_a_sorted_task_alone():
    task = {"tools": [{"name": "a"}, {"name": "b"}]}
    assert in_stable_order(task) is task
    assert in_stable_order({"messages": []}) == {"messages": []}


def test_a_scheduled_turn_gets_its_instructions_after_the_note_and_no_other_turn_does():
    brief = {"system_prompt": MANAGER_PROMPT, "tools": TOOLS,
             "messages": [{"role": "user", "content": "what's on today?"},
                          {"role": "assistant", "content": "Nothing."},
                          {"role": "user", "content": "[scheduled] morning brief"}]}
    out = with_turn_note(brief, owner="Robin")["system_prompt"]
    assert out.index("# This turn") < out.index("# This scheduled turn")
    assert "The morning brief." in out and "A timer started this turn, not Robin." in out
    assert "The check-in" not in out

    # The owner speaking after a scheduled message: an ordinary turn again.
    after = dict(brief, messages=brief["messages"] + [{"role": "assistant", "content": "Brief."},
                                                      {"role": "user", "content": "thanks"}])
    assert "# This scheduled turn" not in with_turn_note(after, owner="Robin")["system_prompt"]
    # An agent that is not the manager is left alone, whatever its message says.
    reminder = {"system_prompt": "You are one commitment.", "tools": [{"name": "now"}],
                "messages": [{"role": "user", "content": "[scheduled] nightly review"}]}
    assert with_turn_note(reminder, owner="Robin") is reminder
