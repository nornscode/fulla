"""Who else may talk to Fulla: the store and the owner's tools over it."""

import pytest

from fulla.people import PeopleStore
from fulla.tools import people as tools


def test_people_are_found_by_their_id_on_a_transport(tmp_path):
    store = PeopleStore(tmp_path / "people.json")
    store.allow_person("Sam", {"signal": "+16045550111"})
    store.allow_person("Sam", {"telegram": "4242"})  # adds an id to the same person
    assert store.person_for("signal", "+16045550111").name == "Sam"
    assert store.person_for("telegram", 4242).name == "Sam"
    assert store.person_for("telegram", 7) is None
    again = PeopleStore(tmp_path / "people.json")  # survives a restart
    assert [p.name for p in again.people()] == ["Sam"]
    assert again.drop_person("sam") and again.person_for("signal", "+16045550111") is None


def test_a_person_may_go_by_several_ids_and_an_unlisted_one_is_remembered_until_the_owner_decides(tmp_path):
    store = PeopleStore(tmp_path / "people.json")
    store.allow_person("Sam", {"signal": ["+16045550111", "u-sam"]})
    assert store.person_for("signal", "u-sam").name == "Sam"
    stranger = store.remember_person("signal", "u-kim", "Kim")
    assert stranger.name == "Kim" and not stranger.allowed and store.person_for("signal", "u-kim") is None
    assert store.remember_person("signal", "u-kim", "") is stranger or len(store.people()) == 2
    store.mark_person_asked("signal", "u-kim", "telegram:7#101")
    assert store.person_for_ask("telegram:7#101").name == "Kim"
    store.decide_person("signal", "u-kim", False)
    assert store.person_for("signal", "u-kim") is None and "Kim" in store.render().split("not let in")[1]
    store.allow_person("Kim Lee", {"signal": "u-kim"})  # the owner lets them in later, by id, with a fuller name
    assert store.person_for("signal", "u-kim").name == "Kim Lee" and len(store.people()) == 2


def test_a_person_needs_a_name_and_an_id(tmp_path):
    store = PeopleStore(tmp_path / "people.json")
    with pytest.raises(ValueError):
        store.allow_person("", {"signal": "+1"})
    with pytest.raises(ValueError):
        store.allow_person("Sam", {"signal": ""})


def test_groups_are_remembered_then_asked_then_allowed(tmp_path):
    store = PeopleStore(tmp_path / "people.json")
    g = store.remember_group("signal:group:abc", "Friends")
    assert not g.allowed and not g.asked
    assert store.remember_group("signal:group:abc", "") is not None and len(store.groups()) == 1
    store.mark_asked("signal:group:abc", "telegram:7#101")
    assert store.group_for_ask("telegram:7#101").name == "Friends"
    assert store.group_named("friends").address == "signal:group:abc"
    assert store.group_named("1").address == "signal:group:abc"
    store.allow_group("signal:group:abc")
    assert PeopleStore(tmp_path / "people.json").group("signal:group:abc").allowed
    assert store.drop_group("signal:group:abc") and store.groups() == []


def test_the_owners_tools_validate_ids_and_render_the_lists(tmp_path):
    tools.init(store=PeopleStore(tmp_path / "people.json"))
    assert tools.list_people.handler() == "Nobody but the owner may talk to you.\nNo groups."
    assert "number" in tools.allow_person.handler("Sam", telegram_user_id="not-a-number")
    assert "international" in tools.allow_person.handler("Sam", signal_number="604 555 0111")
    assert tools.allow_person.handler("Sam", signal_number="+1 604-555-0111") == \
        "Sam may talk to you: signal +16045550111"
    assert "Sam" in tools.list_people.handler()
    assert tools.allow_group.handler("friends").startswith("No group matches")
    tools._store.remember_group("telegram:-100", "Friends")
    assert tools.allow_group.handler("Friends") == "You may talk in Friends when someone addresses you."
    assert tools.drop_group.handler("friends") == "You no longer talk in Friends."
    assert tools.drop_person.handler("Sam") == "Dropped Sam."


def test_the_social_agent_carries_nothing_of_the_owners(tmp_path, monkeypatch):
    from fulla.config import load_config
    from fulla.directives import DirectiveStore
    from fulla.soul import Soul
    from fulla.worker import SOCIAL_AGENT, build_agents

    monkeypatch.setenv("FULLA_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FULLA_TELEGRAM_USER_ID", "7")
    monkeypatch.setenv("NORNS_API_KEY", "k")
    (tmp_path / "soul.md").write_text("I am secretly fond of marmalade, and Robin brushes off gym nudges.\n")
    directives = DirectiveStore(tmp_path / "d.json")
    directives.add("Never mention the marmalade.")
    agents = {a.name: a for a in build_agents(load_config(), directives, soul=Soul(tmp_path / "soul.md"))}
    manager, social = agents["fulla"], agents[SOCIAL_AGENT]
    assert "marmalade" in manager.system_prompt
    assert "marmalade" not in social.system_prompt and "gym" not in social.system_prompt
    owners_tools = {"remember", "recall", "forget", "calendar", "schedule_reminder", "show_soul", "write_soul",
                    "set_directive", "note_feedback", "queue_for_mac", "schedule_followup", "allow_person",
                    "allow_group", "mac_things_add", "mac_imessage_send", "draft_imessage",
                    "mac_imessage_deliver"}
    assert owners_tools & set(social.allowed_tools) == set()
    assert social.allowed_tools == ["now", "schedule_group_reminder", "drop_group_reminder", "group_sources"]
    assert social.subagents["allowed_agents"] == ["fulla-reader"]
    assert "Never call `wait`" in social.system_prompt
    assert agents["fulla-group-reminder"].allowed_tools == ["now"]


def test_people_have_ids_of_their_own_and_old_records_get_one(tmp_path):
    path = tmp_path / "people.json"
    path.write_text('{"people": [{"name": "Sam", "ids": {"signal": ["+16045550111"]}}], '
                    '"groups": [{"address": "signal:group:abc", "name": "Friends", "allowed": true}]}')
    store = PeopleStore(path)
    sam = store.person_named("Sam")
    assert len(sam.id) == 8 and store.person_by_id(sam.id) is sam or store.person_by_id(sam.id).name == "Sam"
    assert PeopleStore(path).person_named("Sam").id == sam.id  # kept, not minted again
    assert store.group("signal:group:abc").members == []


def test_a_group_remembers_who_has_spoken_in_it(tmp_path):
    store = PeopleStore(tmp_path / "people.json")
    assert store.saw_in_group("signal:group:nope", "signal", "u-kim", "Kim") is None
    store.remember_group("signal:group:abc", "Friends")
    kim = store.saw_in_group("signal:group:abc", "signal", "u-kim", "Kim")
    assert kim.name == "Kim" and not kim.allowed
    store.allow_person("Sam", {"signal": "+16045550111"})
    store.saw_in_group("signal:group:abc", "signal", "+16045550111", "Sam")
    store.saw_in_group("signal:group:abc", "signal", "+16045550111", "Sam")  # once
    assert [p.name for p in store.members("signal:group:abc")] == ["Kim", "Sam"]
    assert store.group("signal:group:abc").members == [kim.id, store.person_named("Sam").id]
    assert store.members("signal:group:nope") == []
