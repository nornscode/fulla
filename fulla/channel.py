"""The channel: where the messengers (Telegram, Signal) and Norns meet. Fulla itself never sees it.

Addresses. The owner may reach Fulla on more than one messenger, so the
channel speaks of addresses, not chats: `telegram:<chat id>`,
`signal:<number>`. Every message the owner sends has one, every run the
owner starts remembers the address it came from, and the run's answers,
questions and Working… message go back there. A run with no owner behind
it (a scheduled turn, a reminder) goes to the primary address. All the
owner's addresses share one standing conversation, so Fulla has one memory
of talking to them whichever app they pick up. A message is named by its
address and id together (`telegram:7#101`), since ids mean nothing across
messengers.

Inbound: button taps and replies-to-a-question answer the run that asked;
anything else is a message to the manager's standing conversation, sent and
forgotten. If that conversation is mid-run (a long turn, a sub-agent, a
scheduled check-in), Norns refuses the send as busy; the message is kept,
in order, and sent when the run's run.completed or run.failed arrives, or
on a cold start. An emoji reaction is a short answer to a question, or a
"[reaction]" message to the manager quoting what it was on.

Outbound: Norns tells us. A run webhook delivers run.waiting, run.completed
and run.failed for every run in the tenant; we post the ones that are ours:
a manager run in the owner's standing conversation, or a reminder run Fulla
scheduled. A manager run on some other conversation key (an API call, a
test) is not relayed. A reply that is one emoji the messenger allows becomes
a reaction on the owner's message instead. On a cold start `reconcile` asks
Norns for runs still parked on a question, in case a delivery was missed
while we were asleep.

Unattended: the reader and Fulla's follow-ups run with nobody to ask, and
the reader's caller is the manager, blocked until it returns. Norns offers
`ask_human` to every agent regardless, so if one calls it anyway the channel
answers it at once with UNATTENDED and posts nothing; otherwise the run
would park forever and the owner's messages would queue behind it.

While a manager run the owner started is in flight, the chat shows it: a
"Working…" message with a Cancel button, and the typing indicator re-sent
until the run's event arrives. The message goes when the run does.

Confirm cards (confirm.py): something Fulla proposed that happens only when
the owner approves it. `propose` posts the card to the owner, where they are
talking to the manager now or else the primary address; a button or a quoted
reply naming one approves, edits or cancels it once, and a reaction can
only cancel; a
machine that acts on it claims the approved payload with `claim`. A run that
approving started reports back onto the card. Only the owner decides a
card, one to one, at the address it was posted to: nothing a friend or a
group sends reaches the card code, and the card code checks again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fulla import confirm
from fulla.confirm import ClaimRefused, Confirmation, ConfirmKind, digest
from fulla.groups import GROUP_AGENT_PREFIX, GroupStore, group_agent_name
from fulla.norns_api import Busy, NornsApi
from fulla.people import PeopleStore
from fulla.transport import Inbound, Transport
from fulla.untrusted import untrusted

logger = logging.getLogger("fulla.channel")

STATE_VERSION = 2

# A scheduled run that decided it has nothing to say ends with this, and we
# don't relay it. The run is still in the log, so "why didn't you nudge me"
# has an answer.
SILENT = "[silent]"


def is_silent(output: str | None) -> bool:
    """Whether a run's output is for nobody: empty, the marker, or a work log
    that ends with the marker, on its own line or after a sentence. The prompt
    asks for the marker alone, but a model that writes what it did and then
    "[silent]" meant the marker, and the notes before it were never for the
    owner."""
    text = (output or "").strip()
    if not text:
        return True
    return text.lower().endswith(SILENT)
RELAYED_KEEP = 200
# Reactions older than this many of our messages reach nobody: there is no
# text to quote, and the message may not even be ours.
TEXTS_KEEP = 200
PROMPTS_KEEP = 50
QUOTE_MAX = 200

# A reaction on a question is an answer, when it plainly is one.
REMINDER_REACTIONS = {"👍": "done", "👌": "done", "😴": "snooze 1 hour", "👎": "drop"}
QUESTION_REACTIONS = {"👍": "yes", "👎": "no"}

WORKING = "Working…"
CANCELLED = "Cancelled."
# How long the typing indicator keeps being re-sent for one run. A run that
# outlives this is still shown by its Working… message; only the bubble stops.
TYPING_MAX = 600.0

# Only from a Norns that cannot take a message into the run under way.
QUEUED = "Still on the last thing; I'll pass this on when it's done."
HELD = "I'll pass that on after you answer my question."
HELD_ON = "I'll pass that on after you answer:"

UNATTENDED = "Nobody can answer questions here. Finish now with what you have."
TIMER_STARTED = "schedule"  # Norns's trigger_type for a run one of its timers started

# While a run of the owner's is in flight, a message that is nothing but one
# of these cancels it, on either messenger; so does one of these reactions
# on the Working… message, or a reply to it saying one of the words.
CANCEL_WORDS = frozenset({
    "cancel", "cancel that", "stop", "stop that", "wait", "hold on", "hang on", "nvm", "never mind",
    "nevermind", "abort", "forget it", "scratch that", "no wait",
})
CANCEL_REACTIONS = frozenset({"❌", "🛑", "✋", "🚫"})
CONFIRMS_KEEP = 50
EDIT_PROMPT = "Send me the new text."
EDITED = "Edited; the new draft is below."
EDIT_DROPPED = "Edit dropped; the draft is as it was."
NOT_OURS = {
    confirm.EDITING: "Waiting for your edit.",
    confirm.APPROVED: "Already approved.",
    confirm.CLAIMED: "Already on its way.",
    confirm.DONE: "Already done.",
    confirm.CANCELLED: "Already cancelled.",
}
RUN_TERMINAL = {"completed", "failed", "error", "cancelled"}
# A reaction never sends a card: it is too easy to leave by accident. These
# on a card get a pointer to Send; cancel reactions (and 👎) are Cancel.
CARD_APPROVE_REACTIONS = frozenset({"👍", "👌", "✅"})
CARD_SEND_BY_REPLY = "Reactions don't send. Reply Send to the card to send it."

# A Working… entry whose message id is this has no message: the messenger
# has no buttons, so the work shows as the typing indicator alone.
NO_MESSAGE = "-"

# Replies to the private "may I talk in this group?" question, without buttons.
YES_WORDS = frozenset({"yes", "y", "yes.", "sure", "ok", "okay", "yep", "yeah", "allow", "allow it", "go ahead"})
NO_WORDS = frozenset({"no", "n", "no.", "nope", "don't", "dont", "never", "deny"})
CAPPED = "I'm done here for today."
SOCIAL_KEY = "social:"  # conversation keys of friends' chats and groups: social:<address>
# How a group agent's own check-back comes back to it (tools/group_reminders.py).
FOLLOWUP_MARK = "[scheduled] follow-up you set:"
# How a group message addresses Fulla: its name up front, or a reply to it.
ADDRESSED_RE = re.compile(r"^\W*fulla\b", re.IGNORECASE)
# A group's recent messages are kept to give the social agent the thread when
# it is addressed; older than this they are stale and left out, and a long
# one is kept only up to this many characters.
GROUP_CONTEXT_AGE = 2 * 3600.0
GROUP_LINE_MAX = 300


class NotTaken(Exception):
    """An owner message Norns could not be reached for. Whatever delivered it
    (Telegram's webhook, the Signal forwarder) should deliver it again; the
    server answers 503 to say so."""


def ref(address: str, message_id) -> str:
    """A message's name: its address and the messenger's id for it."""
    return f"{address}#{message_id}"


def split_ref(r: str) -> tuple[str, str]:
    address, _, message_id = r.rpartition("#")
    return address, message_id


def _newest(d: dict, keep: int) -> dict:
    return dict(list(d.items())[-keep:])


def _add_speaker(speakers: list[dict], who: dict) -> None:
    """Add a speaker to a run's list once, in order of first speaking."""
    if not any(s.get("transport") == who["transport"] and s.get("user_id") == who["user_id"] for s in speakers):
        speakers.append(who)


def is_cancel(text: str) -> bool:
    """Is this message nothing but a way of saying stop?"""
    return re.sub(r"[\s!.,…]+$", "", text.strip().lower()) in CANCEL_WORDS


def social_body(speaker: str, text: str, owner_name: str) -> str:
    """A message as the social agent sees it: the owner's words marked as
    theirs, anyone else's inside the untrusted marker."""
    if speaker == owner_name:
        return f"{speaker} (the owner): {text}"
    return untrusted("message", f"{speaker}, who is not the owner", f"{speaker}: {text}")


class ChannelState:
    """Bookkeeping that must survive a restart so nothing is posted twice.
    Messages are keyed by ref (address#id); runs by their id."""

    def __init__(self, path: Path):
        self.path = path
        self.posted: dict[str, str] = {}  # "run_id:tool_call_id" -> ref of the question message
        self.message_runs: dict[str, int] = {}  # ref -> run_id
        self.questions: dict[str, str] = {}  # ref -> question text
        self.question_agents: dict[str, int] = {}  # ref -> agent_id
        self.relayed: list[int] = []  # run ids whose outcome has been sent
        self.said: list[str] = []  # "run_id:digest" of messages a manager run sent mid-turn
        self.working: dict[str, str] = {}  # run_id -> ref of its Working… message
        self.texts: dict[str, str] = {}  # ref -> what we posted, for reactions to quote
        self.prompts: dict[str, str] = {}  # run_id -> ref of the owner's message that started it
        self.origins: dict[str, str] = {}  # run_id -> address its answers go to
        self.reactions: list[str] = []  # "ref:emoji:date" of reactions handled
        self.handled: list[str] = []  # owner messages already handled; a redelivery is ignored
        self.pending: list[str] = []  # owner messages Norns refused as busy, oldest first
        self.pending_prompt: str | None = None  # ref of the newest owner message in `pending`
        self.pending_origin: str | None = None  # where the newest pending message came from
        self.pending_working: bool = False  # something in `pending` deserves a Working… message
        self.queued_told: bool = False  # the owner has been told `pending` is waiting
        self.parked: int | None = None  # standing manager run parked on a question
        self.unstuck: list[str] = []  # "run_id:tool_call_id" of unattended asks answered
        self.social_pending: dict[str, list[str]] = {}  # address -> friends' messages waiting on a run there
        self.social_counts: dict[str, list] = {}  # address -> [day, messages answered that day]
        self.group_asks: dict[str, str] = {}  # ref of a "may I talk in this group?" question -> the group's address
        self.person_asks: dict[str, str] = {}  # ref of a "shall I listen to them?" question -> "transport:id"
        self.social_origins: dict[str, str] = {}  # social run_id -> address, kept after the run ends, for its reminders
        self.speakers: dict[str, list] = {}  # social run_id -> [{"name", "transport", "user_id"}] of who it answers
        self.pending_speakers: dict[str, list] = {}  # address -> the speakers waiting in social_pending, in order
        self.confirms: dict[str, dict] = {}  # confirm id -> Confirmation, oldest first
        self.editing: str | None = None  # the confirm whose new text the owner's next message is
        self.group_ring: dict[str, list[dict]] = {}  # group address -> its last few messages [{"who", "text", "ref", "at"}]
        if path.exists():
            raw = json.loads(path.read_text() or "{}")
            # What is keyed by run id or holds plain text carries over from
            # the one-messenger format; what was keyed by a bare message id
            # cannot, since nothing says which messenger it meant.
            current = raw.get("version") == STATE_VERSION
            if not current and raw:
                logger.warning("channel state %s is from before addresses; message bookkeeping starts over", path)
            if current:
                self.posted = raw.get("posted", {})
                self.message_runs = raw.get("message_runs", {})
                self.questions = raw.get("questions", {})
                self.question_agents = raw.get("question_agents", {})
                self.working = raw.get("working", {})
                self.texts = raw.get("texts", {})
                self.prompts = raw.get("prompts", {})
                self.origins = raw.get("origins", {})
                self.reactions = raw.get("reactions", [])
                self.pending_prompt = raw.get("pending_prompt")
                self.pending_origin = raw.get("pending_origin")
                self.social_pending = raw.get("social_pending", {})
                self.social_counts = raw.get("social_counts", {})
                self.group_asks = raw.get("group_asks", {})
                self.person_asks = raw.get("person_asks", {})
                self.social_origins = raw.get("social_origins", {})
                self.speakers = raw.get("speakers", {})
                self.pending_speakers = raw.get("pending_speakers", {})
                self.confirms = raw.get("confirms", {})
                self.editing = raw.get("editing")
                self.group_ring = raw.get("group_ring", {})
            self.relayed = raw.get("relayed", [])
            self.said = raw.get("said", [])
            self.handled = raw.get("handled", []) if current else []
            self.pending = raw.get("pending", [])
            self.pending_working = raw.get("pending_working", False)
            self.queued_told = raw.get("queued_told", False)
            self.parked = raw.get("parked")
            self.unstuck = raw.get("unstuck", [])

    def save(self):
        self.texts = _newest(self.texts, TEXTS_KEEP)
        self.prompts = _newest(self.prompts, PROMPTS_KEEP)
        self.origins = _newest(self.origins, PROMPTS_KEEP)
        self.reactions = self.reactions[-RELAYED_KEEP:]
        self.confirms = _newest(self.confirms, CONFIRMS_KEEP)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "version": STATE_VERSION,
                    "posted": self.posted,
                    "message_runs": self.message_runs,
                    "questions": self.questions,
                    "question_agents": self.question_agents,
                    "relayed": self.relayed[-RELAYED_KEEP:],
                    "said": self.said[-RELAYED_KEEP:],
                    "working": self.working,
                    "texts": self.texts,
                    "prompts": self.prompts,
                    "origins": self.origins,
                    "reactions": self.reactions,
                    "handled": self.handled[-RELAYED_KEEP:],
                    "pending": self.pending,
                    "pending_prompt": self.pending_prompt,
                    "pending_origin": self.pending_origin,
                    "pending_working": self.pending_working,
                    "queued_told": self.queued_told,
                    "parked": self.parked,
                    "unstuck": self.unstuck[-RELAYED_KEEP:],
                    "social_pending": {a: m for a, m in self.social_pending.items() if m},
                    "social_counts": self.social_counts,
                    "group_asks": _newest(self.group_asks, PROMPTS_KEEP),
                    "person_asks": _newest(self.person_asks, PROMPTS_KEEP),
                    "social_origins": _newest(self.social_origins, RELAYED_KEEP),
                    "speakers": _newest(self.speakers, RELAYED_KEEP),
                    "pending_speakers": {a: s for a, s in self.pending_speakers.items() if self.social_pending.get(a)},
                    "confirms": self.confirms,
                    "editing": self.editing,
                    "group_ring": {a: m for a, m in self.group_ring.items() if m},
                },
                indent=2,
            )
        )
        tmp.replace(self.path)


