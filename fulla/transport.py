"""What the channel needs from a messenger, and nothing more.

Telegram (`telegram.py`), Signal (`signal.py`) and SMS (`sms.py`) each give
the channel the same small surface: send a message and get back something
that names it, change or take down a message by that name, show that Fulla
is typing, react. A messenger that cannot do one of these does nothing for
it (a text cannot be edited); the channel's bookkeeping is unchanged.
Inbound is one shape too, and says which transport it came in on; the
channel keeps its bookkeeping by address and message id together, so ids
may be ints (Telegram), timestamps (Signal) or SIDs (SMS) alike, on several
messengers at once.

A transport knows nothing about Norns. channel.py is where the two meet.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

# Rows of buttons as (label, data). Telegram shows them as inline buttons
# that come back as a "callback"; Signal has no buttons and lists the labels
# for the owner to reply with. `data` is the channel's: "c:<run>" cancels a
# run, "a:<run>:<answer>" answers its question, "k:<confirm>:<digest>:<action>"
# acts on a confirm card.
Buttons = list[list[tuple[str, str]]]

# Who a message tags, as (user id, name): the text says "@<name>" where the
# tag goes, and the transport turns that into the messenger's own mention
# (Signal's mention range, Telegram's user link). A name the text does not
# carry is not tagged. The channel writes the "@<name>"; the model never does.
Mentions = list[tuple[str, str]]


@dataclass
class Inbound:
    kind: str  # "text" | "callback" | "reaction" (text is the emoji)
    user_id: int | str
    chat_id: int | str
    text: str = ""
    transport: str = ""  # the Transport's name this came in on; with one transport it may be left blank
    group: bool = False  # a group chat, where chat_id is the group's, not the sender's
    chat_title: str = ""  # the group's name when the messenger says it
    sender_name: str = ""  # what the messenger calls the sender, for group talk; the store's name wins
    mentions: list[str] = field(default_factory=list)  # who the message mentions: numbers or account ids (Signal), "@handles" (Telegram)
    message_id: int | str | None = None
    reply_to_message_id: int | None = None
    callback_id: str | None = None
    data: str | None = None
    date: int | None = None


class Transport(Protocol):
    name: str  # first half of an address, and of the standing conversation's key: "telegram", "signal", "sms"
    buttons: bool  # has inline buttons; without them the channel shows work as typing only and takes words for taps

    def send(self, chat_id, text: str, buttons: Buttons | None = None, mentions: Mentions | None = None, *,
             plain: bool = False) -> int:
        """Send text written in light Markdown, tagging `mentions` where the
        text says "@<name>", or exactly as given when `plain` (a draft the
        owner must see verbatim). Returns the message's id."""
        ...

    def edit(self, chat_id, message_id: int, text: str, buttons: Buttons | None = None, *,
             plain: bool = False) -> None:
        """Replace a message's text; its buttons become `buttons`, none by default."""

    def delete(self, chat_id, message_id: int) -> bool:
        """Take a message down. False if the messenger would not; the caller
        edits it instead."""
        ...

    def send_chat_action(self, chat_id, action: str = "typing") -> None:
        """Show "typing…" for a few seconds."""

    def answer_callback(self, callback_id: str | None, text: str | None = None) -> None:
        """Acknowledge a button tap, with a short note if there is one."""

    def set_reaction(self, chat_id, message_id: int, emoji: str) -> None:
        """React to a message."""

    def as_reaction(self, text: str) -> str | None:
        """The reaction a reply is, if it is nothing but one emoji this
        messenger lets Fulla react with."""

    def poll(self, handler: Callable[[Inbound], None], stop) -> None:
        """Local dev: receive until `stop` is set, handing the owner's
        messages to `handler`."""
