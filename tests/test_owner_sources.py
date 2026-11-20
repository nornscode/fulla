"""The owner's own sources: the manager gets the groups' GitHub and Linear
tools over `data/groups/owner/`, by the same rule. The scope comes from the
run, here from the agent id core puts on the task, never from the model."""

import httpx
import pytest

from fulla import groupctx, runctx
from fulla.config import load_config
from fulla.directives import DirectiveStore
from fulla.groups import OWNER_ID, GroupStore
from fulla.people import PeopleStore
from fulla.tools import group_github as github_tools
from fulla.tools import groups as tools
from fulla.tools import manager_tools, social_tools
from fulla.wake import ServingNorns
from fulla.worker import build_agents, manager_prompt, own_code, scheduled_playbook
from tests.test_channel import Agent as FakeAgent
from tests.test_group_github import FakeGitHub, in_run

ALPHA = "signal:group:aaa"
MANAGER, GROUP_AGENT = 10, 49


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("FULLA_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FULLA_TELEGRAM_USER_ID", "7")
    monkeypatch.setenv("NORNS_API_KEY", "k")
    return load_config()


@pytest.fixture
def world(tmp_path):
    people = PeopleStore(tmp_path / "people.json")
    people.remember_group(ALPHA, "Alpha")
    people.allow_group(ALPHA)
    store = GroupStore(tmp_path / "groups", secret="s")
    (alpha,) = store.sync(people)
    store.set_tools(alpha.id, ["github"])
    store.add_source(alpha.id, "github_repo", "other/theirs", "Theirs")
    tools.init(store=store, people=people, owner_name="Robin")
    groupctx.init(store=store, owner_name="Robin")
    runctx.origins_from({9: ALPHA}.get)
    runctx.manager_from(lambda agent_id: agent_id == MANAGER)
    yield store, people, alpha
    runctx.origins_from(None)
    runctx.manager_from(None)
    groupctx.init(store=None)


class as_agent:
    def __init__(self, agent_id, run_id):
        self.agent_id, self.run_id = agent_id, run_id

    def __enter__(self):
        self.tokens = (runctx.set_current_run(self.run_id), runctx.set_current_agent(self.agent_id))

    def __exit__(self, *exc):
        runctx.reset_current_agent(self.tokens[1])
        runctx.reset_current_run(self.tokens[0])


def test_me_is_the_owners_own_directory_and_no_group(world):
    store, _people, alpha = world
    assert tools.connect_group_source.handler("me", "github_repo", "nornscode/fulla", "Fulla") == \
        "Robin now has Fulla (github_repo nornscode/fulla)."
    assert tools.set_group_tools.handler("myself", "web").startswith("Your own tools are fixed")
    assert tools.set_group_credential.handler("me", "github", "tok-alpha").startswith("Stored a GitHub token for Robin.")
    out = tools.list_groups.handler()
    assert out.startswith("Your own sources, for you here: Fulla (github_repo nornscode/fulla); credentials set: github.")
    assert "1. Alpha (" in out and "owner" not in out.split("Groups, each")[1].split("Tool catalogue")[0]
    assert [r.id for r in store.all()] == [alpha.id]  # no group record, so no agent of its own
    assert store.get(OWNER_ID).sources[0].identifier == "nornscode/fulla"
    assert tools.connect_group_source.handler("Chess", "url", "https://x.y").startswith(
        "No group matches 'Chess'; \"me\" is your own.")


def test_the_scope_comes_from_the_agent_core_named(world):
    store, _people, alpha = world
    store.owner("Robin")
    with as_agent(MANAGER, 5):  # the manager, no social origin
        assert groupctx.current().id == OWNER_ID
    with as_agent(GROUP_AGENT, 9):  # a group's run: its own directory, never the owner's
        assert groupctx.current().id == alpha.id
    with as_agent(GROUP_AGENT, 6):  # a run the channel does not know, from some other agent
        assert groupctx.current() is None
    with in_run(5):  # a run with no agent on the task
        assert groupctx.current() is None
    assert groupctx.current() is None


def test_the_manager_reads_its_own_sources_and_a_group_cannot_reach_them(world, tmp_path):
    store, _people, alpha = world
    tools.connect_group_source.handler("me", "github_repo", "nornscode/fulla", "Fulla")
    store.set_credential(OWNER_ID, "github", "tok-alpha")
    store.set_credential(alpha.id, "github", "tok-alpha")
    fake = FakeGitHub()
    github_tools.init(store=store, propose=None, transport=httpx.MockTransport(fake.handler))
    try:
        with as_agent(MANAGER, 5):
            out = github_tools.github_prs.handler()
            assert "nornscode/fulla, open:\n#12 by dependabot[bot]" in out
            assert github_tools.github_prs.handler("other/theirs") == groupctx.NOT_CONNECTED  # Alpha's, not the owner's
        with as_agent(GROUP_AGENT, 9):
            assert github_tools.github_prs.handler("nornscode/fulla") == groupctx.NOT_CONNECTED  # the owner's, not Alpha's
            assert github_tools.github_prs.handler("Fulla") == groupctx.NOT_CONNECTED
    finally:
        github_tools.init(store=None)
    assert all(path.startswith("/repos/nornscode/fulla") for _, path, _ in fake.calls)