class Channel:
    def __init__(
        self,
        transports: dict[str, Transport],
        client: NornsApi,
        state: ChannelState,
        *,
        owners: dict[str, int | str],
        primary: str,
        conversation_key: str,
        manager_agent: str = "fulla",
        reminder_agent: str = "fulla-reminder",
        reminder_known: Callable[[int], bool] = lambda run_id: True,
        mac_task_agent: str = "fulla-mac-task",
        mac_task_known: Callable[[int], bool] = lambda run_id: True,
        unattended_agents: tuple[str, ...] = ("fulla-reader", "fulla-followup", "fulla-watch"),
        typing_interval: float = 4.0,
        people: PeopleStore | None = None,
        owner_ids: dict[str, set[str]] | None = None,
        owner_name: str = "the owner",
        social_agent: str = "fulla-social",
        social_cap: int = 50,
        group_reminder_agent: str = "fulla-group-reminder",
        group_reminder_known: Callable[[int], dict | None] = lambda run_id: None,
        confirm_kinds: Iterable[ConfirmKind] = (),
        group_context: int = 8,
        groups: GroupStore | None = None,
        push_groups: Callable[[], None] | None = None,
    ):
        """`transports` by name; `owners` is the owner's chat id on each;
        `primary` is the transport whose owner address gets what has no
        origin: scheduled turns, reminders, anything from before a restart.
        `conversation_key` is the standing conversation, built in config and
        shared with the follow-up tool, so a timer never writes into a
        conversation nobody is watching.

        `people` is who else may talk, and where (people.py); without it,
        nobody. `owner_ids` is every id the owner goes by on each transport,
        when it is more than the chat id (a Signal account id beside the
        number). Friends and groups get `social_agent`, at most `social_cap`
        answers a day per conversation. `confirm_kinds` are the kinds of
        confirm card `propose` may post. A message addressing Fulla in a group
        carries the `group_context` messages before it, so Fulla knows what
        "us" and "that" mean; 0 turns that off."""
        if primary not in transports or primary not in owners:
            raise ValueError(f"primary transport {primary!r} is not configured")
        self.transports = transports
        self.client = client
        self.state = state
        self.owner_addresses = {f"{name}:{chat}": chat for name, chat in owners.items() if name in transports}
        self.owner_ids = owner_ids or {name: {str(chat)} for name, chat in owners.items()}
        self.owner_name = owner_name
        self.people = people
        self.social_agent = social_agent
        self.social_cap = social_cap
        self.group_reminder_agent = group_reminder_agent
        self.group_reminder_known = group_reminder_known
        self.group_context = max(0, group_context)
        self.groups = groups  # each allowed group's directory, kept in step with `people`
        self.push_groups = push_groups  # declares a newly allowed group's agent at once
        self._group_agent_ids: dict[str, int] = {}  # name -> id of the per-group agents, as last listed
        self.primary = f"{primary}:{owners[primary]}"
        self.conversation_key = conversation_key
        self.manager_agent = manager_agent
        self.reminder_agent = reminder_agent
        self.reminder_known = reminder_known
        self.mac_task_agent = mac_task_agent
        self.mac_task_known = mac_task_known
        # Agents nobody answers: an ask from one is unstuck, never posted.
        self.unattended_agents = unattended_agents
        # Seconds between re-sends of the typing indicator; 0 sends it once.
        self.typing_interval = typing_interval
        self.confirm_kinds = {k.name: k for k in confirm_kinds}
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._chats: dict[str, int | str] = dict(self.owner_addresses)  # address -> chat id as the messenger gave it
        self._unreachable_told: set[str | None] = set()  # owner messages told "couldn't reach Norns"
        self._agent_ids: dict[str, int] = {}
        self._conversation_id: int | None = None

    # ---------------------------------------------------------------- addresses

    def address_of(self, inbound: Inbound) -> str:
        """Where this came from. A transport that did not say which it is
        (an old poll loop, a test) is the only one when there is only one."""
        name = inbound.transport or (next(iter(self.transports)) if len(self.transports) == 1 else "")
        if name not in self.transports:
            raise ValueError(f"inbound from unknown transport {name!r}")
        address = f"{name}:{inbound.chat_id}"
        self._chats.setdefault(address, inbound.chat_id)
        return address

    def _transport(self, address: str) -> Transport:
        name = address.split(":", 1)[0]
        if name not in self.transports:
            raise ValueError(f"no transport for {address}")
        return self.transports[name]

    def _chat_id(self, address: str):
        chat = self._chats.get(address)
        if chat is not None:
            return chat
        raw = address.split(":", 1)[1]
        return int(raw) if raw.lstrip("-").isdigit() else raw

    def _target(self, run_id: int) -> str:
        """Where a run's output goes: back where the owner started it, or the
        primary address when nobody did (a scheduled turn, a reminder)."""
        return self.state.origins.get(str(run_id)) or self.primary

    def _send(self, address: str, text: str, buttons=None, mentions=None, *, plain: bool = False) -> str:
        """Send and return the message's ref. A message for the owner that
        their messenger will not take (the Signal machine down, Telegram
        unreachable) goes to them on the other one, saying so, rather than
        nowhere. `plain` sends it verbatim (a card to approve)."""
        extra: dict[str, Any] = {"plain": True} if plain else {}
        try:
            message_id = self._transport(address).send(self._chat_id(address), text, buttons=buttons,
                                                       mentions=mentions, **extra)
        except Exception as e:
            fallback = next((a for a in self.owner_addresses if a != address), None) \
                if address in self.owner_addresses else None
            if fallback is None:
                raise
            logger.warning("could not send to %s (%s); trying %s", address, e, fallback)
            note = f"(I couldn't reach you on {address.split(':', 1)[0]}: {e})"
            message_id = self._transport(fallback).send(self._chat_id(fallback), f"{note}\n\n{text}",
                                                         buttons=buttons, **extra)
            return ref(fallback, message_id)
        return ref(address, message_id)

    def _edit(self, r: str, text: str) -> None:
        address, message_id = split_ref(r)
        self._transport(address).edit(self._chat_id(address), self._message_id(address, message_id), text)  # pyright: ignore[reportArgumentType]

    def _delete(self, r: str) -> bool:
        address, message_id = split_ref(r)
        return self._transport(address).delete(self._chat_id(address), self._message_id(address, message_id))  # pyright: ignore[reportArgumentType]

    def _react(self, r: str, emoji: str) -> None:
        address, message_id = split_ref(r)
        self._transport(address).set_reaction(self._chat_id(address), self._message_id(address, message_id), emoji)  # pyright: ignore[reportArgumentType]

    @staticmethod
    def _message_id(address: str, raw: str):
        """Message ids come back from refs as strings; both messengers name
        messages by integers (Telegram ids, Signal timestamps)."""
        return int(raw) if raw.lstrip("-").isdigit() else raw

    # ------------------------------------------------------------------ inbound

    def handle(self, inbound: Inbound) -> None:
        """Raises NotTaken when Norns could not be reached for an owner
        message, so the messenger delivers it again; returning normally
        means the message is safe (Norns has it, or it is kept in `pending`).

        Both messengers can deliver one message twice: Telegram retries a
        webhook it got no answer to, and the Signal forwarder retries until
        Fulla answers, so a reply lost on the way back means the same message
        again. A message is remembered as handled only once it is safe, and a
        redelivery of one is used as the retry timer for anything kept."""
        address = self.address_of(inbound)
        admitted = self._admit(inbound, address)
        if admitted is None:
            return
        who, speaker = admitted
        key = f"{ref(address, inbound.message_id)}:{inbound.user_id}" \
            if inbound.kind == "text" and inbound.message_id is not None else None
        if key is not None and key in self.state.handled:
            logger.info("message %s delivered again; already handled", inbound.message_id)
            if who == "owner" and not self._flush():
                raise NotTaken("kept; Norns unreachable")
            return
        taken = True
        if who == "social" and inbound.kind == "callback":
            # Only the owner has buttons to press: a friend's tap (on a
            # forwarded card, say) means nothing.
            self._transport(address).answer_callback(inbound.callback_id)
        elif who == "social":
            self._handle_social(inbound, address, speaker)
        elif inbound.kind == "callback":
            self._handle_callback(inbound, address)
        elif inbound.kind == "text":
            taken = self._handle_text(inbound, address)
        elif inbound.kind == "reaction":
            self._handle_reaction(inbound, address)
        if key is not None:
            with self._lock:
                self.state.handled.append(key)
                self.state.save()
        if not taken:
            raise NotTaken("kept; Norns unreachable")

    # ------------------------------------------------------------- who may talk

    def _is_owner(self, inbound: Inbound, address: str) -> bool:
        transport = address.split(":", 1)[0]
        return str(inbound.user_id) in self.owner_ids.get(transport, set())

    def _admit(self, inbound: Inbound, address: str) -> tuple[str, str] | None:
        """Who is this, and may they? ("owner", "") for the owner one to one:
        the manager and the standing conversation. ("social", name) for a
        listed friend one to one, or for anyone listed (the owner included)
        addressing Fulla in a group the owner allowed: the social agent, in
        that address's own conversation. None drops the message. A group
        the owner has not allowed yet is remembered the first time the owner
        speaks in it, and the owner is asked privately."""
        transport = address.split(":", 1)[0]
        owner = self._is_owner(inbound, address)
        if not inbound.group:
            if owner:
                return ("owner", "")
            person = self.people.person_for(transport, inbound.user_id) if self.people else None
            if person is None:
                logger.warning("dropped a %s from %s on %s: not the owner, not on the list",
                               inbound.kind, inbound.user_id, transport)
                return None
            return ("social", person.name)
        group = self.people.group(address) if self.people else None
        if group is None or not group.allowed:
            logger.info("group %s: %s; %s from %s (owner: %s) dropped", address,
                        "unknown" if group is None else "not allowed", inbound.kind, inbound.user_id, owner)
            if owner and self.people is not None and inbound.kind == "text":
                self._ask_about_group(inbound, address)
            return None
        person = None if owner else self.people.person_for(transport, inbound.user_id)  # pyright: ignore[reportOptionalMemberAccess]
        if inbound.kind == "text":
            if not owner:
                # Anyone who speaks in an allowed group is one of its members
                # from now, listed or not: that is how Fulla later tells people
                # apart and tags the right one.
                self.people.saw_in_group(address, transport, inbound.user_id, inbound.sender_name)  # pyright: ignore[reportOptionalMemberAccess]
                self._sync_groups()
            # And what they said is the thread it reads when addressed.
            self._remember_in_group(address, self._label(owner, person, inbound), self._name_me(address, inbound.text),
                                    ref(address, inbound.message_id))
        if inbound.kind != "text" or not self._addressed(inbound, address):
            logger.info("group %s: %s from %s not addressed to Fulla; dropped", address, inbound.kind, inbound.user_id)
            return None
        if owner:
            return ("social", self.owner_name)
        if person is None:
            # Someone the owner has not listed, addressing Fulla in a group
            # the owner allowed: ask the owner once whether to listen.
            logger.info("group %s: %s is not on the list; asking the owner", address, inbound.user_id)
            self._ask_about_person(inbound, address, group.name)
            return None
        return ("social", person.name)

    def _addressed(self, inbound: Inbound, address: str) -> bool:
        """In a group, Fulla answers when named up front, mentioned by its
        bot handle, or replied to."""
        text = inbound.text or ""
        if ADDRESSED_RE.match(text):
            return True
        transport = self._transport(address)
        me = {str(getattr(transport, "number", "") or "")} - {""}
        username = getattr(transport, "username", lambda: "")()
        if username:
            me.add(f"@{username}".lower())
            if f"@{username}".lower() in text.lower():
                return True
        if any(str(m).lower() in me for m in inbound.mentions):
            return True
        return inbound.reply_to_message_id is not None and ref(address, inbound.reply_to_message_id) in self.state.texts

    @staticmethod
    def _group_tag(address: str) -> str:
        """A short handle for a group in button data, which Telegram caps at 64 bytes."""
        return hashlib.sha1(address.encode()).hexdigest()[:10]

    def _ask_about_group(self, inbound: Inbound, address: str) -> None:
        transport = self._transport(address)
        name = inbound.chat_title or getattr(transport, "group_name", lambda c: "")(inbound.chat_id) or ""
        group = self.people.remember_group(address, name)  # pyright: ignore[reportOptionalMemberAccess]
        if group.asked:
            logger.info("group %s: the owner was asked already (%s)", address, group.asked)
            return
        logger.info("group %s (%s): asking the owner at %s", address, group.name, self.primary)
        tag = self._group_tag(address)
        question = (f"You added me to {group.name} on {transport.name}. May I talk there? I'd answer only when "
                    f"someone addresses me, only people on your list, with none of your facts or tools.")
        try:
            r = self._send(self.primary, question, buttons=[[("Yes", f"g:{tag}:yes"), ("No", f"g:{tag}:no")]])
        except Exception:
            logger.exception("could not ask about group %s", address)
            return
        self.people.mark_asked(address, r)  # pyright: ignore[reportOptionalMemberAccess]
        with self._lock:
            self.state.group_asks[r] = address
            self.state.texts[r] = question
            self.state.save()

    def _answer_group_ask(self, group_address: str, allow: bool, ask_ref: str | None) -> str:
        group = self.people.allow_group(group_address, allow) if self.people else None
        if group is None:
            return "That group is gone from my list."
        if allow:
            self._sync_groups()  # an allowed group gets its directory at once
        note = f"I'll talk in {group.name} when addressed." if allow else f"I'll stay quiet in {group.name}."
        if ask_ref is not None:
            question = self.state.texts.get(ask_ref, "")
            try:
                self._edit(ask_ref, f"{question}\n\n→ {note}".strip())
            except Exception:
                logger.info("could not edit the group question %s", ask_ref)
            with self._lock:
                self.state.group_asks.pop(ask_ref, None)
                self.state.save()
        return note

    def _group_by_tag(self, tag: str) -> str | None:
        if self.people is None:
            return None
        return next((g.address for g in self.people.groups() if self._group_tag(g.address) == tag), None)

    def _ask_about_person(self, inbound: Inbound, address: str, group_name: str) -> None:
        transport = address.split(":", 1)[0]
        person = self.people.remember_person(transport, inbound.user_id, inbound.sender_name)  # pyright: ignore[reportOptionalMemberAccess]
        if person.asked:
            return  # asked already; the owner said no, or has not answered
        tag = self._group_tag(f"{transport}:{inbound.user_id}")
        who = f"{inbound.sender_name} ({transport} id {inbound.user_id})" if inbound.sender_name \
            else f"someone with no name shown ({transport} id {inbound.user_id})"
        question = (f"In {group_name}, {who} just addressed me, and they are not on your list. "
                    f"Shall I listen to them? They would get the social me only. Say no and I'll ignore them.")
        try:
            r = self._send(self.primary, question, buttons=[[("Yes", f"p:{tag}:yes"), ("No", f"p:{tag}:no")]])
        except Exception:
            logger.exception("could not ask about %s", inbound.user_id)
            return
        self.people.mark_person_asked(transport, inbound.user_id, r)  # pyright: ignore[reportOptionalMemberAccess]
        with self._lock:
            self.state.person_asks[r] = f"{transport}:{inbound.user_id}"
            self.state.texts[r] = question
            self.state.save()

    def _answer_person_ask(self, person_key: str, allow: bool, ask_ref: str | None) -> str:
        transport, user_id = person_key.split(":", 1)
        person = self.people.decide_person(transport, user_id, allow) if self.people else None
        if person is None:
            return "I no longer have that person on my list."
        note = f"I'll listen to {person.name}." if allow else f"I'll ignore {person.name}."
        if ask_ref is not None:
            question = self.state.texts.get(ask_ref, "")
            try:
                self._edit(ask_ref, f"{question}\n\n→ {note}".strip())
            except Exception:
                logger.info("could not edit the person question %s", ask_ref)
            with self._lock:
                self.state.person_asks.pop(ask_ref, None)
                self.state.save()
        return note

    def _answer_ask(self, ask_ref: str, allow: bool) -> bool:
        """If `ask_ref` is an open question about a group or a person, answer
        it. False when it is neither."""
        group_address = self.state.group_asks.get(ask_ref)
        if group_address is not None:
            self._answer_group_ask(group_address, allow, ask_ref)
            return True
        person_key = self.state.person_asks.get(ask_ref)
        if person_key is not None:
            self._answer_person_ask(person_key, allow, ask_ref)
            return True
        return False

    def _person_by_tag(self, tag: str) -> str | None:
        if self.people is None:
            return None
        for p in self.people.people():
            for transport, ids in p.ids.items():
                for user_id in ids:
                    if self._group_tag(f"{transport}:{user_id}") == tag:
                        return f"{transport}:{user_id}"
        return None

    # ------------------------------------------------------------ friends, groups

    def _today(self) -> str:
        return datetime.now(UTC).date().isoformat()

    def _name_me(self, address: str, text: str) -> str:
        """A mention arrives as a number or account id. Fulla does not know
        which are Fulla's, and took a mention of its own number for someone else's
        conversation; and a bare account id says nothing about who was meant.
        Name it, and name everyone on the owner's list the same way."""
        text = text.strip()
        me = str(getattr(self._transport(address), "number", "") or "")
        if me:
            text = text.replace(f"@{me}", "@Fulla")
        if self.people is not None and "@" in text:
            transport = address.split(":", 1)[0]
            for person in self.people.people():
                for user_id in person.ids.get(transport, []):
                    text = text.replace(f"@{user_id}", f"@{person.name}")
        return text

    def _label(self, owner: bool, person, inbound: Inbound) -> str:
        """How a speaker is named in a group's thread: the store's name wins,
        then what the messenger said, then the bare id."""
        if owner:
            return f"{self.owner_name} (the owner)"
        return (person.name if person else "") or inbound.sender_name or str(inbound.user_id)

    def _remember_in_group(self, address: str, who: str, text: str, r: str | None = None) -> None:
        """Keep a group's last few messages, Fulla's included, as the thread an
        addressed message is read against. Only allowed groups get here."""
        if not self.group_context or not text:
            return
        if len(text) > GROUP_LINE_MAX:
            text = text[:GROUP_LINE_MAX - 1] + "…"
        with self._lock:
            ring = self.state.group_ring.setdefault(address, [])
            ring.append({"who": who, "text": text, "ref": r, "at": time.time()})
            del ring[:-self.group_context - 1]  # the newest may be the one being answered; keep one more
            self.state.save()

    def _group_thread(self, address: str, where: str, exclude: str | None) -> str:
        """The group's recent messages before `exclude`, as untrusted context,
        or "" when there are none. The whole block is data, the owner's lines
        too: their words are instructions only when they address Fulla."""
        cutoff = time.time() - GROUP_CONTEXT_AGE
        entries = [e for e in self.state.group_ring.get(address, [])
                   if e.get("at", 0) >= cutoff and (exclude is None or e.get("ref") != exclude)]
        entries = entries[-self.group_context:] if self.group_context else []
        if not entries:
            return ""
        lines = "\n".join(f"{e['who']}: {e['text']}" for e in entries)
        return untrusted("group thread", f"the messages in {where} before the one addressed to you", lines)

    def _under_cap(self, address: str) -> bool:
        """Count a message against the day's cap for this conversation; say
        once when the cap is reached, nothing after."""
        with self._lock:
            day, n = self.state.social_counts.get(address) or [self._today(), 0]
            if day != self._today():
                day, n = self._today(), 0
            n += 1
            self.state.social_counts[address] = [day, n]
            self.state.save()
        if n <= self.social_cap:
            return True
        if n == self.social_cap + 1:
            try:
                self._send(address, CAPPED)
            except Exception:
                logger.exception("could not say the cap was reached at %s", address)
        return False

    def _handle_social(self, inbound: Inbound, address: str, speaker: str) -> None:
        """A friend's message, or group talk: to the social agent, in that
        address's own conversation, marked with who said it. The owner's
        words are theirs; anyone else's are data, never instructions."""
        body = social_body(speaker, self._name_me(address, inbound.text), self.owner_name)
        thread = ""
        if inbound.group:
            group = self.people.group(address) if self.people else None
            where = inbound.chat_title or (group.name if group else "a group")
            body = f"[In the group {where}, addressed to you]\n{body}"
            thread = self._group_thread(address, where, ref(address, inbound.message_id))
        if not self._under_cap(address):
            return
        who = {"name": speaker, "transport": address.split(":", 1)[0], "user_id": str(inbound.user_id)}
        with self._lock:
            waiting = self.state.social_pending.get(address)
            if waiting:  # a run is in flight there; keep the order
                waiting.append(body)
                _add_speaker(self.state.pending_speakers.setdefault(address, []), who)
                self.state.save()
        if waiting:
            self._unblock_social(address)  # unless what holds the chat is a run asleep in `wait`
            return
        # The thread goes with the first message a run gets, whether it starts
        # the run or waits for one; a message queued behind one already
        # waiting does without, and the next turn's thread covers the gap.
        self._send_social(address, [f"{thread}\n\n{body}" if thread else body], [who])

    def _send_social(self, address: str, bodies: list[str], speakers: Sequence[dict] = (), *,
                     typing: bool = True) -> None:
        """`speakers` are who wrote `bodies` (several, when a busy chat's
        messages were bundled); the run is remembered as answering them all,
        so a tool it calls can ask who asked and tag the right one."""
        agent = self._agent_for(address)
        try:
            try:
                result = self.client.send_message(agent, "\n\n".join(bodies),
                                                  conversation_key=f"{SOCIAL_KEY}{address}")
            except Busy:
                raise
            except Exception:
                if agent == self.social_agent:
                    raise
                # The group's agent is not on the server yet (its push failed,
                # or no worker has joined since the group was allowed): the
                # direct-chat agent answers meanwhile. It holds less, never more.
                logger.exception("send_message to %s failed; falling back to %s", agent, self.social_agent)
                agent = self.social_agent
                result = self.client.send_message(agent, "\n\n".join(bodies),
                                                  conversation_key=f"{SOCIAL_KEY}{address}")
        except Busy:
            with self._lock:
                self.state.social_pending.setdefault(address, []).extend(bodies)
                waiting = self.state.pending_speakers.setdefault(address, [])
                for who in speakers:
                    _add_speaker(waiting, who)
                self.state.save()
            self._unblock_social(address)
            return
        except Exception:
            logger.exception("send_message to %s failed", agent)
            return
        with self._lock:
            self.state.origins[str(result.run_id)] = address
            self.state.social_origins[str(result.run_id)] = address
            if speakers:
                self.state.speakers[str(result.run_id)] = list(speakers)
            self.state.save()
        if typing:
            self._typing(address)

    def speakers_of(self, run_id: int) -> list[dict]:
        """Who wrote the messages a social run is answering, in order, for
        tools that need to know who asked (`runctx.current_speakers`)."""
        return list(self.state.speakers.get(str(run_id), []))

    def social_origin_of(self, run_id: int) -> str | None:
        """The address a social run answers, for tools that must know which
        group they serve (`runctx.current_address`); kept after the run ends."""
        return self.state.social_origins.get(str(run_id))

    def _agent_for(self, address: str) -> str:
        """Who answers at this address: the group's own agent when the group
        has a directory, else the direct-chat social agent."""
        record = self.groups.by_address(address) if self.groups is not None else None
        return group_agent_name(record) if record is not None else self.social_agent

    def _sync_groups(self) -> None:
        """Keep each allowed group's directory in step with the people store,
        and declare the agent of a group that just got one."""
        if self.groups is None or self.people is None:
            return
        try:
            before = {r.id for r in self.groups.all()}
            records = self.groups.sync(self.people)
        except Exception:
            logger.exception("could not sync the group directories")
            return
        if self.push_groups is not None and {r.id for r in records} - before:
            try:
                self.push_groups()
            except Exception:
                logger.exception("could not declare the new group's agent; the next join will")

    def _unblock_social(self, address: str) -> None:
        """A friends' chat is busy. If the run holding it is asleep in `wait`
        (the one built-in the worker cannot take from the model on an old
        definition), cancel it: its run.failed event flushes what is waiting.
        A run that is thinking or reading is left alone."""
        in_flight = next((int(r) for r, a in reversed(list(self.state.origins.items())) if a == address), None)
        if in_flight is None:
            return
        try:
            if self.client.last_event_type(in_flight) != "waiting_for_timer":
                return
            logger.warning("social run %s at %s is asleep in wait; cancelling it so the chat can go on",
                           in_flight, address)
            self.client.cancel_run(in_flight)
        except Exception as e:
            logger.info("could not look at or cancel run %s: %s", in_flight, e)

    def _relay_group_reminder(self, run_id: int, output: str | None, *, failed: str | None = None) -> None:
        """A reminder set from a friends' chat came due: post its line back
        into the chat that set it, found through the run that scheduled it."""
        with self._lock:
            if run_id in self.state.relayed:
                return
            self.state.relayed.append(run_id)
            self.state.save()
        entry = self.group_reminder_known(run_id) or {}
        target = self.state.social_origins.get(str(entry.get("parent")))
        if target is None:
            logger.warning("group reminder %s: no chat to post into (parent run %s unknown)", run_id, entry.get("parent"))
            return
        if failed is not None:
            logger.warning("group reminder %s for %s failed: %s", run_id, target, failed)
            return
        what = (entry.get("what") or "").strip() or (output or "").strip()
        if entry.get("followup"):
            # A check-back the group's agent set itself: back into its own
            # conversation, never posted. It counts against the day's cap,
            # so a job polled forever stops when the chat's turns run out.
            if self._under_cap(target):
                self._send_social(target, [f"{FOLLOWUP_MARK} {what}"], typing=False)
            return
        asked_by = entry.get("asked_by") or {}
        mentions = None
        if asked_by.get("name") and asked_by.get("transport") == target.split(":", 1)[0]:
            # Tag whoever asked, so the reminder reaches them and not everyone.
            text = f"⏰ @{asked_by['name']}: {what}"
            mentions = [(asked_by.get("user_id", ""), asked_by["name"])]
        else:
            text = f"⏰ Reminder: {what}"
        try:
            r = self._send(target, text, mentions=mentions)
            with self._lock:
                self.state.texts[r] = text
                self.state.save()
            self._remember_in_group(target, "Fulla", text, r)
        except Exception:
            logger.exception("could not post group reminder %s at %s", run_id, target)

    def _flush_social(self, address: str) -> None:
        with self._lock:
            bodies = self.state.social_pending.pop(address, [])
            speakers = self.state.pending_speakers.pop(address, [])
            self.state.save()
        if bodies:
            self._send_social(address, bodies, speakers)

    def _relay_social(self, run_id: int, output: str | None, *, failed: str | None = None) -> None:
        """A social run ended: its answer goes to the friend or group, plain
        text, never a reaction (in a group the message reacted to may be
        anyone's). A failure is logged, not announced there."""
        with self._lock:
            if run_id in self.state.relayed:
                return
            self.state.relayed.append(run_id)
            target = self.state.origins.pop(str(run_id), None)
            self.state.save()
        if target is None:
            return
        if failed is not None:
            logger.warning("social run %s at %s failed: %s", run_id, target, failed)
        else:
            text = (output or "").strip()
            if not is_silent(text):
                try:
                    r = self._send(target, text)
                    with self._lock:
                        self.state.texts[r] = text
                        self.state.save()
                    if self.people is not None and self.people.group(target) is not None:
                        self._remember_in_group(target, "Fulla", text, r)
                except Exception:
                    logger.exception("could not answer at %s", target)
        self._flush_social(target)

    def _handle_reaction(self, inbound: Inbound, address: str) -> None:
        """A reaction on a question answers it when it plainly means an answer;
        on anything else we posted, it is a short reply to the manager. On a
        message we don't know (the owner's own, or one too old to remember)
        it means nothing to anyone."""
        emoji, r = inbound.text, ref(address, inbound.message_id)
        key = f"{r}:{emoji}:{inbound.date}"
        with self._lock:
            if key in self.state.reactions:
                return
            self.state.reactions.append(key)
            self.state.save()
        if self._card_reaction(inbound, address, emoji):
            return
        run_id = self.state.message_runs.get(r)
        if run_id is None and emoji in CANCEL_REACTIONS:
            run_id = self._working_run(r)
            if run_id is not None:
                self._cancel(run_id, None, address)
                return
        if run_id is None and emoji in QUESTION_REACTIONS and self._answer_ask(r, QUESTION_REACTIONS[emoji] == "yes"):
            return
        if run_id is not None:
            reminder = self.state.question_agents.get(r) == self._our_agent_ids().get(self.reminder_agent)
            answer = (REMINDER_REACTIONS if reminder else QUESTION_REACTIONS).get(emoji)
            if answer:
                self._answer(run_id, answer, r)
            return
        text = self.state.texts.get(r)
        if text is None:
            return
        if len(text) > QUOTE_MAX:
            text = text[:QUOTE_MAX].rstrip() + "…"
        self._chat(f'[reaction] {emoji} on your message: "{text}"', address, working=False, answers=False)

    def _handle_callback(self, inbound: Inbound, address: str) -> None:
        data = inbound.data or ""
        if data.startswith("k:"):
            self._confirm_tap(data, inbound.callback_id, address, inbound)
            return
        parts = data.split(":", 2)
        transport = self._transport(address)
        if len(parts) == 2 and parts[0] == "c" and parts[1].isdigit():
            self._cancel(int(parts[1]), inbound.callback_id, address)
            return
        if len(parts) == 3 and parts[0] == "g":
            group_address = self._group_by_tag(parts[1])
            ask_ref = ref(address, inbound.message_id) if inbound.message_id is not None else None
            note = (self._answer_group_ask(group_address, parts[2] == "yes", ask_ref)
                    if group_address else "That group is gone from my list.")
            transport.answer_callback(inbound.callback_id, note)
            return
        if len(parts) == 3 and parts[0] == "p":
            person_key = self._person_by_tag(parts[1])
            ask_ref = ref(address, inbound.message_id) if inbound.message_id is not None else None
            note = (self._answer_person_ask(person_key, parts[2] == "yes", ask_ref)
                    if person_key else "I no longer have that person on my list.")
            transport.answer_callback(inbound.callback_id, note)
            return
        if len(parts) != 3 or parts[0] != "a":
            transport.answer_callback(inbound.callback_id, "Unknown button")
            return
        run_id, answer = int(parts[1]), parts[2]
        self._answer(run_id, answer, ref(address, inbound.message_id) if inbound.message_id is not None else None)
        transport.answer_callback(inbound.callback_id)

    def _handle_text(self, inbound: Inbound, address: str) -> bool:
        """True once the message is safe; False when it is kept in `pending`
        because Norns could not be reached."""
        text = inbound.text.strip()
        # A card comes first: a quoted reply to one is about the card, a
        # cancel word included, and while an edit is waiting the next message
        # is its new text, unless it is a cancel word (see `_edit_target`).
        if self._card_reply(inbound, address, text):
            return True
        editing = self._edit_target(inbound, address, text)
        if editing is not None:
            self._confirm_revise(editing, inbound.text)
            return True
        if inbound.reply_to_message_id is not None:
            quoted = ref(address, inbound.reply_to_message_id)
            run_id = self.state.message_runs.get(quoted)
            if run_id is not None:
                self._answer(run_id, text, quoted)
                return True
            if text.lower() in YES_WORDS | NO_WORDS and self._answer_ask(quoted, text.lower() in YES_WORDS):
                return True
            run_id = self._working_run(quoted)
            if run_id is not None and is_cancel(text):
                self._cancel(run_id, None, address)
                return True
        elif not self._transport(address).buttons and self._answer_by_label(inbound, address, text):
            return True
        if text.lower() in YES_WORDS | NO_WORDS and self.state.parked is None:
            # A bare yes or no, nothing quoted: it answers the one question
            # about a group or a person still open, if there is exactly one.
            open_asks = list(self.state.group_asks) + list(self.state.person_asks)
            if len(open_asks) == 1 and self._answer_ask(open_asks[0], text.lower() in YES_WORDS):
                return True
        if is_cancel(text):
            run_id = self._run_in_flight(address)
            if run_id is not None:
                self._cancel(run_id, None, address)
                return True
            if self._drop_edit(address):
                return True
        if text == "/start":
            self._send(
                address,
                "Hi. Tell me things worth remembering, ask me about them, or say "
                "\"remind me to … at …\". Reply to a question to answer it.",
            )
            return True
        if text == "/reset":
            self._reset(address)
            return True
        if text.lower().startswith("/reset "):
            self._reset_social(address, text[len("/reset "):].strip())
            return True
        prompt = ref(address, inbound.message_id) if inbound.message_id is not None else None
        return self._chat(text, address, prompt=prompt)

    def _answer_by_label(self, inbound: Inbound, address: str, text: str) -> bool:
        """On a messenger without buttons, a message that is exactly one of
        the labels on an open question at this address, nothing quoted, is
        that button: the newest question that offers it. SMS has no quoting
        at all, and on Signal the owner may just not have swiped. A card's
        labels count the same way when one card is open there. Anything
        else, a word that is no label included, is not touched."""
        said = text.rstrip(".!").casefold()
        if not said:
            return False
        with self._lock:
            questions = [(r, run_id) for r, run_id in self.state.message_runs.items()
                         if split_ref(r)[0] == address and r in self.state.question_agents]
            agents = {r: self.state.question_agents[r] for r, _ in questions}
            cards = [Confirmation(**raw) for raw in self.state.confirms.values()]
            cards = [c for c in cards if c.address == address and c.status in confirm.OPEN and c.message_id is not None]
        for r, run_id in reversed(questions):
            for row in self._buttons_for(run_id, agents[r]):
                for label, data in row:
                    if label.casefold() == said:
                        self._answer(run_id, data.split(":", 2)[2], r)
                        return True
        if len(cards) == 1:
            conf = cards[0]
            data = next((d for row in self._all_buttons(conf) for label, d in row if label.casefold() == said), None)
            if data is not None:
                if self._may_decide(inbound, address, conf):
                    self._confirm_tap(data, None, address, inbound)
                return True
        return False

    def _reset(self, address: str) -> None:
        """Start the standing conversation over. Facts, directives, feedback, the
        soul and reminders live in their own stores and survive; only the chat window
        goes. For when a run of wrong answers has become the context."""
        try:
            existed = self.client.delete_conversation(self.manager_agent, self.conversation_key)
        except Exception as e:
            logger.exception("reset failed")
            self._send(address, f"I couldn't reset the conversation: {e}")
            return
        self._conversation_id = None
        with self._lock:
            self.state.parked = None
            self.state.save()
        self._send(address, "Fresh start. I still have your facts, standing instructions, "
                   "reminders and who I am; today's chat is gone." if existed else
                   "Nothing to reset; the next message starts a conversation.")

    def _reset_social(self, address: str, name: str) -> None:
        """Start a group's or a friend's conversation over, by name, from the
        owner's own chat: the social conversation Norns holds for it, a run in
        flight there, what is queued behind it, and the group's thread. The
        people list, their reminders and the day's count stay. For when a
        wrong answer of Fulla's there has become the context, since no prompt
        line argues a model out of its own history."""
        if self.people is None:
            self._send(address, "Nobody else talks to me, so there is nothing to reset.")
            return
        group = self.people.group_named(name)
        if group is not None:
            targets, who = [group.address], group.name
        else:
            person = self.people.person_named(name)
            if person is None:
                self._send(address, f"I don't know a group or person called {name}. `list_people` shows who I know.")
                return
            targets = [f"{t}:{i}" for t, ids in person.ids.items() if t in self.transports for i in ids]
            who = person.name
        existed = False
        for target in targets:
            in_flight = next((int(r) for r, a in reversed(list(self.state.origins.items())) if a == target), None)
            if in_flight is not None:
                try:
                    self.client.cancel_run(in_flight)
                except Exception:
                    logger.exception("could not cancel run %s at %s before the reset", in_flight, target)
            try:
                # The group's own agent, and the direct-chat one it may have
                # talked through before it had an agent, or as the fallback.
                for agent in dict.fromkeys((self._agent_for(target), self.social_agent)):
                    existed |= self.client.delete_conversation(agent, f"{SOCIAL_KEY}{target}")
            except Exception as e:
                logger.exception("reset of %s failed", target)
                self._send(address, f"I couldn't reset {who}: {e}")
                return
            with self._lock:
                self.state.social_pending.pop(target, None)
                self.state.pending_speakers.pop(target, None)
                self.state.group_ring.pop(target, None)
                self.state.save()
        self._send(address, f"Fresh start with {who}; what we said there is gone. Their reminders and today's count stay."
                   if existed else f"Nothing to reset with {who}; the next message there starts a conversation.")

    def _answer(self, run_id: int, answer: str, question_ref: str | None) -> None:
        asked = self._asks(run_id)
        try:
            self.client.reply(run_id, answer)
            note = f"→ {answer}"
            answered = True
        except Exception as e:
            logger.warning(f"reply to run {run_id} failed: {e}")
            note = "→ (that one is no longer waiting)"
            answered = False
        agent_id = None
        if question_ref is not None:
            question = self.state.questions.get(question_ref, "")
            self._edit(question_ref, f"{question}\n\n{note}".strip())
            with self._lock:
                self.state.message_runs.pop(question_ref, None)
                self.state.questions.pop(question_ref, None)
                agent_id = self.state.question_agents.pop(question_ref, None)
                if self.state.parked == run_id:
                    self.state.parked = None
                # The run carries on where the owner answered it.
                self.state.origins[str(run_id)] = split_ref(question_ref)[0]
                self.state.save()
        # The manager picks up where it asked; a reminder goes back to waiting,
        # which is not work worth a bubble.
        if answered and agent_id == self._our_agent_ids().get(self.manager_agent):
            self._start_working(run_id, asked)

    def _chat(self, text: str, origin: str, *, prompt: str | None = None, working: bool = True,
              answers: bool = True) -> bool:
        """Send and return. The reply arrives as a run.completed event, or the
        question as a run.waiting one; either way Norns calls us back, and
        the answer goes to `origin`. Behind anything still pending, so the
        manager reads them in the order typed. A reaction is sent with
        `working` off: a thumbs-up is not worth a Working… message, and its
        usual answer is silence.

        While the conversation is parked on a question, Norns takes the next
        message as the answer, as it always has; pending messages are new chat
        and wait for the run to end rather than be read as that answer. A
        reaction on some other message is never that answer (`answers` off),
        so while parked it waits with them, and the owner is told once which
        question it waits on: a question can stay open for days, and the
        reaction is a sign they forgot it."""
        with self._lock:
            parked = self.state.parked
            held = parked is not None and not answers
            queue = parked is None or held
            tell = False
            if queue:
                self.state.pending.append(text)
                if prompt is not None:
                    self.state.pending_prompt = prompt
                self.state.pending_origin = origin
                self.state.pending_working = self.state.pending_working or working
                tell = held and not self.state.queued_told
                self.state.queued_told = self.state.queued_told or tell
                self.state.save()
        if held:
            if tell:
                self._send(origin, self._held_notice(parked))  # pyright: ignore[reportArgumentType]
            return True
        if queue:
            return self._flush(from_owner=True)
        try:
            result = self.client.send_message(
                self.manager_agent, text, conversation_key=self.conversation_key)
        except Exception as e:
            # The answer to the parked question has nowhere to wait: it is
            # not kept, so the messenger must bring it again. Say so once.
            logger.exception("send_message failed")
            if prompt not in self._unreachable_told:
                self._unreachable_told.add(prompt)
                self._send(origin, f"I couldn't reach Norns: {e}. I'll keep trying.")
            raise NotTaken(str(e)) from e
        with self._lock:
            if result.run_id == parked:
                self.state.parked = None
            if prompt is not None:
                self.state.prompts[str(result.run_id)] = prompt
            self.state.origins[str(result.run_id)] = origin
            self.state.save()
        if working:
            self._start_working(result.run_id)
        return True

    def _held_notice(self, run_id: int) -> str:
        """Which open question a held message waits on, quoted when we still
        have it; the question's message may have fallen out of state."""
        question = next((q for r, q in self.state.questions.items()
                         if self.state.message_runs.get(r) == run_id), None)
        if question is None:
            return HELD
        if len(question) > QUOTE_MAX:
            question = question[:QUOTE_MAX].rstrip() + "…"
        return f'{HELD_ON} "{question}"'

    def _flush(self, *, from_owner: bool = False) -> bool:
        """Send everything pending as one message. While a run is under way
        Norns takes it into that run (`interject`) before its next model call,
        so a second ask is not stuck behind a slow first; the run's one reply
        answers both, and goes where the newest message came from. Kept if
        Norns is busy (an older Norns) or unreachable; a pending message
        leaves the list only once Norns has it.
        A one-emoji reply to the batch reacts to the newest owner message in
        it; the Working… message is shown if anything in the batch earned one;
        the batch's answers go where its newest message came from.
        False only when Norns was unreachable: the batch is kept, and the
        caller may ask the messenger to come back. Busy is True; the run's
        end event flushes."""
        with self._flush_lock:
            with self._lock:
                if not self.state.pending or self.state.parked is not None:
                    return True
                batch = list(self.state.pending)
                prompt, working = self.state.pending_prompt, self.state.pending_working
                origin = self.state.pending_origin or self.primary
            try:
                result = self.client.send_message(
                    self.manager_agent, "\n\n".join(batch), conversation_key=self.conversation_key,
                    interject=True)
            except Busy:
                with self._lock:
                    tell = not self.state.queued_told
                    self.state.queued_told = True
                    self.state.save()
                if tell:
                    self._send(origin, QUEUED)
                return True
            except Exception as e:
                logger.exception("send_message failed")
                if from_owner:
                    self._send(origin, f"I couldn't reach Norns: {e}. I've kept your message "
                               "and will keep trying.")
                return False
            with self._lock:
                del self.state.pending[:len(batch)]
                if not self.state.pending:
                    self.state.pending_prompt, self.state.pending_origin = None, None
                    self.state.pending_working = False
                if prompt is not None:
                    self.state.prompts[str(result.run_id)] = prompt
                self.state.origins[str(result.run_id)] = origin
                self.state.queued_told = False
                self.state.save()
        if working:
            self._start_working(result.run_id)
        return True

    def _working_run(self, r: str) -> int | None:
        """The run whose Working… message this is, if it is one."""
        for key, working_ref in self.state.working.items():
            if working_ref == r:
                return int(key)
        return None

    def _run_in_flight(self, address: str) -> int | None:
        """The newest run shown as working at this address, which is what
        "wait!" or "nvm" means to stop."""
        for key, working_ref in reversed(list(self.state.working.items())):
            if split_ref(working_ref)[0] == address:
                return int(key)
        return None

    def _ack(self, callback_id: str | None, text: str, address: str) -> None:
        """Tell the owner something short about a button they pressed: as the
        button's toast when there was a button, as a message when the
        "button" was a reply or a reaction."""
        if callback_id is not None:
            self._transport(address).answer_callback(callback_id, text)
        else:
            self._send(address, text)

    def _cancel(self, run_id: int, callback_id: str | None, address: str) -> None:
        try:
            cancelled = self.client.cancel_run(run_id)
        except Exception as e:
            logger.exception(f"cancel of run {run_id} failed")
            self._ack(callback_id, f"Couldn't cancel: {e}", address)
            return
        if not cancelled:
            self._ack(callback_id, "Norns wouldn't cancel that run: it has finished, or this Norns "
                      "has no cancel yet.", address)
            return
        with self._lock:
            # Whatever Norns says about this run from here on, we've told the
            # owner what happened to it.
            if run_id not in self.state.relayed:
                self.state.relayed.append(run_id)
            self._finish_working_locked(run_id, note=CANCELLED)
            self.state.prompts.pop(str(run_id), None)
            self.state.origins.pop(str(run_id), None)
            self.state.save()
        if callback_id is not None:
            self._transport(address).answer_callback(callback_id, "Cancelled")

    # ---------------------------------------------------------- working runs

    def _start_working(self, run_id: int, asked: frozenset[str] = frozenset()) -> None:
        """Show that a manager run is under way: a Working… message with a
        Cancel button, and the typing indicator until its event arrives.
        `asked` is the run's questions already posted when the owner set it
        going again; a newer one means its next event beat us here."""
        key = str(run_id)
        target = self._target(run_id)
        with self._lock:
            if key in self.state.working or self._finished(run_id, asked):
                return
            if self._transport(target).buttons:
                try:
                    r = self._send(target, WORKING, buttons=[[("Cancel", f"c:{run_id}")]])
                except Exception:
                    logger.exception("could not post the working message")
                    return
            else:
                # No buttons, so no message: the typing indicator is the sign
                # of work, and a word ("wait", "cancel") is the Cancel button.
                r = ref(target, NO_MESSAGE)
            self.state.working[key] = r
            self.state.save()
        self._typing(target)
        if self.typing_interval > 0:
            threading.Thread(target=self._keep_typing, args=(run_id, target), daemon=True).start()

    def _asks(self, run_id: int) -> frozenset[str]:
        prefix = f"{run_id}:"
        return frozenset(k for k in self.state.posted if k.startswith(prefix))

    def _finished(self, run_id: int, asked: frozenset[str]) -> bool:
        """Has this run's next event already reached the owner? A fast run's
        answer or next question can land before the Working… message is
        posted; then don't post it."""
        return run_id in self.state.relayed or bool(self._asks(run_id) - asked)

    def _finish_working_locked(self, run_id: int, *, note: str | None = None) -> None:
        """Take down the run's Working… message. Caller holds the lock and
        saves. Deleted when the run simply finished; edited to `note` when
        there is something to say in its place."""
        r = self.state.working.pop(str(run_id), None)
        if r is None:
            return
        address, message_id = split_ref(r)
        try:
            if message_id == NO_MESSAGE:
                if note is not None:
                    self._send(address, note)
                return
            if note is None and self._delete(r):
                return
            self._edit(r, note or "Done.")
        except Exception:
            logger.exception(f"could not take down the working message for run {run_id}")

    def _typing(self, address: str) -> None:
        try:
            self._transport(address).send_chat_action(self._chat_id(address))
        except Exception as e:
            logger.info(f"sendChatAction failed: {e}")

    def _keep_typing(self, run_id: int, address: str) -> None:
        key = str(run_id)
        deadline = time.monotonic() + TYPING_MAX
        while time.monotonic() < deadline:
            time.sleep(self.typing_interval)
            if key not in self.state.working:
                return
            self._typing(address)

    # ----------------------------------------------------------------- outbound

    def handle_event(self, event: dict) -> None:
        """A run webhook delivery from Norns, already signature-checked."""
        kind = event.get("type")
        if kind == "run.test":
            return
        run_id = event["run_id"]
        if kind in ("run.completed", "run.failed"):
            failed = (event.get("error") or "failed") if kind == "run.failed" else None
            if self._confirm_outcome(run_id, event.get("output"), failed=failed):
                return
        if kind == "run.waiting" and (self._unattended(event.get("agent_id")) or self._is_social(event.get("agent_id"))):
            self._unstick(run_id, event.get("tool_call_id") or "")
            return
        if not self._ours(run_id, event.get("agent_id"), event.get("conversation_id")):
            logger.info("run %s is not ours (agent %s, conversation %s); not relayed",
                        run_id, event.get("agent_id"), event.get("conversation_id"))
            return
        if self._is_social(event.get("agent_id")):
            if kind == "run.completed":
                self._relay_social(run_id, event.get("output"))
            elif kind == "run.failed":
                self._relay_social(run_id, None, failed=event.get("error") or "failed")
            return
        if event.get("agent_id") == self._our_agent_ids().get(self.group_reminder_agent):
            if kind == "run.completed":
                self._relay_group_reminder(run_id, event.get("output"))
            elif kind == "run.failed":
                self._relay_group_reminder(run_id, None, failed=event.get("error") or "failed")
            return
        if kind == "run.waiting":
            if self._internal_wait(event.get("agent_id")):
                return
            if self._on_a_timer(event.get("agent_id"), event.get("trigger_type")):
                self._unstick(run_id, event.get("tool_call_id") or "")
                return
            self._post_question(run_id, event["agent_id"], event["question"], event["tool_call_id"])
        elif kind == "run.completed":
            self._relay(run_id, event.get("output"))
        elif kind == "run.failed":
            self._relay(run_id, None, failed=event.get("error") or "failed")
        if kind in ("run.completed", "run.failed") and self._is_manager(event.get("agent_id")):
            self._flush()

    def reconcile(self) -> None:
        """Post any question still parked that we never posted, and take down
        Working… messages whose runs ended while nothing was listening, then
        send what is pending. For a cold start, when a delivery may have come
        in the meantime."""
        for run in self.client.list_runs(limit=100, status="waiting"):
            if run.status != "waiting" or not run.waiting_for or self._internal_wait(run.agent_id):
                continue
            if self._unattended(run.agent_id) or self._is_social(run.agent_id):
                self._unstick(run.run_id, run.waiting_for.tool_call_id)
                continue
            if self._ours(run.run_id, run.agent_id, run.conversation_id):
                if self._on_a_timer(run.agent_id, run.trigger_type):
                    self._unstick(run.run_id, run.waiting_for.tool_call_id)
                    continue
                self._post_question(
                    run.run_id, run.agent_id, run.waiting_for.question, run.waiting_for.tool_call_id
                )
        for key in list(self.state.working):
            try:
                status = self.client.get_run(int(key)).status
            except Exception as e:
                logger.warning(f"could not check run {key}: {e}")
                status = "gone"
            if status in ("waiting", "completed", "failed", "error", "gone"):
                with self._lock:
                    self._finish_working_locked(int(key))
                    self.state.save()
        for conf in self._confirms_in_flight():
            try:
                found = self.client.get_run(conf.run_id)  # pyright: ignore[reportArgumentType]
            except Exception as e:
                logger.warning(f"could not check run {conf.run_id}: {e}")
                continue
            if found.status in RUN_TERMINAL:
                self._confirm_outcome(conf.run_id, found.output, failed=None if found.status == "completed"  # pyright: ignore[reportArgumentType]
                                      else found.status)
        if self.state.parked is not None:
            try:
                still = self.client.get_run(self.state.parked).status == "waiting"
            except Exception as e:
                logger.warning(f"could not check run {self.state.parked}: {e}")
                still = False
            if not still:
                with self._lock:
                    self.state.parked = None
                    self.state.save()
        self._flush()

    def _post_question(self, run_id: int, agent_id: int, question: str, tool_call_id: str) -> None:
        key = f"{run_id}:{tool_call_id}"
        target = self._target(run_id)
        with self._lock:
            if key in self.state.posted:
                return
            self._finish_working_locked(run_id)
            r = self._send(target, question, buttons=self._buttons_for(run_id, agent_id))
            self.state.posted[key] = r
            self.state.message_runs[r] = run_id
            self.state.questions[r] = question
            self.state.question_agents[r] = agent_id
            self.state.texts[r] = question
            if self._is_manager(agent_id):
                self.state.parked = run_id
            self.state.save()

    def _unstick(self, run_id: int, tool_call_id: str) -> None:
        """Answer an unattended run's `ask_human` so it finishes. Once per ask;
        if Norns would not take the answer, a redelivery or the next cold
        start tries again."""
        key = f"{run_id}:{tool_call_id}"
        with self._lock:
            if key in self.state.unstuck:
                return
            self.state.unstuck.append(key)
            self.state.save()
        logger.warning("run %s asked a question nobody can answer; answering it", run_id)
        try:
            self.client.reply(run_id, UNATTENDED)
        except Exception as e:
            logger.warning(f"could not unstick run {run_id}: {e}")
            with self._lock:
                self.state.unstuck.remove(key)
                self.state.save()

    def _relay(self, run_id: int, output: str | None, *, failed: str | None = None) -> None:
        target = self._target(run_id)
        with self._lock:
            ended_parked = self.state.parked == run_id
            if ended_parked:
                self.state.parked = None
            if run_id in self.state.relayed:
                if ended_parked:
                    self.state.save()
                return
            self.state.relayed.append(run_id)
            self._finish_working_locked(run_id)
            prompt = self.state.prompts.pop(str(run_id), None)
            self.state.origins.pop(str(run_id), None)
            self.state.save()
        if failed is not None:
            self._send(target, f"Run {run_id} failed: {failed}. `nornsctl runs show {run_id}`.")
            return
        text = (output or "").strip()
        if is_silent(text):
            if text.lower() != SILENT:
                # Notes before the marker are dropped with it. Say so here, so a
                # reply that should have been delivered can be found in the log.
                logger.info("run %s ended [silent] after %d characters of notes; not relayed",
                            run_id, len(text) - len(SILENT))
            return
        # A reply that is a single emoji is a reaction to what the owner said.
        emoji = self._transport(target).as_reaction(text)
        if emoji and prompt is not None:
            try:
                self._react(prompt, emoji)
                return
            except Exception as e:
                logger.info(f"could not react to message {prompt}: {e}; sending it instead")
        r = self._send(target, text)
        with self._lock:
            self.state.texts[r] = text
            self.state.save()

    def say(self, run_id: int, text: str) -> bool:
        """A message from a manager run mid-turn (`send_message`), delivered
        the way its final reply will be: to the address the run answers, or
        the primary one when nobody started it. Once per run and text, since
        Norns may dispatch a tool call twice; False when it was already sent.
        A send that fails raises, and is not counted as sent."""
        text = text.strip()
        key = f"{run_id}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"
        with self._lock:
            if key in self.state.said:
                return False
            r = self._send(self._target(run_id), text)
            self.state.said.append(key)
            self.state.texts[r] = text
            self.state.save()
        return True

    def _buttons_for(self, run_id: int, agent_id: int) -> list[list[tuple[str, str]]]:
        rid = run_id
        if agent_id == self._agent_ids.get(self.reminder_agent):
            return [
                [("Done", f"a:{rid}:done"), ("Snooze 1h", f"a:{rid}:snooze 1 hour"),
                 ("Snooze 1d", f"a:{rid}:snooze 1 day")],
                [("Drop it", f"a:{rid}:drop")],
            ]
        return [[("Yes", f"a:{rid}:yes"), ("No", f"a:{rid}:no")]]

    def _ours(self, run_id: int, agent_id: int | None, conversation_id: int | None) -> bool:
        """A manager run in the owner's standing conversation, a reminder run
        Fulla scheduled, or a Mac task it queued. Everything else on the
        tenant is somebody else's."""
        ids = self._our_agent_ids()
        if agent_id == ids.get(self.reminder_agent):
            return self.reminder_known(run_id)
        if agent_id == ids.get(self.mac_task_agent):
            return self.mac_task_known(run_id)
        if agent_id == ids.get(self.manager_agent):
            return conversation_id is not None and conversation_id == self._standing_conversation_id()
        if agent_id == ids.get(self.group_reminder_agent):
            return self.group_reminder_known(run_id) is not None
        if self._is_social(agent_id):
            return str(run_id) in self.state.origins  # we started it, for a friend or a group
        return False

    def _is_manager(self, agent_id: int | None) -> bool:
        return agent_id is not None and agent_id == self._our_agent_ids().get(self.manager_agent)

    def _is_social(self, agent_id: int | None) -> bool:
        """The direct-chat social agent, or any group's own agent. A group
        agent not seen before is looked up once, by its name's prefix."""
        if agent_id is None:
            return False
        ids = self._our_agent_ids()
        if agent_id == ids.get(self.social_agent) or agent_id in self._group_agent_ids.values():
            return True
        if agent_id in ids.values():
            return False
        try:
            agents = self.client.list_agents()
        except Exception:
            logger.warning("could not list agents to recognise %s", agent_id)
            return False
        self._group_agent_ids = {a.name: a.id for a in agents if a.name.startswith(GROUP_AGENT_PREFIX)}
        return agent_id in self._group_agent_ids.values()

    def _unattended(self, agent_id: int | None) -> bool:
        """The reader or a follow-up: known by id, never relayed."""
        ids = self._our_agent_ids()
        return agent_id is not None and any(agent_id == ids.get(n) for n in self.unattended_agents)

    def _on_a_timer(self, agent_id: int | None, trigger_type: str | None) -> bool:
        """A manager run that Norns says a timer started. Its question is
        never posted: parked, it would take the owner's next message, or the
        next timer, as the answer. The worker withholds `ask_human` on these
        turns (`wake.WITHHELD_ON_SCHEDULED`); this is for one that asks anyway."""
        return trigger_type == TIMER_STARTED and self._is_manager(agent_id)

    def _internal_wait(self, agent_id: int | None) -> bool:
        """A Mac task parks on `ask_human` for the Mac, not the owner; the Mac
        worker answers it. Never posted."""
        return agent_id is not None and agent_id == self._our_agent_ids().get(self.mac_task_agent)

    def _standing_conversation_id(self) -> int | None:
        if self._conversation_id is None:
            try:
                self._conversation_id = self.client.conversation_id(self.manager_agent, self.conversation_key)
            except Exception:
                logger.warning("could not resolve the standing conversation")
        return self._conversation_id

    def _our_agent_ids(self) -> dict[str, int]:
        # Includes the unattended agents, to recognise their asks; `_ours`
        # names the agents it relays, so resolving these does not make them ours.
        for name in (self.manager_agent, self.reminder_agent, self.mac_task_agent, self.social_agent,
                     self.group_reminder_agent, *self.unattended_agents):
            if name not in self._agent_ids:
                try:
                    self._agent_ids[name] = self.client.get_agent(name).id
                except Exception:
                    logger.warning(f"agent {name!r} not registered yet")
        return self._agent_ids

    # ---------------------------------------------------------- confirm cards

    def tell_owner(self, text: str) -> None:
        """A line to the owner at the primary address, from Fulla's own side
        (a machine refused for its name), not from a run. Best effort."""
        try:
            r = self._send(self.primary, text)
            with self._lock:
                self.state.texts[r] = text
                self.state.save()
        except Exception:
            logger.exception("could not tell the owner: %s", text[:80])

    def propose(self, kind: str, payload: dict) -> Confirmation:
        """Post a card for the owner to approve. Nothing happens until they
        approve it. The same payload proposed again while undecided is the
        same card. It goes where the owner is talking to the manager now, or
        to the primary address when nobody is (a scheduled turn)."""
        k = self.confirm_kinds[kind]
        pin = digest(kind, payload)
        with self._lock:
            for raw in self.state.confirms.values():
                if raw["digest"] == pin and raw["status"] in confirm.OPEN:
                    return Confirmation(**raw)
            conf = Confirmation(id=secrets.token_hex(5), kind=kind, payload=payload, digest=pin,
                                created_at=datetime.now(UTC).isoformat(timespec="seconds"),
                                address=self._owner_here())
            self._put_confirm(conf)
            self.state.save()
            # A messenger that will not take it sends it on the owner's other
            # one; the card is wherever it landed.
            r = self._send(conf.address, k.render(payload), buttons=self._confirm_buttons(conf), plain=True)
            conf.address, raw_id = split_ref(r)
            conf.message_id = self._message_id(conf.address, raw_id)  # pyright: ignore[reportAttributeAccessIssue]
            self._put_confirm(conf)
            self.state.save()
        return conf

    def claim(self, confirm_id: str, pinned: str) -> dict:
        """The approved payload, handed out once, to the machine that acts on
        it. Raises ClaimRefused for anything not approved by the owner with
        exactly this digest, or already claimed."""
        with self._lock:
            conf = self._get_confirm(confirm_id)
            if conf is None or not self.confirm_kinds.get(conf.kind, _NO_KIND).claimable:
                raise ClaimRefused("no such approval")
            if conf.digest != pinned:
                raise ClaimRefused("that is not the text the owner approved")
            if conf.status != confirm.APPROVED:
                raise ClaimRefused("not approved" if conf.status in confirm.OPEN else NOT_OURS[conf.status].lower())
            conf.status = confirm.CLAIMED
            self._put_confirm(conf)
            self.state.save()
            return {"kind": conf.kind, "payload": conf.payload}

    def _owner_here(self) -> str:
        """Where the owner is talking to the manager now: the address of the
        newest run of theirs shown as working, else the primary address."""
        for working_ref in reversed(list(self.state.working.values())):
            address = split_ref(working_ref)[0]
            if address in self.owner_addresses:
                return address
        return self.primary

    def _may_decide(self, inbound: Inbound, address: str, conf: Confirmation) -> bool:
        """Only the owner decides a card, one to one, where the card is. The
        channel sends nobody else here (friends and groups go to the social
        agent), so this is the second lock, not the first."""
        ok = (not inbound.group and address in self.owner_addresses and address == conf.address
              and self._is_owner(inbound, address))
        if not ok:
            logger.warning("%s from %s at %s on card %s refused: not the owner at the card's address",
                           inbound.kind, inbound.user_id, address, conf.id)
        return ok

    def _confirm_buttons(self, conf: Confirmation) -> list[list[tuple[str, str]]]:
        base = f"k:{conf.id}:{conf.digest[:confirm.DIGEST_SHOWN]}"
        if conf.status == confirm.PENDING:
            row = [(self.confirm_kinds[conf.kind].approve_label, f"{base}:send")]
            if self.confirm_kinds[conf.kind].revise is not None:
                row.append(("Edit", f"{base}:edit"))
            return [row + [("Cancel", f"{base}:cancel")]]
        if conf.status in (confirm.EDITING, confirm.APPROVED):
            return [[("Cancel", f"{base}:cancel")]]
        return []

    def _card(self, conf: Confirmation, note: str = "") -> str:
        text = self.confirm_kinds[conf.kind].render(conf.payload)
        return f"{text}\n\n{note}" if note else text

    def _show(self, conf: Confirmation, note: str = "") -> None:
        """Re-draw a card in place. Caller holds the lock."""
        if conf.message_id is None:
            return
        try:
            self._transport(conf.address).edit(self._chat_id(conf.address), conf.message_id,
                                               self._card(conf, note), buttons=self._confirm_buttons(conf),
                                               plain=True)
        except Exception:
            logger.exception(f"could not update the card for {conf.id}")

    def _toast(self, callback_id: str | None, address: str, text: str | None = None) -> None:
        """Acknowledge a button, when there was one. A reply or a reaction
        standing in for it needs nothing: the card's own edit says it."""
        if callback_id is not None:
            self._transport(address).answer_callback(callback_id, text)

    def _confirm_tap(self, data: str, callback_id: str | None, address: str, inbound: Inbound) -> None:
        """Send, Edit or Cancel on a card. Each moves the confirmation one step,
        once; a tap on a card that has since changed, or a second tap, does
        nothing, and so does anyone but the owner at the card's address."""
        parts = data.split(":")
        if len(parts) != 4:
            self._ack(callback_id, "Unknown button", address)
            return
        _, cid, shown, action = parts
        with self._lock:
            conf = self._get_confirm(cid)
            if conf is None or conf.kind not in self.confirm_kinds:
                self._ack(callback_id, "That one is gone.", address)
                return
            if not self._may_decide(inbound, address, conf):
                self._toast(callback_id, address)
                return
            if conf.digest[:confirm.DIGEST_SHOWN] != shown:
                self._ack(callback_id, "That draft has changed; use the newest card.", address)
                return
            if action == "send":
                if conf.status != confirm.PENDING:
                    self._ack(callback_id, NOT_OURS[conf.status], address)
                    return
                conf.status = confirm.APPROVED
                self._put_confirm(conf)
                self.state.save()
                try:
                    approval = self.confirm_kinds[conf.kind].approve(conf)
                except Exception as e:
                    # Nothing was started, so the owner may try again.
                    logger.exception(f"approving {conf.id} failed")
                    conf.status = confirm.PENDING
                    self._put_confirm(conf)
                    self.state.save()
                    self._show(conf, f"→ couldn't start it: {e}")
                    self._toast(callback_id, address, "That didn't work")
                    return
                conf.run_id = approval.run_id
                if conf.run_id is None:
                    conf.status = confirm.DONE
                self._put_confirm(conf)
                self.state.save()
                self._show(conf, f"→ {approval.note}")
                self._toast(callback_id, address, "Approved")
            elif action == "edit":
                if conf.status != confirm.PENDING or self.confirm_kinds[conf.kind].revise is None:
                    self._ack(callback_id, NOT_OURS.get(conf.status, "Can't edit this one."), address)
                    return
                conf.status = confirm.EDITING
                self._put_confirm(conf)
                self.state.editing = conf.id
                self.state.save()
                self._show(conf, EDIT_PROMPT)
                self._toast(callback_id, address)
            elif action == "cancel":
                if conf.status not in (*confirm.OPEN, confirm.APPROVED):
                    self._ack(callback_id, NOT_OURS[conf.status], address)
                    return
                was = conf.status
                conf.status = confirm.CANCELLED
                self._put_confirm(conf)
                if self.state.editing == conf.id:
                    self.state.editing = None
                self.state.save()
                self._show(conf, "→ Cancelled.")
                self._toast(callback_id, address, "Cancelled")
                if was == confirm.APPROVED and conf.run_id is not None:
                    # The claim is refused now either way; this only spares the run.
                    try:
                        self.client.cancel_run(conf.run_id)
                    except Exception as e:
                        logger.info(f"could not cancel run {conf.run_id}: {e}")
            else:
                self._ack(callback_id, "Unknown button", address)

    def _card_for(self, r: str | None) -> tuple[Confirmation | None, bool]:
        """The confirm a message of ours (by ref) shows, and whether it is
        that confirm's current card (False: a card retired by an edit).
        Caller holds the lock."""
        if r is None:
            return None, False
        for raw in reversed(self.state.confirms.values()):
            conf = Confirmation(**raw)
            if conf.message_id is not None and ref(conf.address, conf.message_id) == r:
                return conf, True
            if r in (ref(conf.address, m) for m in conf.old_message_ids):
                return conf, False
        return None, False

    def _all_buttons(self, conf: Confirmation) -> list[list[tuple[str, str]]]:
        """Every button the card had when it was posted, whatever it shows
        now: a reply naming one it has dropped still reaches `_confirm_tap`,
        which says why it does nothing ("Already approved.")."""
        return self._confirm_buttons(Confirmation(**{**conf.to_dict(), "status": confirm.PENDING}))

    def _card_reply(self, inbound: Inbound, address: str, text: str) -> bool:
        """A reply to a card that names one of its buttons is that button:
        on a messenger without buttons (Signal) it is how the owner taps one,
        and the card shows the labels to reply with. Any cancel word
        ("nvm", "stop") quoted on a card is its Cancel, never a cancel of
        some run, and never new text. It goes through `_confirm_tap` with
        the card's current data, so it is one-shot and pinned to the digest
        the card shows. A reply to a card an edit retired is refused, as a
        tap on it would be. False when the reply is not about a card's
        buttons (any other reply to an undecided card is new text for it;
        see `_edit_target`)."""
        if inbound.reply_to_message_id is None:
            return False
        with self._lock:
            conf, current = self._card_for(ref(address, inbound.reply_to_message_id))
            if conf is None:
                return False
            if not self._may_decide(inbound, address, conf):
                return True
            data = None
            if current:
                said = text.strip().rstrip(".!").casefold()
                buttons = {d.rsplit(":", 1)[1]: d for row in self._all_buttons(conf) for _, d in row}
                data = next((d for row in self._all_buttons(conf) for label, d in row
                             if label.casefold() == said), None)
                if data is None and is_cancel(text):
                    data = buttons.get("cancel")
        if not current:
            self._ack(None, "That draft has changed; use the newest card.", address)
            return True
        if data is None:
            return False
        self._confirm_tap(data, None, address, inbound)
        return True

    def _card_reaction(self, inbound: Inbound, address: str, emoji: str) -> bool:
        """A reaction on a card: a cancel reaction is Cancel; 👍 sends
        nothing and says how to.
        Through `_confirm_tap`, like a reply. False when the message is not
        a card; any other emoji on one, or anyone else's, does nothing."""
        r = ref(address, inbound.message_id) if inbound.message_id is not None else None
        with self._lock:
            conf, current = self._card_for(r)
            if conf is None:
                return False
            if not self._may_decide(inbound, address, conf):
                return True
            buttons = self._all_buttons(conf) if current else []
        actions = {d.rsplit(":", 1)[1]: d for row in buttons for _, d in row}
        if not current:
            self._ack(None, "That draft has changed; use the newest card.", address)
        elif emoji in CARD_APPROVE_REACTIONS and "send" in actions:
            self._ack(None, CARD_SEND_BY_REPLY, address)
        elif emoji in CANCEL_REACTIONS | {"👎"} and "cancel" in actions:
            self._confirm_tap(actions["cancel"], None, address, inbound)
        return True

    def _edit_target(self, inbound: Inbound, address: str, text: str) -> str | None:
        """The confirm the owner's message is new text for: a reply to an
        undecided card, or any unquoted message at the card's address while
        one is waiting for an edit. A cancel word is never new text: quoted on the
        card it was its Cancel (`_card_reply`); otherwise it stops the run
        in flight, or drops the edit when there is none (`_drop_edit`)."""
        with self._lock:
            if inbound.reply_to_message_id is not None:
                conf, current = self._card_for(ref(address, inbound.reply_to_message_id))
                if conf is not None:
                    if (current and conf.status in confirm.OPEN
                            and self.confirm_kinds.get(conf.kind, _NO_KIND).revise is not None
                            and self._may_decide(inbound, address, conf)):
                        return conf.id
                    return None
                # A quoted reply is about what it quotes (a question, the
                # Working… message), not the edit.
                return None
            cid = self.state.editing
            if cid is None or text.startswith("/") or is_cancel(text):
                return None
            conf = self._get_confirm(cid)
            if conf is None or conf.status != confirm.EDITING:
                self.state.editing = None
                self.state.save()
                return None
            if address != conf.address or not self._may_decide(inbound, address, conf):
                return None
            return cid

    def _drop_edit(self, address: str) -> bool:
        """A cancel word while an edit waits and nothing is running: back to
        the card as it was, buttons and all. The owner's message only."""
        with self._lock:
            conf = self._get_confirm(self.state.editing) if self.state.editing else None
            if conf is None or conf.status != confirm.EDITING or conf.address != address:
                return False
            conf.status = confirm.PENDING
            self._put_confirm(conf)
            self.state.editing = None
            self.state.save()
            self._show(conf, EDIT_DROPPED)
        return True

    def _confirm_revise(self, cid: str, text: str) -> None:
        """The owner's edit: a new payload, a new digest, a new card. The old
        card loses its buttons, and a tap on it would not match anyway."""
        with self._lock:
            conf: Confirmation = self._get_confirm(cid)  # pyright: ignore[reportAssignmentType]
            k = self.confirm_kinds[conf.kind]
            old = Confirmation(**conf.to_dict())
            conf.payload = k.revise(conf.payload, text)  # pyright: ignore[reportOptionalCall]
            conf.digest = digest(conf.kind, conf.payload)
            conf.status = confirm.PENDING
            if self.state.editing == cid:
                self.state.editing = None
            conf.message_id = None
            self._put_confirm(conf)
            self.state.save()
            if old.message_id is not None:
                conf.old_message_ids = [*conf.old_message_ids, old.message_id][-5:]
                try:
                    self._transport(old.address).edit(self._chat_id(old.address), old.message_id,
                                                      self._card(old, EDITED), plain=True)
                except Exception:
                    logger.exception(f"could not retire the old card for {cid}")
            r = self._send(conf.address, k.render(conf.payload), buttons=self._confirm_buttons(conf), plain=True)
            address, raw_id = split_ref(r)
            if address != conf.address:
                # It landed on the owner's other messenger; retired cards
                # stay named where they were.
                conf.old_message_ids = []
                conf.address = address
            conf.message_id = self._message_id(address, raw_id)  # pyright: ignore[reportAttributeAccessIssue]
            self._put_confirm(conf)
            self.state.save()

    def _confirm_outcome(self, run_id: int, output: str | None, *, failed: str | None = None) -> bool:
        """Write a run's outcome onto the card whose approval started it. False
        if no card did; once per card."""
        with self._lock:
            conf = next((Confirmation(**raw) for raw in self.state.confirms.values()
                         if raw.get("run_id") == run_id), None)
            if conf is None:
                return False
            if conf.status in (confirm.DONE, confirm.CANCELLED):
                return True
            conf.status = confirm.DONE
            self._put_confirm(conf)
            self.state.save()
            note = f"failed: {failed}" if failed is not None else (output or "").strip() or "Finished."
            self._show(conf, f"→ {note}")
        return True

    def _confirms_in_flight(self) -> list[Confirmation]:
        with self._lock:
            return [Confirmation(**raw) for raw in self.state.confirms.values()
                    if raw["status"] in (confirm.APPROVED, confirm.CLAIMED) and raw.get("run_id") is not None]

    def _get_confirm(self, cid: str) -> Confirmation | None:
        raw = self.state.confirms.get(cid)
        return Confirmation(**raw) if raw else None

    def _put_confirm(self, conf: Confirmation) -> None:
        self.state.confirms[conf.id] = conf.to_dict()


_NO_KIND = ConfirmKind(name="", render=str, approve=lambda c: None)  # pyright: ignore[reportArgumentType]
