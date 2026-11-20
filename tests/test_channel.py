"""The channel with a fake Telegram and a fake Norns client."""

import json
from dataclasses import dataclass, field

import httpx
import pytest

from fulla.channel import CAPPED, UNATTENDED, Channel, ChannelState, NotTaken, split_ref
from fulla.groups import GroupStore
from fulla.norns_api import Agent, Busy, MessageResult, NornsApi, Run, WaitingFor
from fulla.people import PeopleStore
from fulla.telegram import as_reaction
from fulla.transport import Inbound

MANAGER, REMINDER, MAC_TASK, FOLLOWUP, READER, SOCIAL, GROUP_REMINDER, OTHER = 1, 2, 3, 4, 5, 6, 7, 9
STANDING = 1  # the owner's conversation id
SCHEDULED_REMINDERS = {42, 5}
QUEUED_MAC_TASKS = {77}


class FakeTelegram:
    name = "telegram"
    buttons = True

    def __init__(self):
        self.sent = []  # (text, buttons)
        self.edited = []
        self.edit_buttons = {}
        self.answered = []
        self.deleted = []
        self.actions = []
        self.reactions = []
        self.mentions = []  # (message id, [(user id, name)])
        self.reactable = True
        self.deletable = True
        self.down = False
        self._next_id = 100

    def send(self, chat_id, text, buttons=None, mentions=None, *, plain=False):
        if self.down:
            raise RuntimeError(f"{self.name} send: 502")
        self.sent.append((text, buttons))
        if mentions:
            self.mentions.append((self._next_id + 1, mentions))
        self._next_id += 1
        return self._next_id

    def edit(self, chat_id, message_id, text, buttons=None, *, plain=False):
        self.edited.append((message_id, text))
        self.edit_buttons[message_id] = buttons

    def answer_callback(self, callback_id, text=None):
        self.answered.append((callback_id, text))

    def delete(self, chat_id, message_id):
        self.deleted.append(message_id)
        return self.deletable

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append(action)

    def set_reaction(self, chat_id, message_id, emoji):
        if not self.reactable:
            raise RuntimeError("telegram setMessageReaction: REACTION_INVALID")
        self.reactions.append((message_id, emoji))

    def as_reaction(self, text):
        return as_reaction(text)


def run(run_id, status, *, agent_id=MANAGER, trigger="message", output=None, question=None,
        conversation_id=STANDING):
    return Run(
        run_id=run_id, status=status, output=output, agent_id=agent_id,
        trigger_type=trigger, inserted_at="2026-10-15T12:00:00Z",
        waiting_for=WaitingFor(question=question, tool_call_id=f"tc{run_id}") if question else None,
        conversation_id=conversation_id,
    )


def event(kind, run_id, *, agent_id=MANAGER, conversation_id=STANDING, **extra):
    return {"type": kind, "run_id": run_id, "agent_id": agent_id, "conversation_id": conversation_id,
            "trigger_type": "message", "status": kind.split(".")[1], **extra}


@dataclass
class FakeClient:
    runs: list = field(default_factory=list)
    replies: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    cancelled: list = field(default_factory=list)
    cancellable: bool = True
    busy: bool = False  # a Norns that refuses even an interjection (an older one)
    in_flight: int | None = None  # a run under way, which takes an interjection
    down: bool = False
    next_run: int = 9  # the run id send_message hands back
    events: dict = field(default_factory=dict)  # run_id -> its newest event type
    agents: list = field(default_factory=list)  # Agents beyond the fixed seven, such as a group's own
    refuse_agents: set = field(default_factory=set)  # name prefixes send_message answers 404 for

    def last_event_type(self, run_id):
        return self.events.get(run_id, "llm_request")

    def list_agents(self):
        ids = {"fulla": MANAGER, "fulla-reminder": REMINDER, "fulla-mac-task": MAC_TASK,
               "fulla-followup": FOLLOWUP, "fulla-reader": READER, "fulla-social": SOCIAL,
               "fulla-group-reminder": GROUP_REMINDER}
        return [Agent(id=i, name=n) for n, i in ids.items()] + list(self.agents)

    def list_runs(self, limit=50, status=None, agent_id=None):
        return self.runs

    def get_run(self, run_id):
        for r in self.runs:
            if r.run_id == run_id:
                return r
        raise RuntimeError("404")

    def cancel_run(self, run_id):
        self.cancelled.append(run_id)
        return self.cancellable

    def get_agent(self, name):
        for a in self.list_agents():
            if a.name == name:
                return a
        raise ValueError(f"Agent not found: {name}")

    def conversation_id(self, agent, key):
        assert key == "telegram:7"
        return STANDING

    def delete_conversation(self, agent, key):
        self.messages.append(("delete", agent, key))
        return True

    def reply(self, run_id, answer):
        self.replies.append((run_id, answer))

    def send_message(self, agent, content, *, conversation_key=None, wait=False, timeout=30,
                     interject=False):
        if self.in_flight is not None and interject:
            self.messages.append((agent, content, conversation_key, wait))
            return MessageResult(run_id=self.in_flight, status="accepted", output=None)
        if self.busy or self.in_flight is not None:
            raise Busy("agent is busy")
        if self.down:
            raise RuntimeError("connection refused")
        if any(agent.startswith(p) for p in self.refuse_agents):
            raise RuntimeError(f"404 Agent not found: {agent}")
        self.messages.append((agent, content, conversation_key, wait))
        return MessageResult(run_id=self.next_run, status="accepted", output=None)


def make(tmp_path, client=None):
    tg = FakeTelegram()
    client = client or FakeClient()
    ch = Channel({"telegram": tg}, client, ChannelState(tmp_path / "channel.json"),
                 owners={"telegram": 7}, primary="telegram", conversation_key="telegram:7",
                 reminder_known=lambda run_id: run_id in SCHEDULED_REMINDERS,
                 mac_task_known=lambda run_id: run_id in QUEUED_MAC_TASKS, typing_interval=0)
    return ch, tg, client


WORKING = ("Working…", [[("Cancel", "c:9")]])


def wref(message_id):
    """The ref of a message in the owner's Telegram chat."""
    return f"telegram:7#{message_id}"


def mid_of(r):
    return int(split_ref(r)[1])


def mids(ch):
    """The ids of the question messages the channel is tracking, oldest first."""
    return [mid_of(r) for r in ch.state.message_runs]


def say(ch, text="hello"):
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text=text))
    return mid_of(ch.state.working["9"])


def test_waiting_event_posts_question_with_reminder_buttons(tmp_path):
    ch, tg, _ = make(tmp_path)
    ch.handle_event(event("run.waiting", 42, agent_id=REMINDER, question="Did you call the dentist?",
                          tool_call_id="tc42"))
    assert tg.sent == [("Did you call the dentist?", [
        [("Done", "a:42:done"), ("Snooze 1h", "a:42:snooze 1 hour"), ("Snooze 1d", "a:42:snooze 1 day")],
        [("Drop it", "a:42:drop")],
    ])]


def test_button_tap_answers_the_run(tmp_path):
    ch, tg, client = make(tmp_path)
    ch.handle_event(event("run.waiting", 42, agent_id=REMINDER, question="Did you?", tool_call_id="tc42"))
    mid = mids(ch)[0]
    ch.handle(Inbound(kind="callback", user_id=7, chat_id=7, message_id=mid, callback_id="cb", data="a:42:done"))
    assert client.replies == [(42, "done")]
    assert tg.edited[0][1].endswith("→ done")
    assert tg.answered == [("cb", None)]
    assert ch.state.message_runs == {}


def test_duplicate_delivery_and_restart_post_once(tmp_path):
    ch, tg, client = make(tmp_path)
    ev = event("run.waiting", 5, question="Which dentist?", tool_call_id="tc5")
    ch.handle_event(ev)
    ch.handle_event(ev)
    assert len(tg.sent) == 1
    assert tg.sent[0][1] == [[("Yes", "a:5:yes"), ("No", "a:5:no")]]

    ch2, tg2, _ = make(tmp_path, client)  # restart with the same state file
    ch2.handle_event(ev)
    client.runs = [run(5, "waiting", question="Which dentist?")]
    ch2.reconcile()
    assert tg2.sent == []


def test_reconcile_posts_missed_questions_only(tmp_path):
    ch, tg, client = make(tmp_path)
    client.runs = [
        run(5, "waiting", question="Missed one?"),
        run(6, "waiting", question="Not ours", agent_id=OTHER),
        run(7, "completed", output="done"),
        run(8, "waiting", question="API test on another key", conversation_id=2),
        run(42, "waiting", question="Scheduled reminder", agent_id=REMINDER, conversation_id=50),
        run(43, "waiting", question="Reminder we never scheduled", agent_id=REMINDER, conversation_id=51),
    ]
    ch.reconcile()
    assert [t for t, _ in tg.sent] == ["Missed one?", "Scheduled reminder"]


def test_manager_runs_on_other_conversations_are_not_relayed(tmp_path):
    ch, tg, _ = make(tmp_path)
    ch.handle_event(event("run.completed", 30, output="api reply", conversation_id=2))
    ch.handle_event(event("run.waiting", 31, question="api question?", tool_call_id="tc31", conversation_id=2))
    ch.handle_event(event("run.completed", 32, output="no conversation", conversation_id=None))
    assert tg.sent == []
    ch.handle_event(event("run.completed", 33, output="mine", conversation_id=STANDING))
    assert tg.sent == [("mine", None)]


