"""The two locks on a group: an agent per group whose allowlist is exactly
its tools (outer), and tools bound to the run's group, never to an argument
(inner)."""

import pytest

from fulla import groupctx, runctx
from fulla.config import load_config
from fulla.directives import DirectiveStore
from fulla.groups import GroupStore, group_agent_name
from fulla.people import PeopleStore
from fulla.tools import group_sources, manager_tools, social_tools
from fulla.wake import WITHHELD, withheld_for
from fulla.worker import SOCIAL_AGENT, build_agents

ALPHA, BETA = "signal:group:aaa", "signal:group:bbb"


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("FULLA_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FULLA_TELEGRAM_USER_ID", "7")
    monkeypatch.setenv("NORNS_API_KEY", "k")
    return load_config()


def two_groups(tmp_path):
    people = PeopleStore(tmp_path / "people.json")
    for address, name in ((ALPHA, "Alpha"), (BETA, "Beta")):
        people.remember_group(address, name)
        people.allow_group(address)
    store = GroupStore(tmp_path / "groups")
    a, b = store.sync(people)
    store.set_tools(a.id, ["web", "memory"])
    store.add_source(a.id, "github_repo", "nornscode/fulla", "Fulla")
    store.add_source(a.id, "url", "https://docs.norns.dev", "the Norns docs")
    store.add_source(b.id, "github_repo", "other/secret", "Secret")
    return store, people, store.get(a.id), store.get(b.id)


def test_each_allowed_group_gets_an_agent_whose_allowlist_is_exactly_its_tools(config, tmp_path):
    store, people, a, b = two_groups(tmp_path)
    agents = build_agents(config, DirectiveStore(tmp_path / "d.json"), groups=store.sync(people))
    by_name = {ag.name: ag for ag in agents}
    ga, gb, social = by_name[group_agent_name(a)], by_name[group_agent_name(b)], by_name[SOCIAL_AGENT]
    # The outer lock: the allowlist is the group's tools and nothing else ("web" is the reader).
    assert set(ga.allowed_tools) == {t.name for t in social_tools} | {"group_remember", "group_recall"}
    assert set(gb.allowed_tools) == {t.name for t in social_tools}
    assert not any(n.startswith("github_") for n in ga.allowed_tools)  # github is not enabled for Alpha
    assert set(social.allowed_tools) == {t.name for t in social_tools}  # one to one: never a group's tools
    assert not (set(ga.allowed_tools) & ({t.name for t in manager_tools} - {"now"}))
    assert ga.subagents["mode"] == "allowlist" and gb.subagents == {"mode": "disabled"}
    # Each prompt is rendered from its own record: this group, its sources by label, nothing else.
    assert "The group Alpha." in ga.system_prompt and "Fulla (github_repo nornscode/fulla)" in ga.system_prompt
    assert "fulla-reader" in ga.system_prompt and "fulla-reader" not in gb.system_prompt
    assert "Nothing is connected here" not in ga.system_prompt
    for other in ("Secret", "other/secret", b.id, "Beta"):
        assert other not in ga.system_prompt
    assert "Alpha" not in gb.system_prompt and "Alpha" not in social.system_prompt
    assert "A direct chat" in social.system_prompt and "Nothing is connected here" in social.system_prompt
    # Nothing of the owner's, and nothing of Fulla's own: no map, no machines, no file, host or model.
    for prompt in (ga.system_prompt, gb.system_prompt, social.system_prompt):
        for leak in ("Hard limits", "Your machines", "data/", ".json", config.norns.runtime_url, config.fulla.model,
                     "fulla-group-"):
            assert leak not in prompt, leak


def test_the_group_comes_from_the_run_and_nothing_outside_its_sources_is_reachable(tmp_path):
    store, _people, a, _b = two_groups(tmp_path)
    groupctx.init(store=store)
    runctx.origins_from({9: ALPHA, 10: BETA, 11: "signal:+16045550111"}.get)
    try:
        assert groupctx.current() is None and group_sources.handler() == groupctx.NO_GROUP  # no run at all
        with pytest.raises(groupctx.Refused):
            groupctx.require()
        token = runctx.set_current_run(9)
        assert groupctx.current().id == a.id
        assert group_sources.handler() == ("Connected to Alpha: Fulla (github_repo nornscode/fulla); "
                                           "the Norns docs (url https://docs.norns.dev).")
        assert [s.identifier for s in groupctx.scoped(a, "github_repo")] == ["nornscode/fulla"]  # none named: its own
        assert groupctx.scoped(a, "github_repo", "fulla")[0].identifier == "nornscode/fulla"  # by label
        assert groupctx.scoped(a, "url", "docs.norns.dev/guide/x")[0].label == "the Norns docs"  # by host
        with pytest.raises(groupctx.Refused, match="isn't connected here"):
            groupctx.scoped(a, "github_repo", "other/secret")  # Beta's, named outright
        with pytest.raises(groupctx.Refused, match="Nothing of that kind"):
            groupctx.scoped(a, "linear_team")
        store.set_tools(a.id, ["web", "github"])  # the allowlist widening changes nothing about reach
        with pytest.raises(groupctx.Refused):
            groupctx.scoped(store.get(a.id), "github_repo", "other/secret")
        runctx.reset_current_run(token)
        token = runctx.set_current_run(10)
        assert group_sources.handler() == "Connected to Beta: Secret (github_repo other/secret)."
        runctx.reset_current_run(token)
        token = runctx.set_current_run(11)
        assert groupctx.current() is None and group_sources.handler().startswith("Nothing is connected to a direct chat")
        runctx.reset_current_run(token)
    finally:
        runctx.origins_from(None)
        groupctx.init(store=None)


def test_a_group_agent_is_withheld_what_the_social_agent_is():
    assert withheld_for("fulla-group-ab12") == WITHHELD[SOCIAL_AGENT]
    assert withheld_for("fulla") == WITHHELD["fulla"] and withheld_for("someone-else") == frozenset()
