"""Fulla's Signal account, through signal-cli-rest-api.

signal-cli runs as its own always-on machine (deploy/signal/), holding the
account's keys and a small REST API. Outbound is a call to it; inbound is
its websocket, which a forwarder beside it turns into signed POSTs to
/signal (production, so Fulla can sleep between messages) or which `poll`
reads directly (local dev). A message is named by its timestamp.

Signal has no buttons. Choices go out as a line of labels to reply with,
and a quoted reply or a reaction is how the owner picks one; the channel
already reads both. Deliberately framework-free, and this module knows
nothing about Norns; channel.py is where the two meet.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import unicodedata
from collections.abc import Callable

import httpx

from fulla.transport import Buttons, Inbound, Mentions

logger = logging.getLogger("fulla.signal")

_FENCE = re.compile(r"```([\w+-]*)[ \t]*\n(.*?)```", re.DOTALL)
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+")
_CODE = re.compile(r"`[^`\n]+`")
_BOLD_US = re.compile(r"__(?=\S)(.+?)(?<=\S)__")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_ITALIC_US = re.compile(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")

# What an attachment is called when the manager is told about one.
_ATTACHMENTS = (
    ("image/", "a photo"),
    ("video/", "a video"),
    ("audio/", "a voice message"),
)
CANCEL_WORDS = frozenset({"cancel", "stop"})


def _spans(text: str) -> str:
    text = _LINK.sub(lambda m: m.group(1) if m.group(1) == m.group(2) else f"{m.group(1)} ({m.group(2)})", text)
    text = _BOLD_US.sub(r"**\1**", text)
    text = _STRIKE.sub(r"~\1~", text)
    text = _ITALIC_US.sub(r"*\1*", text)
    return text


def _inline(line: str) -> str:
    """Rewrite one line's Markdown into Signal's, leaving inline code alone."""
    out: list[str] = []
    pos = 0
    for m in _CODE.finditer(line):
        out.append(_spans(line[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_spans(line[pos:]))
    return "".join(out)


def _block(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        h = _HEADING.match(line)
        if h:
            lines.append(f"**{_inline(h.group(1))}**")
            continue
        b = _BULLET.match(line)
        if b:
            line = f"{b.group(1)}• {line[b.end():]}"
        lines.append(_inline(line))
    return "\n".join(lines)


def render(text: str) -> str:
    """Turn the light Markdown a model writes into signal-cli-rest-api's
    "styled" text: `**bold**`, `*italic*`, `~strike~`, backticks for
    monospace. Bold, italics and inline code are already spelled that way;
    double tildes, underscores, headings (shown bold), bullets (shown as
    "•"), links (text, then the address) and fences (monospace) are not."""
    out: list[str] = []
    pos = 0
    for m in _FENCE.finditer(text):
        out.append(_block(text[pos:m.start()]))
        out.append(f"`{m.group(2).rstrip(chr(10))}`")
        pos = m.end()
    out.append(_block(text[pos:]))
    return "".join(out)


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def mention_ranges(message: str, mentions: Mentions) -> list[dict]:
    """Signal's mentions for a rendered message: for each (id, name) whose
    "@<name>" the message carries, the range to tag, in UTF-16 units as
    Signal counts them, and the account (number or id) it points at. Clients
    show the range as the person's own name; one without mention support
    still reads "@<name>"."""
    ranges = []
    for user_id, name in mentions:
        needle = f"@{name}"
        at = message.find(needle)
        if at < 0:
            continue
        ranges.append({"author": str(user_id), "start": _utf16_len(message[:at]), "length": _utf16_len(needle)})
    return ranges


def choices(buttons: Buttons) -> str:
    """The line that stands in for buttons: what to reply with."""
    labels = [label for row in buttons for label, _ in row]
    if len(labels) == 1:
        return f"Reply to this with “{labels[0]}”."
    return "Reply to this with one of: " + " · ".join(labels)


def is_emoji(text: str) -> bool:
    """One emoji, allowing skin tones, joiners and the variation selector."""
    text = text.strip()
    if not text or len(text) > 12:
        return False
    base = [c for c in text if c not in "‍️" and not 0x1F3FB <= ord(c) <= 0x1F3FF]
    if not base:
        return False
    if all(0x1F1E6 <= ord(c) <= 0x1F1FF for c in base):
        return len(base) == 2  # a flag
    if len(base) > 4:
        return False
    return all(unicodedata.category(c) in ("So", "Sk") or 0x1F000 <= ord(c) <= 0x1FAFF for c in base)


def _attachment_text(dm: dict) -> str | None:
    """The text for a message that carries something other than text: a note
    of what it was, then its caption. None when there is nothing at all."""
    attachments = dm.get("attachments") or []
    if dm.get("sticker"):
        what = "a sticker"
    elif attachments:
        a = attachments[0]
        kind = a.get("contentType") or ""
        what = next((name for prefix, name in _ATTACHMENTS if kind.startswith(prefix)), "a file")
        if what == "a file" and a.get("filename"):
            what += f" ({a['filename']})"
        if a.get("voiceNote"):
            what = "a voice message"
    else:
        return None
    note = f"[The owner sent {what}, which you can't see.]"
    caption = (dm.get("message") or "").strip()
    return f"{note}\n{caption}" if caption else note


GROUP = "group:"  # a group's chat id is "group:<signal-cli's group id>"


def parse_envelope(payload: dict) -> Inbound | None:
    """Turn one received envelope into an Inbound, or None if it's not
    something we handle: typing, receipts, sync messages, a reaction taken
    back. A group message's chat is the group; the channel decides whether
    Fulla may speak there."""
    env = payload.get("envelope") or {}
    dm = env.get("dataMessage")
    if not dm:
        return None
    sender = env.get("sourceNumber") or env.get("sourceUuid") or env.get("source") or ""
    group_info = dm.get("groupInfo") or {}
    group = bool(group_info.get("groupId"))
    chat = f"{GROUP}{group_info['groupId']}" if group else sender
    common = {"transport": "signal", "user_id": sender, "chat_id": chat, "group": group,
              "chat_title": group_info.get("groupName") or "", "sender_name": env.get("sourceName") or ""}
    reaction = dm.get("reaction")
    if reaction:
        if reaction.get("isRemove") or not reaction.get("emoji"):
            return None
        return Inbound(kind="reaction", text=reaction["emoji"], message_id=reaction.get("targetSentTimestamp"),
                       date=env.get("timestamp"), **common)
    text = _attachment_text(dm) or dm.get("message")
    if not text:
        return None
    quote = dm.get("quote") or {}
    mentions = [m for m in (dm.get("mentions") or []) if isinstance(m, dict)]
    return Inbound(kind="text", text=_with_mentions(text, mentions),
                   message_id=dm.get("timestamp") or env.get("timestamp"),
                   reply_to_message_id=quote.get("id"), date=env.get("timestamp"),
                   mentions=[str(v) for m in mentions for v in (m.get("number"), m.get("uuid")) if v], **common)


MENTION_PLACEHOLDER = "￼"


def _with_mentions(text: str, mentions: list[dict]) -> str:
    """Signal puts an object-replacement character where a mention was and
    lists who was mentioned beside it. Put "@name" back, in order, so the
    text reads as typed."""
    if MENTION_PLACEHOLDER not in text:
        return text
    names = iter(m.get("name") or m.get("number") or m.get("uuid") or "" for m in mentions)
    out = []
    for ch in text:
        out.append(f"@{next(names, '')}" if ch == MENTION_PLACEHOLDER else ch)
    return "".join(out)


def group_recipient(chat_id: str) -> str:
    """signal-cli-rest-api names a group `group.<base64 of its internal id>`
    when sending, and gives the internal id itself in a received envelope."""
    internal = chat_id[len(GROUP):]
    return "group." + base64.b64encode(internal.encode()).decode()


def recipient(chat_id: str) -> str:
    return group_recipient(chat_id) if chat_id.startswith(GROUP) else chat_id


def _timestamp(result) -> int:
    """The sent message's timestamp. The API answers `[{"timestamp": "…"}]`,
    one entry per recipient (older releases answered a bare object)."""
    if isinstance(result, list):
        result = result[0] if result else {}
    return int(result["timestamp"])


class Signal:
    name = "signal"
    buttons = False

    def __init__(self, api_url: str, number: str, owner: str):
        """`owner` names who may talk to Fulla: their number, their Signal
        account id, or both, comma-separated. Everyone else is dropped."""
        self._api = api_url.rstrip("/")
        self.number = number
        self.owners = frozenset(o.strip() for o in owner.split(",") if o.strip())
        self._http = httpx.Client(timeout=40)

    def allowed(self, inbound: Inbound) -> bool:
        """Is this the owner? Who else may talk is the channel's decision."""
        return str(inbound.user_id) in self.owners

    def group_name(self, chat_id: str) -> str:
        """A group's name from the account's group list, "" if unknown."""
        try:
            groups = self._call("GET", f"/v1/groups/{self.number}")
        except Exception as e:
            logger.info(f"listing groups failed: {e}")
            return ""
        internal = chat_id[len(GROUP):]
        for g in groups if isinstance(groups, list) else []:
            if g.get("internal_id") == internal or g.get("id") == group_recipient(chat_id):
                return g.get("name") or ""
        return ""

    def _call(self, method: str, path: str, **body) -> dict:
        resp = self._http.request(method, f"{self._api}{path}", json=body or None)
        if resp.status_code >= 300:
            raise RuntimeError(f"signal {method} {path}: {resp.status_code} {resp.text[:200]}")
        return resp.json() if resp.content else {}

    @staticmethod
    def _message(text: str, buttons: Buttons | None, plain: bool) -> dict:
        """The text as Signal gets it: styled unless `plain` (a draft shown
        verbatim, where an asterisk is an asterisk), with the choices a button
        row would have offered spelled out."""
        message = text if plain else render(text)
        if buttons:
            message = f"{message}\n\n{choices(buttons)}"
        return {"message": message, "text_mode": "normal" if plain else "styled"}

    def send(self, chat_id: str, text: str, buttons: Buttons | None = None, mentions: Mentions | None = None, *,
             plain: bool = False) -> int:
        """Send text written in light Markdown, with the choices a button row
        would have offered spelled out, and `mentions` tagged where the text
        says "@<name>". Returns the message's timestamp."""
        body: dict = {"number": self.number, "recipients": [recipient(chat_id)],
                      **self._message(text, buttons, plain)}
        ranges = mention_ranges(body["message"], mentions or [])
        if ranges:
            body["mentions"] = ranges
        result = self._call("POST", "/v2/send", **body)
        return _timestamp(result)

    def edit(self, chat_id: str, message_id: int, text: str, buttons: Buttons | None = None, *,
             plain: bool = False) -> None:
        """Replace a message's text, and its choices line with `buttons`'. The
        original timestamp keeps naming it, so a quoted reply to the edited
        message still finds it."""
        self._call("POST", "/v2/send", number=self.number, recipients=[recipient(chat_id)],
                   edit_timestamp=int(message_id), **self._message(text, buttons, plain))

    def delete(self, chat_id: str, message_id: int) -> bool:
        """Delete a message for both of us. False if Signal would not (too
        old); the caller edits it instead."""
        try:
            self._call("DELETE", f"/v1/remote-delete/{self.number}", recipient=recipient(chat_id),
                       timestamp=int(message_id))
        except RuntimeError as e:
            logger.info(f"remote-delete {message_id}: {e}")
            return False
        return True

    def send_chat_action(self, chat_id: str, action: str = "typing") -> None:
        """Show "typing…". Signal clears it after about fifteen seconds, so a
        long wait re-sends it."""
        self._call("PUT", f"/v1/typing-indicator/{self.number}", recipient=recipient(chat_id))

    def answer_callback(self, callback_id: str | None, text: str | None = None) -> None:
        """Signal has no buttons, so nothing taps one; never called."""

    def set_reaction(self, chat_id: str, message_id: int, emoji: str) -> None:
        self._call("POST", f"/v1/reactions/{self.number}", recipient=chat_id, reaction=emoji,
                   target_author=chat_id, timestamp=int(message_id))

    def as_reaction(self, text: str) -> str | None:
        """Signal lets Fulla react with any one emoji."""
        return text.strip() if is_emoji(text) else None

    def poll(self, handler: Callable[[Inbound], None], stop: threading.Event) -> None:
        """Read the account's receive stream until `stop` is set, handing
        each of the owner's messages to `handler`. Local dev; in production
        the forwarder beside signal-cli does this and POSTs to /signal."""
        from websockets.sync.client import connect

        url = f"{self._api.replace('http', 'ws', 1)}/v1/receive/{self.number}"
        while not stop.is_set():
            try:
                with connect(url) as ws:
                    logger.info("receiving from %s", url)
                    while not stop.is_set():
                        try:
                            raw = ws.recv(timeout=1)
                        except TimeoutError:
                            continue
                        inbound = parse_envelope(json.loads(raw))
                        if inbound is None:
                            continue
                        try:
                            handler(inbound)
                        except Exception:
                            logger.exception("handler failed")
            except Exception as e:  # the daemon restarting, the network
                logger.warning(f"receive stream failed: {e}")
                stop.wait(5)