def test_reminder_runs_are_relayed_only_if_fulla_scheduled_them(tmp_path):
    ch, tg, _ = make(tmp_path)
    ch.handle_event(event("run.completed", 42, agent_id=REMINDER, conversation_id=50, output="Done: starter"))
    ch.handle_event(event("run.completed", 43, agent_id=REMINDER, conversation_id=51, output="Done: not ours"))
    assert tg.sent == [("Done: starter", None)]


def test_mac_task_runs_are_relayed_only_if_fulla_queued_them(tmp_path):
    ch, tg, _ = make(tmp_path)
    ch.handle_event(event("run.completed", 77, agent_id=MAC_TASK, conversation_id=60,
                          output="Done: added Test to Things"))
    ch.handle_event(event("run.completed", 78, agent_id=MAC_TASK, conversation_id=61, output="Done: not ours"))
    assert tg.sent == [("Done: added Test to Things", None)]


def test_mac_task_waits_are_internal_and_never_posted(tmp_path):
    ch, tg, client = make(tmp_path)
    ch.handle_event(event("run.waiting", 77, agent_id=MAC_TASK, conversation_id=60,
                          question="Waiting for the Mac.", tool_call_id="tc77"))
    client.runs = [run(77, "waiting", agent_id=MAC_TASK, question="Waiting for the Mac.", conversation_id=60)]
    ch.reconcile()
    assert tg.sent == [] and ch.state.posted == {}


def test_followup_runs_are_never_relayed_but_the_manager_turn_they_start_is(tmp_path):
    ch, tg, client = make(tmp_path)
    followup = FOLLOWUP
    ch.handle_event(event("run.completed", 88, agent_id=followup, conversation_id=70,
                          output="Delivered: check the build"))
    ch.handle_event(event("run.waiting", 88, agent_id=followup, conversation_id=70,
                          question="should not happen", tool_call_id="tc88"))
    client.runs = [run(88, "waiting", agent_id=followup, question="should not happen", conversation_id=70)]
    ch.reconcile()
    assert tg.sent == []
    ch.handle_event(event("run.completed", 89, output="The build finished green.", conversation_id=STANDING))
    ch.handle_event(event("run.completed", 90, output="[silent]", conversation_id=STANDING))
    assert tg.sent == [("The build finished green.", None)]


def test_an_unattended_ask_is_answered_once_and_never_posted(tmp_path):
    ch, tg, client = make(tmp_path)
    ev = event("run.waiting", 120, agent_id=READER, conversation_id=80,
               question="Which source do you prefer?", tool_call_id="tc120")
    ch.handle_event(ev)
    ch.handle_event(ev)  # Norns delivered it twice
    assert client.replies == [(120, UNATTENDED)]
    assert tg.sent == [] and ch.state.posted == {} and ch.state.parked is None

    ch2, tg2, _ = make(tmp_path, client)  # a restart remembers it was answered
    ch2.handle_event(ev)
    client.runs = [run(120, "waiting", agent_id=READER, question="Which source?", conversation_id=80)]
    ch2.reconcile()
    assert client.replies == [(120, UNATTENDED)] and tg2.sent == []

    # A second ask in the same run is a new question, and is answered too.
    ch2.handle_event(dict(ev, tool_call_id="tc121"))
    assert client.replies[-1] == (120, UNATTENDED) and len(client.replies) == 2


def test_a_question_from_a_scheduled_turn_is_answered_and_never_posted(tmp_path):
    # Runs 679 and 703: a check-in called ask_human, the owner was sent its
    # question, and the run sat parked until the next timer answered it.
    ch, tg, client = make(tmp_path)
    ev = dict(event("run.waiting", 150, question="placeholder", tool_call_id="tc150"), trigger_type="schedule")
    ch.handle_event(ev)
    ch.handle_event(ev)
    assert client.replies == [(150, UNATTENDED)]
    assert tg.sent == [] and ch.state.posted == {} and ch.state.parked is None

    # Found waiting on a cold start, the same.
    client.runs = [run(151, "waiting", trigger="schedule", question="placeholder")]
    ch.reconcile()
    assert client.replies == [(150, UNATTENDED), (151, UNATTENDED)] and tg.sent == []

    # What it goes on to say is relayed like any scheduled turn.
    ch.handle_event(dict(event("run.completed", 150, output="Drink some water."), trigger_type="schedule"))
    assert tg.sent == [("Drink some water.", None)]

    # A question in a turn the owner started is still theirs to answer.
    ch.handle_event(event("run.waiting", 152, question="Which dentist?", tool_call_id="tc152"))
    assert tg.sent[-1][0] == "Which dentist?" and ch.state.parked == 152


def test_reconcile_unsticks_unattended_runs_found_waiting(tmp_path):
    ch, tg, client = make(tmp_path)
    client.runs = [
        run(130, "waiting", agent_id=READER, question="?", conversation_id=81),
        run(131, "waiting", agent_id=FOLLOWUP, question="?", conversation_id=82),
        run(132, "completed", agent_id=READER, output="answer", conversation_id=83),
    ]
    ch.reconcile()
    assert client.replies == [(130, UNATTENDED), (131, UNATTENDED)]
    assert tg.sent == []


def test_an_unstick_norns_refuses_is_tried_again(tmp_path):
    ch, _, client = make(tmp_path)
    ev = event("run.waiting", 140, agent_id=READER, conversation_id=84, question="?", tool_call_id="tc140")
    fail = {"on": True}

    def reply(run_id, answer):
        if fail["on"]:
            raise RuntimeError("502")
        client.replies.append((run_id, answer))

    client.reply = reply
    ch.handle_event(ev)
    assert client.replies == [] and ch.state.unstuck == []
    fail["on"] = False
    ch.handle_event(ev)
    assert client.replies == [(140, UNATTENDED)]


def test_reply_to_question_message_answers_it(tmp_path):
    ch, _, client = make(tmp_path)
    ch.handle_event(event("run.waiting", 5, question="Which dentist?", tool_call_id="tc5"))
    mid = mids(ch)[0]
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="Dr. Lee", message_id=200, reply_to_message_id=mid))
    assert client.replies == [(5, "Dr. Lee")]
    assert client.messages == []


def test_plain_text_is_sent_and_shown_as_working(tmp_path):
    ch, tg, client = make(tmp_path)
    mid = say(ch, "mum's birthday is march 4")
    assert client.messages == [("fulla", "mum's birthday is march 4", "telegram:7", False)]
    assert tg.sent == [WORKING]  # the reply itself comes back as a run.completed event
    assert tg.actions == ["typing"]
    assert ch.state.working == {"9": wref(mid)}


def test_a_message_delivered_twice_is_handled_once(tmp_path):
    ch, tg, client = make(tmp_path)
    msg = Inbound(kind="text", user_id=7, chat_id=7, text="milk", message_id=500)
    ch.handle(msg)
    ch.handle(msg)  # Telegram retrying a webhook, or the Signal forwarder retrying a lost reply
    assert len(client.messages) == 1
    assert tg.sent == [WORKING]
    # and it survives a restart, since the retry may land on a fresh process
    ch2, _, _ = make(tmp_path, client)
    ch2.handle(msg)
    assert len(client.messages) == 1


def test_completed_event_takes_the_working_message_down_first(tmp_path):
    ch, tg, _ = make(tmp_path)
    mid = say(ch)
    ch.handle_event(event("run.completed", 9, output="Remembered #1."))
    assert tg.deleted == [mid]
    assert tg.sent[-1] == ("Remembered #1.", None)
    assert ch.state.working == {}


def test_working_message_is_edited_when_it_cannot_be_deleted(tmp_path):
    ch, tg, _ = make(tmp_path)
    tg.deletable = False
    mid = say(ch)
    ch.handle_event(event("run.waiting", 9, question="Which one?", tool_call_id="tc9"))
    assert tg.edited == [(mid, "Done.")]
    assert tg.sent[-1][0] == "Which one?"
    assert ch.state.working == {}


def test_failed_run_takes_the_working_message_down_too(tmp_path):
    ch, tg, _ = make(tmp_path)
    mid = say(ch)
    ch.handle_event(event("run.failed", 9, error="LLM timed out"))
    assert tg.deleted == [mid]
    assert tg.sent[-1][0].startswith("Run 9 failed")


def test_cancel_button_stops_the_run_and_silences_its_outcome(tmp_path):
    ch, tg, client = make(tmp_path)
    mid = say(ch)
    ch.handle(Inbound(kind="callback", user_id=7, chat_id=7, message_id=mid, callback_id="cb", data="c:9"))
    assert client.cancelled == [9]
    assert tg.edited == [(mid, "Cancelled.")]
    assert tg.answered == [("cb", "Cancelled")]
    assert ch.state.working == {}
    ch.handle_event(event("run.failed", 9, error="Cancelled by user"))
    ch.handle_event(event("run.completed", 9, output="too late"))
    assert tg.sent == [WORKING]


def test_cancel_norns_refuses_leaves_the_working_message(tmp_path):
    ch, tg, client = make(tmp_path)
    client.cancellable = False
    mid = say(ch)
    ch.handle(Inbound(kind="callback", user_id=7, chat_id=7, message_id=mid, callback_id="cb", data="c:9"))
    assert tg.answered[0][1].startswith("Norns wouldn't cancel")
    assert tg.edited == [] and ch.state.working == {"9": wref(mid)}


