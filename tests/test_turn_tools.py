"""What the manager is offered on a scheduled turn: its own tools for that
turn and no way to type into a terminal, and the size the standing prompt is
held to."""

import json
import re

import pytest

from fulla import evals, shell, tmux
from fulla.config import load_config
from fulla.directives import DirectiveStore
from fulla.mac.tools import mac_tools
from fulla.schedule import TURNS
from fulla.tools import manager_tools
from fulla.wake import TYPING, for_turn, offered
from fulla.worker import (
    LOOKING,
    LOOKS_AT_MACHINES,
    MACHINE_LOOKING,
    MANAGER_AGENT,
    PLAYBOOKS,
    SOCIAL_AGENT,
    TURN_TOOLS,
    manager_prompt,
    turn_note,
    turn_offers,
)

KNOWN = {t.name for t in manager_tools} | {t.name for t in mac_tools}
MACHINE = tmux.tool_names("box") + shell.tool_names("box")
PROMPT = "You are Fulla. ... `mac_things_add` for Things ..."
# Characters, with no machines, sources or directives, the soul unwritten and the
# default owner name ("the owner"): 18,865 when set.
BUDGET = 19_000


@pytest.fixture
def sent():
    """Every tool core sends the manager with the Mac, a box and Bubbo connected."""
    c = evals.Case(name="t", path=None, messages=[], checks=[], connected=["mac", "box", evals.BUBBO])
    return evals.sent_tools(c)


def names_on(message, sent, name=MANAGER_AGENT, history=()):
    task = {"tools": sent, "messages": [*history, {"role": "user", "content": message}]}
    return {t["name"] for t in offered(name, task)["tools"]}


def test_every_turn_has_a_set_of_real_tools_and_none_of_them_changes_a_machine():
    assert set(TURN_TOOLS) == set(TURNS) and LOOKS_AT_MACHINES <= set(TURNS)
    for name, tools in TURN_TOOLS.items():
        assert tools <= KNOWN | {"launch_agent"}, (name, tools - KNOWN)
        assert "ask_human" not in tools and "wait" not in tools
    # The suffixes name real machine tools, and reading never overlaps with typing or the shell's hands.
    for suffix in (*MACHINE_LOOKING, *TYPING):
        assert f"box{suffix}" in MACHINE, suffix
    looked = {n for n in MACHINE if n.endswith(MACHINE_LOOKING)}
    assert not looked & {n for n in MACHINE if n.endswith(TYPING)}
    assert not looked & {"box_exec", "box_write_file", "box_edit_file"}
    assert not {n for n in LOOKING if n.startswith("mac_")} & {"mac_things_add", "mac_home_run", "mac_applescript"}


def test_a_turn_is_offered_every_tool_its_playbook_names():
    """The playbook and the set are written apart; a tool the one names and
    the other leaves out would have the turn told to call what it cannot."""
    for name, text in PLAYBOOKS.items():
        named = {w for w in re.findall(r"`(\w+)`", text) if w in KNOWN}
        assert named, name
        assert named <= TURN_TOOLS[name], (name, named - TURN_TOOLS[name])


def test_the_quiet_turns_are_offered_exactly_their_sets(sent):
    assert names_on("[scheduled] nightly review", sent) == TURN_TOOLS["nightly-review"]
    assert names_on("[scheduled] shipping check", sent) == TURN_TOOLS["shipping-check"]
    assert names_on("[scheduled] question", sent) == TURN_TOOLS["question"]


def test_the_brief_and_the_check_in_look_and_do_not_touch(sent):
    brief, check_in = names_on("[scheduled] morning brief", sent), names_on("[scheduled] check-in", sent)
    # What the owner's standing instructions have them read, and each machine's reading tools.
    assert {"mac_things_list", "linear_issues", "mac_imessage_recent", "mac_tmux_read", "box_tmux_read",
            "box_grep", "calendar", "recall", "send_message", "launch_agent"} <= brief & check_in
    assert "draft_imessage" in check_in and "draft_imessage" not in brief
    for held in ("ask_human", "box_exec", "mac_write_file", "mac_tmux_send", "box_tmux_new", "set_trigger",
                 "set_directive", "allow_person", "set_group_credential", "mac_home_run", "mac_things_add",
                 "provision_environment", "linear_update", "propose_merge", "remember", "wait"):
        assert held not in brief | check_in, held
    assert brief <= {t["name"] for t in sent}