def test_the_manager_holds_the_tools_and_its_prompt_shows_its_sources(config, world, tmp_path):
    store, people, _alpha = world
    names = {t.name for t in manager_tools}
    assert {"github_prs", "propose_merge", "propose_review", "linear_issues", "propose_linear_issue", "linear_assign",
            "linear_update", "linear_comment", "linear_create_issue"} <= names  # the owner's own Linear needs no switch
    assert not {"group_remember", "group_recall"} & names  # the owner's memory is `remember`
    assert not ({t.name for t in social_tools} - {"now"}) & names
    prompt = manager_prompt(config, DirectiveStore(tmp_path / "d.json"), store=store)
    assert "# Your sources\nNothing is connected for you yet." in prompt
    assert "`propose_review`" in prompt
    tools.connect_group_source.handler("me", "linear_team", "FUL", "Fulla")
    prompt = manager_prompt(config, DirectiveStore(tmp_path / "d.json"), store=store)
    assert "Connected for you: Fulla (linear_team FUL)." in prompt and "other/theirs" not in prompt
    agents = build_agents(config, DirectiveStore(tmp_path / "d.json"), groups=store.sync(people), store=store)
    group = next(a for a in agents if a.name.startswith("fulla-group-"))
    assert "FUL" not in group.system_prompt and "Your sources" not in group.system_prompt
    assert "Fulla (linear_team FUL)" in agents[0].system_prompt


def test_the_worker_tells_the_managers_id_from_the_server(monkeypatch):
    norns = ServingNorns("http://unused", api_key="k")
    monkeypatch.setattr(ServingNorns, "_list_agents", lambda self: [FakeAgent(id=10, name="fulla"),
                                                                     FakeAgent(id=49, name="fulla-group-ab")])
    assert norns._is_manager(10) and not norns._is_manager(49)
    monkeypatch.setattr(ServingNorns, "_list_agents", lambda self: (_ for _ in ()).throw(RuntimeError("down")))
    assert norns._is_manager(10)  # resolved once, kept
    assert not ServingNorns("http://unused", api_key="k")._is_manager(10)  # unknown means no


def test_the_prompt_names_her_own_code_only_once_it_is_a_source(config, world, tmp_path):
    store, _people, _alpha = world
    config.fulla.repo, config.fulla.commit = "nornscode/fulla", "0123456789abcdef"
    prompt = manager_prompt(config, DirectiveStore(tmp_path / "d.json"), store=store)
    assert "Your own code is the repository nornscode/fulla, but it is not among your sources" in prompt
    assert "commit 0123456 of it" in prompt
    tools.connect_group_source.handler("me", "github_repo", "NornsCode/Fulla", "Fulla")
    prompt = manager_prompt(config, DirectiveStore(tmp_path / "d.json"), store=store)
    assert "Your own code is the repository nornscode/fulla, among your sources" in prompt
    assert "commit 0123456 of it; a commit merged after that one is not running yet" in prompt
    assert "remains the truth about what you can do" in prompt
    # The shipping check is a turn of its own, after the review; a note is never a close.
    check = scheduled_playbook("[scheduled] shipping check", "Robin")
    assert "The shipping check: only if your own code is among your sources" in check
    assert "`note_gap_shipped` or `note_feedback_shipped`" in check and "at most five notes a night" in check
    review = scheduled_playbook("[scheduled] nightly review", "Robin")
    assert "github_commits" not in review and "note_gap_shipped" not in review
    assert "note it, never resolve on it" in check
    assert "never close one unasked" in scheduled_playbook("[scheduled] morning brief", "Robin")
    assert "call `resolve_gap`" in prompt and "`resolve_feedback`" in prompt
    config.fulla.commit = ""
    assert "Which commit of it you are running is not recorded here." in \
        manager_prompt(config, DirectiveStore(tmp_path / "d.json"), store=store)
    config.fulla.repo = ""
    assert "Your own code" not in manager_prompt(config, DirectiveStore(tmp_path / "d.json"), store=store)
    assert own_code("Robin", None, "", "abc") == ""