def test_outcome_that_beats_the_working_message_suppresses_it(tmp_path):
    ch, tg, _ = make(tmp_path)
    ch.handle_event(event("run.completed", 9, output="quick"))
    ch._start_working(9)
    assert tg.sent == [("quick", None)] and ch.state.working == {}


def test_answering_a_manager_question_shows_working_but_a_reminder_does_not(tmp_path):
    ch, tg, _ = make(tmp_path)
    ch.handle_event(event("run.waiting", 9, question="Which dentist?", tool_call_id="tc9"))
    ch.handle_event(event("run.waiting", 42, agent_id=REMINDER, question="Did you?", tool_call_id="tc42"))
    manager_mid, reminder_mid = mids(ch)
    ch.handle(Inbound(kind="callback", user_id=7, chat_id=7, message_id=reminder_mid, callback_id="cb1",
                      data="a:42:done"))
    assert ch.state.working == {}
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="Dr. Lee", message_id=300,
                      reply_to_message_id=manager_mid))
    assert tg.sent[-1] == WORKING and "9" in ch.state.working
    assert ch.state.question_agents == {}


def test_working_runs_survive_a_restart_and_reconcile_clears_finished_ones(tmp_path):
    ch, _, client = make(tmp_path)
    mid = say(ch)
    ch2, tg2, _ = make(tmp_path, client)
    assert ch2.state.working == {"9": wref(mid)}
    client.runs = [run(9, "running")]
    ch2.reconcile()
    assert ch2.state.working == {"9": wref(mid)} and tg2.deleted == []
    client.runs = [run(9, "completed", output="done while asleep")]
    ch2.reconcile()
    assert tg2.deleted == [mid] and ch2.state.working == {}


def test_completed_event_relays_output_once(tmp_path):
    ch, tg, _ = make(tmp_path)
    ev = event("run.completed", 9, output="Remembered #1.")
    ch.handle_event(ev)
    ch.handle_event(ev)
    assert tg.sent == [("Remembered #1.", None)]


def test_silent_and_foreign_outputs_are_not_relayed(tmp_path):
    ch, tg, _ = make(tmp_path)
    ch.handle_event(event("run.completed", 10, output="[silent]"))
    ch.handle_event(event("run.completed", 11, output="  "))
    # A work log with the marker as its last line was never for the owner either.
    ch.handle_event(event("run.completed", 13, output="Stored one fact. Nothing to flag.\n\n[silent]"))
    ch.handle_event(event("run.completed", 14, output="Nothing to note.\n[SILENT] "))
    # So was a note with the marker at the end of its sentence (run 654, 2026-10-27).
    ch.handle_event(event("run.completed", 15, output="Already flagged the gym window. Nothing new here. [silent]"))
    ch.handle_event(event("run.completed", 12, output="not for telegram", agent_id=OTHER))
    ch.handle_event(event("run.test", None, agent_id=None))
    assert tg.sent == []


def test_failed_event_posts_a_pointer(tmp_path):
    ch, tg, _ = make(tmp_path)
    ch.handle_event(event("run.failed", 42, agent_id=REMINDER, conversation_id=50, error="LLM timed out"))
    assert tg.sent[0][0].startswith("Run 42 failed: LLM timed out")


def test_reset_deletes_the_standing_conversation_and_forgets_its_id(tmp_path):
    ch, tg, client = make(tmp_path)
    assert ch._standing_conversation_id() == STANDING
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="/reset"))
    assert client.messages == [("delete", "fulla", "telegram:7")]
    assert tg.sent[-1][0].startswith("Fresh start.")
    assert ch._conversation_id is None  # re-resolved on the next event, not stale


QUEUED = ("Still on the last thing; I'll pass this on when it's done.", None)


def test_busy_messages_are_kept_and_sent_together_when_the_run_ends(tmp_path):
    ch, tg, client = make(tmp_path)
    client.busy = True
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="also, milk"))
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="and eggs"))
    assert tg.sent == [QUEUED]  # told once, not per message
    assert ch.state.pending == ["also, milk", "and eggs"]
    client.busy = False
    ch.handle_event(event("run.completed", 8, output="Here's your brief."))
    assert client.messages == [("fulla", "also, milk\n\nand eggs", "telegram:7", False)]
    assert tg.sent[1:] == [("Here's your brief.", None), WORKING]
    assert ch.state.pending == [] and not ch.state.queued_told


def test_a_message_while_a_run_works_goes_into_that_run_without_a_notice(tmp_path):
    ch, tg, client = make(tmp_path)
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="link me draft PRs"))
    assert tg.sent == [WORKING]
    client.in_flight = 9
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="also restart bubbo"))
    assert [c for _, c, _, _ in client.messages] == ["link me draft PRs", "also restart bubbo"]
    assert tg.sent == [WORKING]  # no queued notice, no second Working…
    assert ch.state.pending == [] and not ch.state.queued_told
    client.in_flight = None
    ch.handle_event(event("run.completed", 9, output="Two drafts; bubbo restarted."))
    assert tg.sent[-1] == ("Two drafts; bubbo restarted.", None)


def test_still_busy_keeps_them_pending_without_saying_so_again(tmp_path):
    ch, tg, client = make(tmp_path)
    client.busy = True
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="one"))
    ch.handle_event(event("run.failed", 8, error="LLM timed out"))
    assert ch.state.pending == ["one"] and client.messages == []
    assert [t for t, _ in tg.sent].count(QUEUED[0]) == 1


def test_only_the_manager_ending_a_run_sends_pending(tmp_path):
    ch, _, client = make(tmp_path)
    client.busy = True
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="one"))
    client.busy = False
    ch.handle_event(event("run.completed", 42, agent_id=REMINDER, conversation_id=50, output="Done: x"))
    assert ch.state.pending == ["one"]


def test_pending_survives_a_restart_and_reconcile_sends_it(tmp_path):
    ch, _, client = make(tmp_path)
    client.busy = True
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="one"))
    client.busy = False
    ch2, tg2, _ = make(tmp_path, client)
    assert ch2.state.pending == ["one"]
    ch2.reconcile()
    assert client.messages == [("fulla", "one", "telegram:7", False)]
    assert ch2.state.pending == [] and tg2.sent == [WORKING]


def test_pending_is_not_sent_as_the_answer_to_a_question(tmp_path):
    ch, _, client = make(tmp_path)
    client.busy = True
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="one"))
    client.busy = False
    ch.handle_event(event("run.waiting", 8, question="Which dentist?", tool_call_id="tc8"))
    assert ch.state.parked == 8 and client.messages == []
    client.runs = [run(8, "waiting", question="Which dentist?")]
    ch.reconcile()
    assert client.messages == []
    mid = mids(ch)[0]
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="Dr. Lee", message_id=300, reply_to_message_id=mid))
    assert client.replies == [(8, "Dr. Lee")] and ch.state.parked is None
    ch.handle_event(event("run.completed", 8, output="Booked."))
    assert client.messages == [("fulla", "one", "telegram:7", False)]


def test_plain_text_while_parked_still_goes_alone(tmp_path):
    ch, _, client = make(tmp_path)
    ch.handle_event(event("run.waiting", 9, question="Which dentist?", tool_call_id="tc9"))
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="Dr. Lee"))
    assert client.messages == [("fulla", "Dr. Lee", "telegram:7", False)]
    assert ch.state.pending == [] and ch.state.parked is None  # Norns resumed run 9 with it


def test_unreachable_norns_keeps_the_message(tmp_path):
    ch, tg, client = make(tmp_path)
    client.down = True
    with pytest.raises(NotTaken):
        ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="one"))
    assert tg.sent[0][0].startswith("I couldn't reach Norns")
    client.down = False
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="two"))
    assert client.messages == [("fulla", "one\n\ntwo", "telegram:7", False)]


def test_unreachable_norns_asks_the_messenger_to_bring_the_message_again(tmp_path):
    ch, tg, client = make(tmp_path)
    client.down = True
    msg = Inbound(kind="text", user_id=7, chat_id=7, text="one", message_id=600)
    with pytest.raises(NotTaken):
        ch.handle(msg)
    assert ch.state.pending == ["one"]  # kept on disk meanwhile
    with pytest.raises(NotTaken):
        ch.handle(msg)  # the redelivery is the retry; still down, still kept once
    assert ch.state.pending == ["one"]
    assert [t for t, _ in tg.sent] == ["I couldn't reach Norns: connection refused. I've kept your message and will keep trying."]
    client.down = False
    ch.handle(msg)  # the next redelivery gets it through, as one message
    assert client.messages == [("fulla", "one", "telegram:7", False)]
    assert ch.state.pending == []
    ch.handle(msg)  # and a later one is just ignored
    assert len(client.messages) == 1


def test_unreachable_norns_while_parked_is_not_taken_and_told_once(tmp_path):
    ch, tg, client = make(tmp_path)
    ch.handle_event(event("run.waiting", 9, question="Which dentist?", tool_call_id="tc9"))
    tg.sent.clear()
    client.down = True
    msg = Inbound(kind="text", user_id=7, chat_id=7, text="Dr. Lee", message_id=601)
    with pytest.raises(NotTaken):
        ch.handle(msg)
    with pytest.raises(NotTaken):
        ch.handle(msg)
    assert ch.state.pending == [] and "601" not in str(ch.state.handled)
    assert len(tg.sent) == 1 and tg.sent[0][0].startswith("I couldn't reach Norns")
    client.down = False
    ch.handle(msg)
    assert client.messages == [("fulla", "Dr. Lee", "telegram:7", False)]
    assert ch.state.parked is None


