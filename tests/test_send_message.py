"""`send_message`: the manager's mid-turn message to the owner. Registered on
the manager alone, refused for any other run, and delivered the way the final
reply is."""

import pytest

from fulla import runctx
from fulla.channel import ChannelState
from fulla.config import load_config
from fulla.directives import DirectiveStore
from fulla.tools import manager_tools, messages, send_message, social_tools
from fulla.wake import withheld_for
from fulla.worker import MANAGER_AGENT, build_agents, manager_prompt
from tests.test_channel import (
    MANAGER,
    SOCIAL,
    event,
    make,
    make_two,
    on_signal,
    on_telegram,
    wref,
)


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("FULLA_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FULLA_TELEGRAM_USER_ID", "7")
    monkeypatch.setenv("NORNS_API_KEY", "k")
    return load_config()


class as_agent:
    def __init__(self, agent_id, run_id):
        self.agent_id, self.run_id = agent_id, run_id

    def __enter__(self):
        self.tokens = (runctx.set_current_run(self.run_id), runctx.set_current_agent(self.agent_id))

    def __exit__(self, *exc):
        runctx.reset_current_agent(self.tokens[1])
        runctx.reset_current_run(self.tokens[0])


@pytest.fixture
def wired():
    """The tool wired to a channel's `say`, the manager known by id."""
    def wire(ch):
        messages.init(say=ch.say)
    runctx.manager_from(lambda agent_id: agent_id == MANAGER)
    yield wire
    messages.init(say=None)
    runctx.manager_from(None)


# ---------------------------------------------------------------- registration

def test_the_manager_holds_it_and_nobody_else_does(config, tmp_path):
    assert send_message in manager_tools and send_message not in social_tools
    agents = {a.name: a for a in build_agents(config, DirectiveStore(tmp_path / "d.json"))}
    assert "send_message" in agents[MANAGER_AGENT].allowed_tools
    for name, agent in agents.items():
        if name != MANAGER_AGENT:
            assert "send_message" not in agent.allowed_tools, name
            assert "send_message" not in {t.name for t in agent.tools}, name


def test_the_prompt_says_how_to_use_it(config, tmp_path):
    prompt = manager_prompt(config, DirectiveStore(tmp_path / "d.json"))
    assert "`send_message`" in prompt and "tmux session" in prompt


def test_the_quiet_scheduled_turns_are_not_offered_it_and_the_others_are():
    def turn(text):
        return [{"role": "user", "content": text}]
    assert "send_message" in withheld_for(MANAGER_AGENT, turn("[scheduled] nightly review"))
    assert "send_message" in withheld_for(MANAGER_AGENT, turn("[scheduled] shipping check"))
    for spoken in ("[scheduled] morning brief", "[scheduled] check-in", "[scheduled] question",
                   "[scheduled] follow-up: the PR", "how are my tmux sessions?"):
        assert "send_message" not in withheld_for(MANAGER_AGENT, turn(spoken)), spoken


# ---------------------------------------------------------------- owner-only

def test_a_social_run_is_refused_and_nothing_is_sent(tmp_path, wired):
    ch, tg, _ = make(tmp_path)
    wired(ch)
    with as_agent(SOCIAL, 9):
        assert send_message.handler("hi").startswith("Refused")
    assert tg.sent == []


def test_a_call_with_no_run_or_no_agent_is_refused(tmp_path, wired):
    ch, tg, _ = make(tmp_path)
    wired(ch)
    assert send_message.handler("hi").startswith("Refused")
    with as_agent(None, 9):
        assert send_message.handler("hi").startswith("Refused")
    assert tg.sent == []


def test_an_empty_message_is_refused(tmp_path, wired):
    ch, tg, _ = make(tmp_path)
    wired(ch)
    with as_agent(MANAGER, 9):
        assert send_message.handler("   ").startswith("Refused")
    assert tg.sent == []


# ---------------------------------------------------------------- delivery

def test_one_message_per_item_then_the_reply(tmp_path, wired):
    ch, tg, _ = make(tmp_path)
    wired(ch)
    ch.handle(on_telegram("how are my tmux sessions?"))
    with as_agent(MANAGER, 9):
        assert send_message.handler("build: tests passing") == "Sent."
        assert send_message.handler("deploy: waiting on review") == "Sent."
    ch.handle_event(event("run.completed", 9, output="That's all three."))
    texts = [t for t, _ in tg.sent]
    assert texts[1:] == ["build: tests passing", "deploy: waiting on review", "That's all three."]
    # Quotable and reactable, like any reply.
    assert ch.state.texts[wref(102)] == "build: tests passing"


def test_a_silent_reply_drops_only_itself(tmp_path, wired):
    ch, tg, _ = make(tmp_path)
    wired(ch)
    with as_agent(MANAGER, 12):  # a scheduled turn: nobody started it
        send_message.handler("Today: dentist at 3.")
    ch.handle_event(event("run.completed", 12, output="[silent]"))
    assert [t for t, _ in tg.sent] == ["Today: dentist at 3."]


def test_a_redelivered_call_is_sent_once_even_after_a_restart(tmp_path, wired):
    ch, tg, _ = make(tmp_path)
    wired(ch)
    with as_agent(MANAGER, 12):
        assert send_message.handler("one") == "Sent."
        assert send_message.handler("one") == "Already sent; not sent again."
    ch.state = ChannelState(tmp_path / "channel.json")
    with as_agent(MANAGER, 12):
        assert send_message.handler("one") == "Already sent; not sent again."
        assert send_message.handler("two") == "Sent."
    assert [t for t, _ in tg.sent] == ["one", "two"]


def test_it_goes_where_the_run_answers_on_signal_too(tmp_path, wired):
    ch, tg, sg, _ = make_two(tmp_path, primary="telegram")
    wired(ch)
    ch.handle(on_signal("and the sessions?", message_id=1700000000000))
    with as_agent(MANAGER, 9):
        assert send_message.handler("build: green") == "Sent."
    assert sg.sent == [("build: green", None)] and tg.sent == []
    with as_agent(MANAGER, 30):  # no origin: the primary address
        send_message.handler("morning")
    assert tg.sent == [("morning", None)]


def test_a_failed_send_says_so_and_is_not_counted(tmp_path, wired):
    ch, tg, _ = make(tmp_path)
    wired(ch)
    tg.down = True
    with as_agent(MANAGER, 12):
        assert send_message.handler("one").startswith("Not sent")
    tg.down = False
    with as_agent(MANAGER, 12):
        assert send_message.handler("one") == "Sent."
    assert tg.sent == [("one", None)]