def test_a_follow_up_and_the_owners_own_timers_keep_everything_but_typing(sent):
    everything = {t["name"] for t in sent}
    typing = {n for n in everything if n.endswith(TYPING)}
    assert typing == {f"{m}{s}" for m in ("mac", "box") for s in TYPING}
    for message in ("[scheduled] follow-up: the build in work", "[scheduled] ask me how the week went"):
        assert names_on(message, sent) == everything - typing - {"ask_human", "wait"}, message


def test_the_owners_own_turn_is_offered_everything(sent):
    everything = {t["name"] for t in sent} - {"wait"}
    assert names_on("run the tests on the box", sent) == everything
    # Their word after a scheduled message, or joining a scheduled run, makes it their turn again.
    history = [{"role": "user", "content": "[scheduled] nightly review"},
               {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "name": "recall", "arguments": {}}]},
               {"role": "tool", "tool_call_id": "1", "content": "No facts."}]
    assert names_on("type make test into work", sent, history=history) == everything
    assert turn_offers("move the [scheduled] nightly review to 2am", "set_trigger")


def test_only_the_manager_is_held_to_a_turns_set_and_a_task_is_never_changed(sent):
    task = {"tools": sent, "messages": [{"role": "user", "content": "[scheduled] nightly review"}]}
    assert offered(None, task) is task  # a name not known yet: what core sent
    assert offered(MANAGER_AGENT, dict(task, purpose="compact"))["tools"] is sent
    social = {t["name"] for t in offered(SOCIAL_AGENT, task)["tools"]}
    assert "provision_environment" in social and "ask_human" not in social  # withheld as before, no set
    assert len(offered(MANAGER_AGENT, task)["tools"]) == len(TURN_TOOLS["nightly-review"])
    assert len(task["tools"]) == len(sent)


def test_a_scheduled_turn_carries_a_fraction_of_the_definitions(sent):
    def size(message):
        task = {"tools": sent, "messages": [{"role": "user", "content": message}]}
        return len(json.dumps(offered(MANAGER_AGENT, task)["tools"]))

    full = size("what's on today?")
    assert size("[scheduled] nightly review") < full / 8
    assert size("[scheduled] shipping check") < full / 8
    assert size("[scheduled] check-in") < full / 2


def test_the_note_tells_a_machine_that_is_off_from_one_this_turn_does_not_carry(sent):
    everything = [t["name"] for t in sent]
    note = turn_note(["now", "recall"], ["box"], connected=everything)
    assert "The Mac is connected on this turn" in note and "The machine box is connected on this turn" in note
    assert "its tools are not in your list on a turn like this one" in note and "is off" not in note
    assert "the Mac is off" in turn_note(["now", "recall"], connected=["now", "recall", "box_exec"])
    assert turn_note(["now"]) == turn_note(["now"], connected=["now"])

    review = {"system_prompt": PROMPT, "tools": sent,
              "messages": [{"role": "user", "content": "[scheduled] nightly review"}]}
    out = for_turn(MANAGER_AGENT, review, ["box"], "Robin")
    assert [t["name"] for t in out["tools"]] == sorted(TURN_TOOLS["nightly-review"])
    assert "The Mac is connected on this turn" in out["system_prompt"] and "is off" not in out["system_prompt"]
    assert "Your tool list is shorter on a timer's turn" in out["system_prompt"]
    # A check-in names the Mac's tools it does hold.
    check_in = for_turn(MANAGER_AGENT, dict(review, messages=[{"role": "user", "content": "[scheduled] check-in"}]))
    assert "mac_imessage_unanswered" in check_in["system_prompt"] and "mac_tmux_send" not in check_in["system_prompt"]


def test_the_standing_prompt_stays_within_its_budget(monkeypatch, tmp_path):
    """The manager's prompt was halved in November 2026 and is held there.
    New guidance goes where it costs least: a lock in code, then the tool's own
    description, then a scheduled turn's playbook, and into the standing
    prompt only when it is judgment across tools. To raise this number, first
    look for what the new text can replace."""
    monkeypatch.setenv("FULLA_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FULLA_TELEGRAM_USER_ID", "7")
    monkeypatch.setenv("NORNS_API_KEY", "k")
    # No machines, sources or directives, and the unwritten soul: the code's share alone.
    prompt = manager_prompt(load_config(), DirectiveStore(tmp_path / "d.json"))
    assert len(prompt) <= BUDGET, f"the manager's prompt is {len(prompt)} characters; the budget is {BUDGET}"