def test_norns_api_raises_busy_on_409():
    def handler(request):
        if request.url.path == "/api/v1/agents":
            return httpx.Response(200, json={"data": [{"id": 1, "name": "fulla"}]})
        return httpx.Response(409, json={"error": "agent is busy"})

    api = NornsApi("http://norns", "k")
    api._http = httpx.Client(base_url="http://norns", transport=httpx.MockTransport(handler))
    with pytest.raises(Busy):
        api.send_message("fulla", "hi", conversation_key="telegram:7")


# --- reactions -----------------------------------------------------------------


def react(ch, message_id, emoji, date=1000):
    ch.handle(Inbound(kind="reaction", user_id=7, chat_id=7, text=emoji, message_id=message_id, date=date))


def test_reaction_on_a_reminder_answers_it_like_its_button(tmp_path):
    ch, tg, client = make(tmp_path)
    ch.handle_event(event("run.waiting", 42, agent_id=REMINDER, question="Did you?", tool_call_id="tc42"))
    ch.handle_event(event("run.waiting", 5, agent_id=REMINDER, question="Did you too?", tool_call_id="tc5"))
    first, second = mids(ch)
    react(ch, first, "🔥")
    assert client.replies == [] and tg.edited == []
    react(ch, first, "👌")
    react(ch, second, "😴")
    assert client.replies == [(42, "done"), (5, "snooze 1 hour")]
    assert tg.edited == [(first, "Did you?\n\n→ done"), (second, "Did you too?\n\n→ snooze 1 hour")]
    assert ch.state.message_runs == {} and client.messages == []


def test_reaction_on_another_question_is_yes_or_no(tmp_path):
    ch, _, client = make(tmp_path)
    ch.handle_event(event("run.waiting", 5, question="Book it?", tool_call_id="tc5"))
    mid = mids(ch)[0]
    react(ch, mid, "😴")
    react(ch, mid, "👎")
    assert client.replies == [(5, "no")]


def test_reaction_on_a_relayed_message_goes_to_the_manager_once(tmp_path):
    ch, tg, client = make(tmp_path)
    ch.handle_event(event("run.completed", 33, output="Gym window at 5?"))
    mid = 101
    react(ch, mid, "👎")
    react(ch, mid, "👎")  # Telegram redelivered it
    ch2, _, _ = make(tmp_path, client)
    react(ch2, mid, "👎")
    assert client.messages == [
        ("fulla", '[reaction] 👎 on your message: "Gym window at 5?"', "telegram:7", False)]
    assert ch.state.working == {} and tg.sent == [("Gym window at 5?", None)]
    react(ch2, mid, "👎", date=2000)  # taken back and given again
    assert len(client.messages) == 2


def test_reaction_quotes_a_long_message_clipped(tmp_path):
    ch, _, client = make(tmp_path)
    ch.handle_event(event("run.completed", 33, output="x" * 500))
    react(ch, 101, "👍")
    sent = client.messages[0][1]
    assert sent.endswith('…"') and len(sent) < 300


def test_reaction_on_an_unknown_message_is_ignored(tmp_path):
    ch, tg, client = make(tmp_path)
    react(ch, 55, "👍")
    assert client.messages == [] and client.replies == [] and tg.sent == []


def test_a_one_emoji_reply_reacts_to_the_owners_message(tmp_path):
    ch, tg, _ = make(tmp_path)
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="bought the milk", message_id=300))
    ch.handle_event(event("run.completed", 9, output="👍"))
    assert tg.reactions == [(300, "👍")]
    assert tg.sent == [WORKING] and ch.state.prompts == {}


def test_emoji_replies_fall_back_to_text(tmp_path):
    ch, tg, _ = make(tmp_path)
    ch.handle_event(event("run.completed", 33, output="👍"))  # scheduled: no message to react to
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="done", message_id=300))
    ch.handle_event(event("run.completed", 9, output="✅"))  # not one Telegram allows
    assert tg.reactions == []
    assert [t for t, _ in tg.sent] == ["👍", "Working…", "✅"]


def test_a_refused_reaction_is_sent_as_text(tmp_path):
    ch, tg, _ = make(tmp_path)
    tg.reactable = False
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="done", message_id=300))
    ch.handle_event(event("run.completed", 9, output="❤️"))
    assert tg.sent[-1] == ("❤️", None)


def test_a_busy_batch_reacts_to_its_newest_message_and_a_lone_reaction_stays_quiet(tmp_path):
    ch, tg, client = make(tmp_path)
    ch.handle_event(event("run.completed", 33, output="Gym window at 5?"))  # message 101
    client.busy = True
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="one", message_id=300))
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="two", message_id=301))
    client.busy = False
    ch.handle_event(event("run.completed", 8, output="Here's your brief."))
    assert ch.state.prompts == {"9": wref(301)} and tg.sent[-1] == WORKING
    ch.handle_event(event("run.completed", 9, output="👍"))
    assert tg.reactions == [(301, "👍")]
    client.busy = True
    react(ch, 101, "👎")
    client.busy = False
    ch.handle_event(event("run.completed", 10, output="ok"))
    assert client.messages[-1][1].startswith("[reaction] 👎")
    assert ch.state.working == {} and tg.sent[-1] == ("ok", None)


def test_a_reaction_while_parked_waits_instead_of_answering_the_question(tmp_path):
    ch, tg, client = make(tmp_path)
    ch.handle_event(event("run.completed", 33, output="Gym window at 5?"))  # message 101
    ch.handle_event(event("run.waiting", 8, question="Which dentist?", tool_call_id="tc8"))
    react(ch, 101, "👎")
    react(ch, 101, "🔥")  # a second one: held too, and told about only once
    assert client.messages == [] and client.replies == []
    assert ch.state.pending == ['[reaction] 👎 on your message: "Gym window at 5?"',
                                '[reaction] 🔥 on your message: "Gym window at 5?"']
    assert tg.sent[-1] == ('I\'ll pass that on after you answer: "Which dentist?"', None)
    mid = mids(ch)[0]
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="Dr. Lee", message_id=300, reply_to_message_id=mid))
    assert client.replies == [(8, "Dr. Lee")] and client.messages == []
    ch.handle_event(event("run.completed", 8, output="Booked."))
    both = '[reaction] 👎 on your message: "Gym window at 5?"\n\n[reaction] 🔥 on your message: "Gym window at 5?"'
    assert client.messages == [("fulla", both, "telegram:7", False)]
    assert ch.state.pending == [] and ch.state.working == {} and not ch.state.queued_told
    assert [t for t, _ in tg.sent] == ["Gym window at 5?", "Which dentist?",
                                       'I\'ll pass that on after you answer: "Which dentist?"',
                                       "Working…", "Booked."]


def test_a_held_reaction_without_the_question_text_gets_the_plain_notice(tmp_path):
    ch, tg, _ = make(tmp_path)
    ch.handle_event(event("run.completed", 33, output="Gym window at 5?"))
    ch.handle_event(event("run.waiting", 8, question="Which dentist?", tool_call_id="tc8"))
    ch.state.questions.clear()  # the question's message fell out of state
    react(ch, 101, "👎")
    assert tg.sent[-1] == ("I'll pass that on after you answer my question.", None)


def test_reply_saying_cancel_to_the_working_message_cancels(tmp_path):
    """Signal has no buttons: a quoted reply to Working… saying cancel is the Cancel button."""
    ch, tg, client = make(tmp_path)
    mid = say(ch)
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="Cancel", reply_to_message_id=mid))
    assert client.cancelled == [9]
    assert tg.edited == [(mid, "Cancelled.")]
    assert tg.answered == [] and len(tg.sent) == 1
    assert client.messages[-1][1] == "hello"  # the reply was not chat


def test_a_bare_yes_with_nothing_open_is_chat(tmp_path):
    ch, _tg, client = make(tmp_path)
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="ok"))
    assert client.messages[-1][1] == "ok"


def test_reply_to_the_working_message_that_is_not_cancel_is_chat(tmp_path):
    ch, _tg, client = make(tmp_path)
    mid = say(ch)
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="also this", reply_to_message_id=mid))
    assert client.cancelled == []
    assert client.messages[-1][1] == "also this"


def test_cancel_reaction_on_the_working_message_cancels(tmp_path):
    ch, tg, client = make(tmp_path)
    mid = say(ch)
    ch.handle(Inbound(kind="reaction", user_id=7, chat_id=7, text="❌", message_id=mid, date=1))
    assert client.cancelled == [9]
    assert tg.edited == [(mid, "Cancelled.")]


def test_cancel_refused_without_a_button_is_said_in_a_message(tmp_path):
    ch, tg, _client = make(tmp_path, FakeClient(cancellable=False))
    mid = say(ch)
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="stop", reply_to_message_id=mid))
    assert tg.answered == []
    assert tg.sent[-1][0].startswith("Norns wouldn't cancel")


def test_conversation_key_can_be_given(tmp_path):
    class Client(FakeClient):
        def conversation_id(self, agent, key):
            assert key == "owner"
            return STANDING

    tg = FakeTelegram()
    ch = Channel({"telegram": tg}, Client(), ChannelState(tmp_path / "c.json"), owners={"telegram": 7},
                 primary="telegram", conversation_key="owner", typing_interval=0)
    say(ch)
    assert ch.conversation_key == "owner"
    ch.handle_event(event("run.completed", 9, output="hi"))
    assert tg.sent[-1][0] == "hi"


