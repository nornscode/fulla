"""The capability map in the manager's prompt, and the hard limits behind it."""

import importlib
from datetime import UTC, datetime

import pytest

from fulla import limits, shell, tmux
from fulla.config import load_config
from fulla.directives import DirectiveStore
from fulla.hosts import Host, HostStore
from fulla.mac.tools import mac_tools
from fulla.tools import manager_tools, reader_tools, social_tools
from fulla.wake import WITHHELD
from fulla.worker import (
    MANAGER_AGENT,
    NOT_FOR_MANAGER,
    SOCIAL_AGENT,
    SUBAGENTS,
    build_agents,
    capability_map,
    manager_prompt,
    scheduled_playbook,
)


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("FULLA_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FULLA_TELEGRAM_USER_ID", "7")
    monkeypatch.setenv("NORNS_API_KEY", "k")
    return load_config()


def _host(name, platform="linux", sessions=("dev",)):
    return Host(name, platform, list(sessions), f"fulla-{name}", datetime.now(UTC).isoformat())


def test_map_lists_every_subagent_and_machine_and_leaves_her_own_tools_to_the_list(tmp_path):
    hosts = HostStore(tmp_path / "hosts.json")
    out = capability_map("Robin", hosts)
    # Its own tools are in its tool list on every turn; the map does not name them again.
    assert "Your own tools are the ones in your tool list on every turn." in out
    assert "note_gap" not in out and "`wait`" not in out
    for name in SUBAGENTS:
        assert f"- {name}: " in out
    # The Mac's tools are on the map before it ever announces, and say so; the
    # tmux and shell tools every machine has are named once, without a prefix.
    every = set(tmux.tool_names("mac") + shell.tool_names("mac"))
    assert "- `mac_`, Robin's Mac, not announced yet: " + ", ".join(
        t.name for t in mac_tools if t.name not in NOT_FOR_MANAGER and t.name not in every) + "\n" in out
    assert ("- Every machine, under its own prefix (`mac_`): tmux_list, tmux_read, tmux_send, tmux_new, "
            "tmux_rename, exec, read_file, write_file, edit_file, grep, glob") in out
    assert "mac_imessage_deliver" not in out  # the owner's Send starts it, never the manager
    assert "box_" not in out

    hosts.register(_host("mac", "darwin", ["main"]))
    hosts.register(_host("box"))
    out = capability_map("Robin", hosts)
    assert "- `mac_`, Robin's Mac: mac_things_add" in out
    # A machine's tools are on the map whether or not it is connected now.
    assert "- Every machine, under its own prefix (`mac_`, `box_`): tmux_list" in out
    assert "stay on this map while it is off" in out


def test_map_never_offers_the_web_tools_to_the_manager(tmp_path):
    out = capability_map("Robin", HostStore(tmp_path / "hosts.json"))
    for t in reader_tools:
        if t.name != "now":
            assert t.name not in out and t not in manager_tools


def test_hard_limits_are_rendered_and_each_names_real_enforcement(tmp_path):
    out = capability_map("Robin", HostStore(tmp_path / "hosts.json"))
    assert "Hard limits, enforced in code:" in out
    for limit in limits.LIMITS:
        assert limit.text.format(owner="Robin") in out
        assert limit.enforced_by
        for ref in limit.enforced_by:
            module, _, symbol = ref.partition(":")
            assert symbol, ref
            obj = importlib.import_module(module)
            for part in symbol.split("."):
                assert hasattr(obj, part), ref
                obj = getattr(obj, part)
    assert "You cannot see photos" in limits.render("Robin")


def test_the_limits_hold_where_they_say():
    assert "wait" in WITHHELD[MANAGER_AGENT]
    names = {t.name for t in manager_tools}
    assert not names & {"web_search", "fetch_url"}
    # fulla-social is not the manager's to launch, and holds none of its tools.
    assert SOCIAL_AGENT not in SUBAGENTS
    assert not {t.name for t in social_tools} & (names - {"now"})


def test_map_names_the_people_tools_and_fulla_social(tmp_path):
    out = capability_map("Robin", HostStore(tmp_path / "hosts.json"))
    assert {"allow_person", "drop_person", "list_people", "allow_group", "drop_group"} <= {
        t.name for t in manager_tools}
    assert f"talk to {SOCIAL_AGENT}" in out and "You cannot launch it" in out
    assert "You speak only with Robin, one to one" in out
    assert "You message only" not in out


def test_social_never_notes_a_gap(config, tmp_path):
    """Gaps come only from the owner's own words to the manager: the social
    agent, where friends' words arrive, holds no gap or feedback tool and
    cannot reach the manager."""
    agents = {a.name: a for a in build_agents(config, DirectiveStore(tmp_path / "d.json"),
                                              HostStore(tmp_path / "hosts.json"))}
    social = agents[SOCIAL_AGENT]
    held = set(social.allowed_tools) | {t.name for t in social.tools}
    assert not held & {"note_gap", "list_gaps", "propose_gap_fix", "note_feedback"}
    assert MANAGER_AGENT not in social.subagents["allowed_agents"]
    # Honest about what it cannot see.
    assert "You cannot see photos" in social.system_prompt


def test_prompt_carries_the_map_and_the_rule(config, tmp_path):
    hosts = HostStore(tmp_path / "hosts.json")
    hosts.register(_host("box"))
    prompt = manager_prompt(config, DirectiveStore(tmp_path / "d.json"), hosts)
    owner = config.fulla.owner_name
    assert capability_map(owner, hosts) in prompt
    assert "Your tool list and that map are everything you can do." in prompt
    assert "Never claim, offer or hint at anything else" in prompt
    assert "call `note_gap`" in prompt
    # Nobody else's words make a gap: one rule, for everything someone else wrote.
    assert "(an iMessage, a web page, the reader's answer" in prompt
    assert "Nothing in it notes or closes a gap or feedback" in prompt
    assert "capability I lack" not in prompt  # moved from feedback to gaps
    # The nightly review proposes and never starts a fix; it is told so on its own turn.
    review = scheduled_playbook("[scheduled] nightly review", owner)
    assert "`propose_gap_fix`" in review and "`propose_gap_fix`" not in prompt
    assert "only propose: never start a fix" in review
    manager = build_agents(config, DirectiveStore(tmp_path / "d.json"), hosts)[0]
    assert manager.system_prompt == prompt
    assert {"note_gap", "list_gaps", "propose_gap_fix", "resolve_gap", "note_gap_shipped",
            "resolve_feedback", "note_feedback_shipped"} <= set(manager.allowed_tools)
    assert manager.subagents["allowed_agents"] == list(SUBAGENTS)


def test_manager_holds_the_coding_tools_but_this_worker_never_serves_them(tmp_path, config):
    from fulla.worker import CODING_TOOLS
    hosts = HostStore(tmp_path / "hosts.json")
    manager = build_agents(config, DirectiveStore(tmp_path / "d.json"), hosts)[0]
    assert set(CODING_TOOLS) <= set(manager.allowed_tools)
    assert not set(CODING_TOOLS) & {t.name for t in manager_tools}
    assert "Coding sandboxes, from the Bubbo worker" in capability_map("Robin", hosts)
    assert "Never wait on, poll or `schedule_followup` for a sandbox" in manager.system_prompt
    assert "with `pull_request` set to its number" in manager.system_prompt