# --- two messengers at once ----------------------------------------------------


class FakeSignal(FakeTelegram):
    name = "signal"
    buttons = False


OWNER_NUMBER = "+16045550100"


def make_two(tmp_path, client=None, primary="signal"):
    """Telegram and Signal together, Signal primary, the standing conversation
    still keyed telegram:7 (the fake client insists)."""
    tg, sg = FakeTelegram(), FakeSignal()
    client = client or FakeClient()
    ch = Channel({"telegram": tg, "signal": sg}, client, ChannelState(tmp_path / "channel.json"),
                 owners={"telegram": 7, "signal": OWNER_NUMBER}, primary=primary, conversation_key="telegram:7",
                 reminder_known=lambda run_id: run_id in SCHEDULED_REMINDERS, typing_interval=0)
    return ch, tg, sg, client


def on_signal(text, message_id=None, **extra):
    return Inbound(kind="text", transport="signal", user_id=OWNER_NUMBER, chat_id=OWNER_NUMBER, text=text,
                   message_id=message_id, **extra)


def on_telegram(text, message_id=None, **extra):
    return Inbound(kind="text", transport="telegram", user_id=7, chat_id=7, text=text, message_id=message_id, **extra)


def test_each_message_is_answered_on_the_app_it_came_from(tmp_path):
    ch, tg, sg, client = make_two(tmp_path)
    ch.handle(on_telegram("what's on today?", message_id=300))
    assert tg.sent == [WORKING] and sg.sent == []
    ch.handle_event(event("run.completed", 9, output="Two meetings."))
    assert tg.sent[-1] == ("Two meetings.", None) and sg.sent == []
    client.next_run = 10
    ch.handle(on_signal("and tomorrow?", message_id=1700000000000))
    assert sg.sent == [] and sg.actions == ["typing"] and len(tg.sent) == 2  # Signal: typing, no Working… message
    ch.handle_event(event("run.completed", 10, output="Nothing."))
    assert sg.sent[-1] == ("Nothing.", None) and len(tg.sent) == 2
    # one conversation behind both
    assert [m[2] for m in client.messages] == ["telegram:7", "telegram:7"]


def test_what_nobody_asked_for_goes_to_the_primary(tmp_path):
    ch, tg, sg, _ = make_two(tmp_path, primary="signal")
    ch.handle_event(event("run.completed", 33, output="Morning. Two meetings today."))  # a scheduled turn
    ch.handle_event(event("run.waiting", 42, agent_id=REMINDER, question="Call the dentist?", tool_call_id="tc42"))
    assert [t for t, _ in sg.sent] == ["Morning. Two meetings today.", "Call the dentist?"]
    assert tg.sent == []


def test_a_question_follows_the_owner_to_where_they_answered(tmp_path):
    ch, tg, sg, _ = make_two(tmp_path, primary="signal")
    ch.handle(on_telegram("book the dentist", message_id=300))
    ch.handle_event(event("run.waiting", 9, question="Which dentist?", tool_call_id="tc9"))
    assert tg.sent[-1][0] == "Which dentist?" and sg.sent == []
    mid = mids(ch)[0]
    ch.handle(on_telegram("Dr. Lee", message_id=301, reply_to_message_id=mid))
    assert tg.sent[-1] == WORKING  # the run carries on where it was answered, not on the primary
    ch.handle_event(event("run.waiting", 9, question="Morning or afternoon?", tool_call_id="tc9b"))
    assert tg.sent[-1][0] == "Morning or afternoon?" and sg.sent == []


def test_message_ids_do_not_collide_across_messengers(tmp_path):
    ch, _tg, sg, client = make_two(tmp_path, primary="signal")
    ch.handle_event(event("run.waiting", 42, agent_id=REMINDER, question="Did you?", tool_call_id="tc42"))
    assert sg.sent[0][0] == "Did you?"
    mid = mids(ch)[0]  # a Signal message id
    # the same number as a Telegram message id names nothing
    ch.handle(Inbound(kind="reaction", transport="telegram", user_id=7, chat_id=7, text="👍", message_id=mid, date=1))
    assert client.replies == []
    ch.handle(Inbound(kind="reaction", transport="signal", user_id=OWNER_NUMBER, chat_id=OWNER_NUMBER,
                      text="👍", message_id=mid, date=1))
    assert client.replies == [(42, "done")]


def test_a_busy_batch_is_answered_where_its_newest_message_came_from(tmp_path):
    ch, tg, sg, client = make_two(tmp_path)
    client.busy = True
    ch.handle(on_telegram("one", message_id=300))
    ch.handle(on_signal("two", message_id=1700000000000))
    assert [t for t, _ in tg.sent] == [QUEUED[0]] and sg.sent == []  # told once, where the first came from
    client.busy = False
    ch.handle_event(event("run.completed", 8, output="Brief."))
    assert sg.sent == [("Brief.", None)] and "9" in ch.state.working  # the brief had no origin: primary; the batch answers on Signal
    ch.handle_event(event("run.completed", 9, output="Both noted."))
    assert sg.sent[-1] == ("Both noted.", None)


def test_an_inbound_that_does_not_say_its_transport_needs_there_to_be_one(tmp_path):
    ch, _tg, _sg, _ = make_two(tmp_path)
    with pytest.raises(ValueError):
        ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="hi"))


def test_a_state_file_from_before_addresses_keeps_what_it_safely_can(tmp_path):
    (tmp_path / "channel.json").write_text(json.dumps({
        "posted": {"5:tc5": 101}, "message_runs": {"101": 5}, "working": {"9": 102},
        "relayed": [3, 4], "unstuck": ["7:tc7"], "pending": ["one"], "parked": 5,
    }))
    ch, _, _ = make(tmp_path)
    assert ch.state.relayed == [3, 4] and ch.state.unstuck == ["7:tc7"] and ch.state.pending == ["one"]
    assert ch.state.parked == 5
    assert ch.state.posted == {} and ch.state.message_runs == {} and ch.state.working == {}
    ch.state.save()
    assert json.loads((tmp_path / "channel.json").read_text())["version"] == 2


# --- friends and groups ---------------------------------------------------------


SAM = "+16045550111"


def make_people(tmp_path, client=None, primary="signal", cap=3):
    tg, sg = FakeTelegram(), FakeSignal()
    people = PeopleStore(tmp_path / "people.json")
    client = client or FakeClient()
    ch = Channel({"telegram": tg, "signal": sg}, client, ChannelState(tmp_path / "channel.json"),
                 owners={"telegram": 7, "signal": OWNER_NUMBER}, primary=primary, conversation_key="telegram:7",
                 people=people, owner_name="Robin", social_cap=cap, typing_interval=0)
    return ch, tg, sg, client, people


def from_sam(text, message_id=None, **extra):
    return Inbound(kind="text", transport="signal", user_id=SAM, chat_id=SAM, text=text, message_id=message_id,
                   sender_name="Sam", **extra)


def in_group(sender, text, message_id, **extra):
    return Inbound(kind="text", transport="signal", user_id=sender, chat_id="group:abc", group=True,
                   chat_title="Friends", text=text, message_id=message_id, **extra)


def social_done(run_id, output):
    return event("run.completed", run_id, agent_id=SOCIAL, conversation_id=77, output=output)


def test_a_stranger_is_dropped_and_a_listed_friend_gets_the_social_agent(tmp_path):
    ch, tg, sg, client, people = make_people(tmp_path)
    ch.handle(from_sam("hey fulla", message_id=1))
    assert client.messages == [] and sg.sent == []
    people.allow_person("Sam", {"signal": SAM})
    ch.handle(from_sam("hey fulla", message_id=2))
    agent, content, key, _ = client.messages[0]
    assert agent == "fulla-social" and key == f"social:signal:{SAM}"
    assert content.startswith("[Untrusted message from Sam, who is not the owner") and "\nSam: hey fulla\n" in content
    assert sg.sent == [] and sg.actions == ["typing"]  # no Working… message for friends
    ch.handle_event(social_done(9, "Hey Sam."))
    assert sg.sent == [("Hey Sam.", None)] and tg.sent == []
    ch.handle_event(social_done(9, "Hey Sam."))  # delivered twice
    assert len(sg.sent) == 1


def test_the_owner_is_asked_privately_about_a_new_group_and_a_tap_allows_it(tmp_path):
    ch, tg, sg, client, people = make_people(tmp_path, primary="telegram")
    ch.handle(in_group(OWNER_NUMBER, "Fulla, hi", 5))
    assert client.messages == [] and sg.sent == []
    assert tg.sent[0][0].startswith("You added me to Friends on signal.")
    tag = ch._group_tag("signal:group:abc")
    assert tg.sent[0][1] == [[("Yes", f"g:{tag}:yes"), ("No", f"g:{tag}:no")]]
    ch.handle(in_group(OWNER_NUMBER, "Fulla?", 6))  # asked once
    assert len(tg.sent) == 1 and not people.group("signal:group:abc").allowed
    ch.handle(Inbound(kind="callback", transport="telegram", user_id=7, chat_id=7, message_id=101, callback_id="cb",
                      data=f"g:{tag}:yes"))
    assert people.group("signal:group:abc").allowed
    assert tg.edited[0][1].endswith("→ I'll talk in Friends when addressed.")
    assert tg.answered == [("cb", "I'll talk in Friends when addressed.")]
    # the owner's own words in the group go to the social agent, as the owner
    ch.handle(in_group(OWNER_NUMBER, "Fulla, what's the capital of Peru?", 7))
    agent, content, key, _ = client.messages[0]
    assert agent == "fulla-social" and key == "social:signal:group:abc"
    assert content == "[In the group Friends, addressed to you]\nRobin (the owner): Fulla, what's the capital of Peru?"
    ch.handle_event(social_done(9, "Lima."))
    assert sg.sent == [("Lima.", None)]


def test_the_group_question_can_be_answered_by_replying_without_buttons(tmp_path):
    ch, _tg, sg, client, people = make_people(tmp_path, primary="signal")
    ch.handle(in_group(OWNER_NUMBER, "Fulla, hi", 5))
    assert sg.sent[0][0].startswith("You added me to Friends")  # asked where the primary is
    ch.handle(on_signal("no", message_id=1700000000001, reply_to_message_id=101))
    assert people.group("signal:group:abc").allowed is False
    assert sg.edited[0][1].endswith("→ I'll stay quiet in Friends.") and client.messages == []
    ch.handle(in_group(OWNER_NUMBER, "Fulla?", 6))
    assert client.messages == [] and len(sg.sent) == 1  # not asked again either


def test_in_a_group_only_listed_people_are_heard_and_only_when_they_address_her(tmp_path):
    ch, tg, sg, client, people = make_people(tmp_path)
    people.remember_group("signal:group:abc", "Friends")
    people.allow_group("signal:group:abc")
    ch.handle(in_group(SAM, "Fulla, hi", 1))  # not on the list: the owner is asked, Sam is not answered
    assert client.messages == [] and sg.sent[0][0].startswith("In Friends, ")
    people.allow_person("Sam", {"signal": SAM})
    ch.handle(in_group(SAM, "anyone up for dinner?", 2))  # not addressed to Fulla
    assert client.messages == []
    ch.handle(in_group(SAM, "fulla - pick a place", 3))
    assert len(client.messages) == 1 and client.messages[0][2] == "social:signal:group:abc"
    ch.handle_event(social_done(9, "Sushi."))
    assert sg.sent[-1] == ("Sushi.", None)
    sushi = sg._next_id
    client.next_run = 10
    ch.handle(in_group(SAM, "why?", 4, reply_to_message_id=sushi))  # a reply to Fulla counts
    assert len(client.messages) == 2
    ch.handle(Inbound(kind="reaction", transport="signal", user_id=SAM, chat_id="group:abc", group=True,
                      text="👍", message_id=sushi, date=1))  # reactions in groups mean nothing
    assert len(client.messages) == 2 and tg.sent == []


def test_a_conversation_hits_its_daily_cap_and_is_told_once(tmp_path):
    ch, _tg, sg, client, people = make_people(tmp_path, cap=3)
    people.allow_person("Sam", {"signal": SAM})
    for i in range(1, 6):
        client.next_run = i
        ch.handle(from_sam(f"q{i}", message_id=i))
        ch.handle_event(social_done(i, f"a{i}"))
    assert [t for t, _ in sg.sent] == ["a1", "a2", "a3", CAPPED]
    assert len(client.messages) == 3


def test_a_friends_messages_wait_while_a_run_is_in_flight_and_go_together_after(tmp_path):
    ch, _tg, _sg, client, people = make_people(tmp_path)
    people.allow_person("Sam", {"signal": SAM})
    client.busy = True
    ch.handle(from_sam("one", message_id=1))
    ch.handle(from_sam("two", message_id=2))
    assert client.messages == [] and len(ch.state.social_pending[f"signal:{SAM}"]) == 2
    client.busy = False
    ch.state.origins["5"] = f"signal:{SAM}"  # the run that was in flight there
    ch.handle_event(social_done(5, "[silent]"))
    assert len(client.messages) == 1 and "Sam: one" in client.messages[0][1] and "Sam: two" in client.messages[0][1]
    assert ch.state.social_pending == {}


def test_a_social_run_that_asks_a_question_is_unstuck(tmp_path):
    ch, _tg, sg, client, _people = make_people(tmp_path)
    ch.handle_event(event("run.waiting", 9, agent_id=SOCIAL, conversation_id=77, question="?", tool_call_id="tc9"))
    assert client.replies == [(9, UNATTENDED)] and sg.sent == []


def test_what_signal_will_not_take_reaches_the_owner_on_telegram(tmp_path):
    ch, tg, sg, _ = make_two(tmp_path, primary="signal")
    sg.down = True
    ch.handle_event(event("run.completed", 33, output="Morning brief."))
    assert sg.sent == []
    assert tg.sent[-1][0].startswith("(I couldn't reach you on signal: ") and tg.sent[-1][0].endswith("Morning brief.")


def test_the_owner_is_known_by_every_id_config_lists(tmp_path):
    tg, sg = FakeTelegram(), FakeSignal()
    client = FakeClient()
    ch = Channel({"telegram": tg, "signal": sg}, client, ChannelState(tmp_path / "channel.json"),
                 owners={"telegram": 7, "signal": OWNER_NUMBER}, primary="telegram", conversation_key="telegram:7",
                 owner_ids={"telegram": {"7"}, "signal": {OWNER_NUMBER, "u-1"}}, typing_interval=0)
    ch.handle(Inbound(kind="text", transport="signal", user_id="u-1", chat_id="u-1", text="hi", message_id=1))
    assert client.messages[0][0] == "fulla" and client.messages[0][2] == "telegram:7"
    assert sg.sent == [] and sg.actions == ["typing"]  # answered where asked, by the manager


# --- cancelling by word, and Signal's lack of buttons --------------------------


def test_on_signal_work_shows_as_typing_and_a_word_cancels_it(tmp_path):
    ch, _tg, sg, client = make_two(tmp_path, primary="signal")
    ch.handle(on_signal("book the dentist", message_id=1))
    assert sg.sent == [] and sg.actions == ["typing"] and ch.state.working == {"9": f"signal:{OWNER_NUMBER}#-"}
    ch.handle(on_signal("wait!", message_id=2))
    assert client.cancelled == [9] and ch.state.working == {}
    assert sg.sent == [("Cancelled.", None)] and sg.edited == [] and sg.deleted == []
    assert [m[1] for m in client.messages] == ["book the dentist"]  # "wait!" was not chat
    ch.handle_event(event("run.completed", 9, output="too late"))
    assert sg.sent == [("Cancelled.", None)]


def test_cancel_words_are_chat_when_nothing_is_running(tmp_path):
    ch, _tg, _sg, client = make_two(tmp_path, primary="signal")
    ch.handle(on_signal("nvm", message_id=1))
    assert client.cancelled == [] and [m[1] for m in client.messages] == ["nvm"]
    ch.handle_event(event("run.completed", 9, output="ok"))
    client.next_run = 10
    ch.handle(on_signal("never mind the dentist, book the gym", message_id=2))  # a sentence is not a cancel
    assert client.cancelled == [] and len(client.messages) == 2


def test_a_plain_cancel_works_on_telegram_too(tmp_path):
    ch, tg, client = make(tmp_path)
    mid = say(ch)
    ch.handle(Inbound(kind="text", user_id=7, chat_id=7, text="Cancel that.", message_id=300))
    assert client.cancelled == [9] and tg.edited == [(mid, "Cancelled.")]
    assert ch.state.working == {} and client.messages[-1][1] == "hello"


def test_an_unlisted_person_addressing_her_in_a_group_gets_the_owner_asked_once(tmp_path):
    ch, tg, sg, client, people = make_people(tmp_path, primary="telegram")
    people.remember_group("signal:group:abc", "Friends")
    people.allow_group("signal:group:abc")
    kim = in_group("u-kim", "Fulla, settle a bet", 1, sender_name="Kim")
    ch.handle(kim)
    assert client.messages == [] and sg.sent == []
    assert tg.sent[0][0].startswith("In Friends, Kim (signal id u-kim) just addressed me")
    tag = ch._group_tag("signal:u-kim")
    assert tg.sent[0][1] == [[("Yes", f"p:{tag}:yes"), ("No", f"p:{tag}:no")]]
    ch.handle(in_group("u-kim", "Fulla?", 2, sender_name="Kim"))
    assert len(tg.sent) == 1  # asked once
    ch.handle(in_group("u-kim", "hello everyone", 3, sender_name="Kim"))  # not addressed: no ask, no answer
    assert len(tg.sent) == 1
    ch.handle(Inbound(kind="callback", transport="telegram", user_id=7, chat_id=7, message_id=101, callback_id="cb",
                      data=f"p:{tag}:no"))
    assert tg.answered == [("cb", "I'll ignore Kim.")] and people.person_for("signal", "u-kim") is None
    ch.handle(in_group("u-kim", "Fulla, please?", 4, sender_name="Kim"))
    assert len(tg.sent) == 1 and client.messages == []  # ignored, and not asked again
    # the owner relents from chat, by the id in the question
    people.allow_person("Kim", {"signal": "u-kim"})
    ch.handle(in_group("u-kim", "Fulla, settle a bet", 5, sender_name="Kim"))
    assert len(client.messages) == 1 and "Kim: Fulla, settle a bet" in client.messages[0][1]


def test_a_signal_mention_of_fulla_addresses_her_and_a_bare_yes_or_thumbs_up_answers_the_ask(tmp_path):
    ch, _tg, sg, client, people = make_people(tmp_path, primary="signal")
    sg.number = "+16045550142"  # pyright: ignore[reportAttributeAccessIssue]  # Fulla's own number, as the Signal transport knows it
    ch.handle(in_group(OWNER_NUMBER, "@Fulla is my test agent", 1, mentions=["+16045550142", "u-fulla"]))
    assert sg.sent[0][0].startswith("You added me to Friends")  # asked; nothing in the group yet
    ch.handle(on_signal("yes", message_id=1700000000001))  # bare, not quoted: the one open question
    assert people.group("signal:group:abc").allowed and client.messages == []
    # the mention arrives as Fulla's number, which it does not know is its own; it is named anyway
    ch.handle(in_group(OWNER_NUMBER, "@+16045550142 you alive?", 2, mentions=["+16045550142"]))
    assert len(client.messages) == 1 and client.messages[0][1].endswith("@Fulla you alive?")
    # a second group: 👍 on the question answers it
    ch.handle(Inbound(kind="text", transport="signal", user_id=OWNER_NUMBER, chat_id="group:xyz", group=True,
                      chat_title="Family", text="Fulla, hi", message_id=3))
    ask_mid = sg._next_id
    ch.handle(Inbound(kind="reaction", transport="signal", user_id=OWNER_NUMBER, chat_id=OWNER_NUMBER, text="👍",
                      message_id=ask_mid, date=5))
    assert people.group("signal:group:xyz").allowed and len(client.messages) == 1  # no "[reaction]" chat


def test_saying_yes_to_the_person_question_lets_them_in(tmp_path):
    ch, tg, _sg, client, people = make_people(tmp_path, primary="telegram")
    people.remember_group("signal:group:abc", "Friends")
    people.allow_group("signal:group:abc")
    ch.handle(in_group("u-kim", "Fulla, hi", 1, sender_name="Kim"))
    ch.handle(Inbound(kind="text", transport="telegram", user_id=7, chat_id=7, text="yes", message_id=300,
                      reply_to_message_id=101))
    assert tg.edited[0][1].endswith("→ I'll listen to Kim.")
    ch.handle(in_group("u-kim", "Fulla, hi again", 2, sender_name="Kim"))
    assert len(client.messages) == 1


# --- group reminders, and a chat that must not go deaf -------------------------


def make_group(tmp_path, client=None, reminders=None, context=8, groups=None, push_groups=None):
    """An allowed group with Sam on the list; `reminders` is the group reminder index."""
    tg, sg = FakeTelegram(), FakeSignal()
    people = PeopleStore(tmp_path / "people.json")
    people.remember_group("signal:group:abc", "Friends")
    people.allow_group("signal:group:abc")
    people.allow_person("Sam", {"signal": SAM})
    client = client or FakeClient()
    index = reminders if reminders is not None else {}
    ch = Channel({"telegram": tg, "signal": sg}, client, ChannelState(tmp_path / "channel.json"),
                 owners={"telegram": 7, "signal": OWNER_NUMBER}, primary="signal", conversation_key="telegram:7",
                 people=people, owner_name="Robin", typing_interval=0,
                 group_reminder_known=lambda run_id: index.get(run_id), group_context=context,
                 groups=groups, push_groups=push_groups)
    return ch, sg, client, index


def test_a_group_reminder_comes_back_into_the_chat_that_set_it(tmp_path):
    ch, sg, _client, index = make_group(tmp_path)
    ch.handle(in_group(SAM, "Fulla, remind us in an hour to leave", 1))  # run 9, at the group
    index[70] = {"what": "leave", "due_at": "2026-10-19T12:00", "parent": 9}  # the tool scheduled run 70 from run 9
    ch.handle_event(social_done(9, "Will do."))
    assert sg.sent[-1] == ("Will do.", None)
    ch.handle_event(event("run.completed", 70, agent_id=GROUP_REMINDER, conversation_id=88, output="Reminder: leave"))
    assert sg.sent[-1] == ("⏰ Reminder: leave", None)
    ch.handle_event(event("run.completed", 70, agent_id=GROUP_REMINDER, conversation_id=88, output="Reminder: leave"))
    assert len(sg.sent) == 2  # once
    ch.handle_event(event("run.completed", 71, agent_id=GROUP_REMINDER, conversation_id=89, output="not ours"))
    assert len(sg.sent) == 2


def test_a_group_reminder_tags_whoever_asked_for_it(tmp_path):
    ch, sg, _client, index = make_group(tmp_path)
    ch.handle(in_group(SAM, "Fulla, remind us in an hour to stretch", 1))  # run 9, Sam asking
    assert ch.speakers_of(9) == [{"name": "Sam", "transport": "signal", "user_id": SAM}]
    index[70] = {"what": "stretch", "due_at": "2026-10-19T12:00", "parent": 9, "asked_by": ch.speakers_of(9)[0]}
    ch.handle_event(event("run.completed", 70, agent_id=GROUP_REMINDER, conversation_id=88, output="Reminder: stretch"))
    assert sg.sent[-1] == ("⏰ @Sam: stretch", None)
    assert sg.mentions == [(sg._next_id, [(SAM, "Sam")])]


def test_the_owner_asking_in_a_group_is_tagged_too(tmp_path):
    ch, sg, _client, index = make_group(tmp_path)
    ch.handle(in_group(OWNER_NUMBER, "Fulla, remind us at 7 to leave", 1))  # run 9
    assert ch.speakers_of(9) == [{"name": "Robin", "transport": "signal", "user_id": OWNER_NUMBER}]
    index[70] = {"what": "leave", "due_at": "2026-10-19T19:00", "parent": 9, "asked_by": ch.speakers_of(9)[0]}
    ch.handle_event(event("run.completed", 70, agent_id=GROUP_REMINDER, conversation_id=88, output="Reminder: leave"))
    assert sg.sent[-1] == ("⏰ @Robin: leave", None) and sg.mentions[-1][1] == [(OWNER_NUMBER, "Robin")]


def test_every_speaker_in_a_bundled_run_is_kept_in_order(tmp_path):
    ch, _sg, client, _ = make_group(tmp_path)
    ch.handle(in_group(SAM, "Fulla, hi", 1))  # run 9 in flight
    client.busy = True
    client.events[9] = "llm_request"
    ch.handle(in_group(OWNER_NUMBER, "Fulla, remind us to stretch", 2))  # queued behind run 9
    ch.handle(in_group(SAM, "Fulla, and me too", 3))
    ch.handle(in_group(OWNER_NUMBER, "Fulla, at 7", 4))
    assert [s["name"] for s in ch.state.pending_speakers["signal:group:abc"]] == ["Robin", "Sam"]
    client.busy = False
    client.next_run = 10
    ch.handle_event(social_done(9, "Hi Sam."))  # flushes the queue into run 10
    assert [s["name"] for s in ch.speakers_of(10)] == ["Robin", "Sam"]  # both, so the tool can pick by name
    assert ch.state.pending_speakers == {}


KIM = "+16045550122"


def test_everyone_who_speaks_in_an_allowed_group_is_one_of_its_members(tmp_path):
    ch, sg, client, _ = make_group(tmp_path)
    ch.handle(in_group(KIM, "morning all", 1, sender_name="Kim"))  # not addressed to Fulla, not listed
    ch.handle(in_group(SAM, "morning", 2))
    ch.handle(in_group(OWNER_NUMBER, "hey", 3))
    assert client.messages == [] and sg.sent == []
    members = ch.people.members("signal:group:abc")
    assert [(p.name, p.allowed) for p in members] == [("Kim", False), ("Sam", True)]  # the owner is not a Person
    assert "heard: Kim, Sam" in ch.people.render()


def test_a_chat_held_by_a_sleeping_run_is_freed_and_a_thinking_one_is_not(tmp_path):
    ch, sg, client, _ = make_group(tmp_path)
    ch.handle(in_group(SAM, "Fulla, hi", 1))  # run 9 in flight at the group
    client.busy = True
    client.events[9] = "llm_request"  # thinking: leave it
    ch.handle(in_group(SAM, "Fulla, and another thing", 2))
    assert client.cancelled == [] and len(ch.state.social_pending["signal:group:abc"]) == 1
    client.events[9] = "waiting_for_timer"  # asleep in wait: the chat would be deaf
    ch.handle(in_group(SAM, "Fulla, you there?", 3))
    assert client.cancelled == [9]
    client.busy = False
    ch.handle_event(event("run.failed", 9, agent_id=SOCIAL, conversation_id=77, error="cancelled"))
    assert sg.sent == []  # a failure is not announced in the group
    assert len(client.messages) == 2 and "another thing" in client.messages[1][1] and "you there" in client.messages[1][1]
    assert ch.state.social_pending == {}


THREAD = "[Untrusted group thread from the messages in Friends before the one addressed to you."
THREAD_END = "[End of untrusted group thread.]"


def thread_of(content):
    """The lines inside the thread block of what the social agent was sent, or None."""
    if THREAD not in content:
        return None
    inside = content.split(THREAD, 1)[1].split(THREAD_END, 1)[0]
    return inside.strip().split("\n")[1:]  # the first line is the rest of the marker


def test_an_addressed_message_carries_the_thread_before_it(tmp_path):
    ch, _sg, client, _ = make_group(tmp_path)
    ch.handle(in_group(KIM, "when do you leave for Lisbon again?", 1, sender_name="Kim"))  # unlisted, unaddressed
    ch.handle(in_group(SAM, "Weds", 2))
    ch.handle(in_group(OWNER_NUMBER, "Fulla, remind me to send Sam a bon voyage message that day", 3))
    assert len(client.messages) == 1
    content = client.messages[0][1]
    assert thread_of(content) == ["Kim: when do you leave for Lisbon again?", "Sam: Weds"]
    assert content.index(THREAD_END) < content.index("[In the group Friends, addressed to you]")
    assert "Robin (the owner): Fulla, remind me" in content and content.count("Fulla, remind me") == 1
    ch.handle_event(social_done(9, "Will do."))  # its own reply joins the thread
    client.next_run = 10
    ch.handle(in_group(SAM, "Fulla, me too", 4))
    assert thread_of(client.messages[1][1]) == [
        "Kim: when do you leave for Lisbon again?", "Sam: Weds",
        "Robin (the owner): Fulla, remind me to send Sam a bon voyage message that day", "Fulla: Will do.",
    ]


def test_the_thread_is_capped_and_forgets_what_is_old(tmp_path):
    ch, _sg, client, _ = make_group(tmp_path, context=3)
    for i in range(1, 6):
        ch.handle(in_group(SAM, f"line {i}", i))
    ch.state.group_ring["signal:group:abc"][-1]["at"] -= 3 * 3600  # line 5 is from this morning
    ch.handle(in_group(SAM, "Fulla, which?", 6))
    assert thread_of(client.messages[0][1]) == ["Sam: line 3", "Sam: line 4"]


def test_the_thread_is_off_at_zero_and_absent_one_to_one(tmp_path):
    ch, _sg, client, _ = make_group(tmp_path, context=0)
    ch.handle(in_group(SAM, "context", 1))
    ch.handle(in_group(SAM, "Fulla, hi", 2))
    assert ch.state.group_ring == {} and THREAD not in client.messages[0][1]
    ch2, _tg, _sg2, client2, people = make_people(tmp_path / "dm")
    people.allow_person("Sam", {"signal": SAM})
    ch2.handle(from_sam("hi", message_id=1))
    ch2.handle(from_sam("Fulla, again", message_id=2))
    assert ch2.state.group_ring == {} and all("group thread" not in m[1] for m in client2.messages)


def test_a_group_the_owner_has_not_allowed_stores_nothing(tmp_path):
    ch, _tg, _sg, client, people = make_people(tmp_path)
    people.remember_group("signal:group:abc", "Friends")  # asked, not allowed
    ch.handle(in_group(SAM, "anyone there?", 1))
    ch.handle(in_group(OWNER_NUMBER, "Fulla, hello", 2))
    assert ch.state.group_ring == {} and client.messages == []


def test_only_the_first_message_of_a_bundle_carries_the_thread(tmp_path):
    ch, _sg, client, _ = make_group(tmp_path)
    ch.handle(in_group(SAM, "dinner?", 1))
    ch.handle(in_group(SAM, "Fulla, hi", 2))  # run 9, with the thread
    assert thread_of(client.messages[0][1]) == ["Sam: dinner?"]
    client.busy = True
    client.events[9] = "llm_request"
    ch.handle(in_group(SAM, "Fulla, and sushi?", 3))  # finds the chat busy: queued, thread and all
    ch.handle(in_group(SAM, "Fulla, or ramen", 4))  # behind one already waiting: no thread of its own
    client.busy = False
    client.next_run = 10
    ch.handle_event(social_done(9, "Hi."))
    assert len(client.messages) == 2
    bundle = client.messages[1][1]
    assert bundle.count(THREAD) == 1 and thread_of(bundle) == ["Sam: dinner?", "Sam: Fulla, hi"]
    assert bundle.index("and sushi?") < bundle.index("or ramen")
    assert [e["who"] for e in ch.state.group_ring["signal:group:abc"]] == ["Sam", "Sam", "Sam", "Sam", "Fulla"]


def test_the_thread_survives_a_restart_and_a_reminder_joins_it(tmp_path):
    ch, _sg, _client, index = make_group(tmp_path)
    ch.handle(in_group(SAM, "Fulla, remind us in an hour to stretch", 1))  # run 9
    index[70] = {"what": "stretch", "due_at": "2026-10-19T12:00", "parent": 9, "asked_by": ch.speakers_of(9)[0]}
    ch.handle_event(event("run.completed", 70, agent_id=GROUP_REMINDER, conversation_id=88, output="Reminder: stretch"))
    again = ChannelState(tmp_path / "channel.json")
    assert [(e["who"], e["text"]) for e in again.group_ring["signal:group:abc"]] == [
        ("Sam", "Fulla, remind us in an hour to stretch"), ("Fulla", "⏰ @Sam: stretch")]


def owner_says(text, message_id):
    return Inbound(kind="text", transport="signal", user_id=OWNER_NUMBER, chat_id=OWNER_NUMBER, text=text,
                   message_id=message_id)


def test_the_owner_can_reset_a_groups_conversation_by_name(tmp_path):
    ch, sg, client, _ = make_group(tmp_path)
    ch.handle(in_group(SAM, "dinner?", 1))
    ch.handle(in_group(SAM, "Fulla, can you message Kim?", 2))  # run 9 in flight, a wrong answer coming
    client.busy = True
    client.events[9] = "llm_request"
    ch.handle(in_group(SAM, "Fulla, well?", 3))  # queued behind it
    client.busy = False
    ch.handle(owner_says("/reset Friends", 50))
    assert client.cancelled == [9]
    assert client.messages[-1] == ("delete", "fulla-social", "social:signal:group:abc")
    assert ch.state.social_pending == {} and ch.state.pending_speakers == {} and ch.state.group_ring == {}
    assert sg.sent[-1][0].startswith("Fresh start with Friends;")
    assert ch.people.group("signal:group:abc").allowed  # the door is untouched


def test_the_owner_can_reset_a_friends_conversation_and_only_the_owner_can(tmp_path):
    ch, sg, client, _ = make_group(tmp_path)
    ch.handle(from_sam("/reset Friends", message_id=1))  # a friend asking: a message to the social agent, not a command
    assert client.messages[-1][0] == "fulla-social" and "/reset Friends" in client.messages[-1][1]
    ch.handle(owner_says("/reset sam", 50))
    assert client.messages[-1] == ("delete", "fulla-social", f"social:signal:{SAM}")
    assert sg.sent[-1][0].startswith("Fresh start with Sam;")
    ch.handle(owner_says("/reset Bob", 51))
    assert sg.sent[-1][0].startswith("I don't know a group or person called Bob.")
    assert sum(1 for m in client.messages if m[0] == "delete") == 1


def test_a_mention_of_a_listed_person_reaches_the_social_agent_by_name(tmp_path):
    ch, _sg, client, _ = make_group(tmp_path)
    ch.handle(in_group(OWNER_NUMBER, f"Fulla, remind @{SAM} to crack a beer", 1))  # Signal gives the id, not the name
    assert "Fulla, remind @Sam to crack a beer" in client.messages[0][1] and SAM not in client.messages[0][1]
    assert ch.state.group_ring["signal:group:abc"][-1]["text"] == "Fulla, remind @Sam to crack a beer"


def test_a_group_with_a_directory_talks_to_its_own_agent_and_a_friend_to_the_shared_one(tmp_path):
    pushes = []
    store = GroupStore(tmp_path / "groups")
    ch, sg, client, _ = make_group(tmp_path, groups=store, push_groups=lambda: pushes.append(1))
    ch.handle(in_group(SAM, "Fulla, hi", 1))  # the first word in an allowed group gives it a directory and an agent
    record = store.by_address("signal:group:abc")
    assert record is not None and pushes == [1]
    name = f"fulla-group-{record.id}"
    assert client.messages[-1][0] == name
    client.agents.append(Agent(id=99, name=name))
    ch.handle_event(event("run.completed", 9, agent_id=99, conversation_id=77, output="Hi Sam."))
    assert sg.sent[-1] == ("Hi Sam.", None)  # a group agent's run is ours, known by its name
    client.next_run = 10
    ch.handle(in_group(SAM, "Fulla, again", 2))
    assert client.messages[-1][0] == name and pushes == [1]  # no new directory, no new push
    client.next_run = 11
    ch.handle(from_sam("hi", message_id=3))
    assert client.messages[-1][0] == "fulla-social"  # one to one stays with the shared agent
    ch.handle(owner_says("/reset Friends", 50))
    assert [m for m in client.messages if m[0] == "delete"] == [
        ("delete", name, "social:signal:group:abc"), ("delete", "fulla-social", "social:signal:group:abc")]


def test_a_group_whose_agent_is_not_on_the_server_yet_is_answered_by_the_shared_one(tmp_path):
    store = GroupStore(tmp_path / "groups")
    client = FakeClient()
    ch, _sg, client, _ = make_group(tmp_path, client=client, groups=store)
    client.refuse_agents = {"fulla-group-"}  # any group agent: Norns has none yet
    ch.handle(in_group(SAM, "Fulla, hi", 1))
    assert client.messages[-1][0] == "fulla-social" and ch.state.origins["9"] == "signal:group:abc"


def test_tell_owner_posts_a_plain_line_at_the_primary_address(tmp_path):
    ch, tg, _ = make(tmp_path)
    ch.tell_owner("A machine calling itself box is trying to join, but that name belongs to another machine I know.")
    assert tg.sent == [("A machine calling itself box is trying to join, but that name belongs to another machine I know.", None)]
    assert list(ch.state.texts.values()) == [tg.sent[0][0]]  # remembered, so a reaction to it has its text
