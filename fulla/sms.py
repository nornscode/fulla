"""Fulla over text messages, through Twilio.

Twilio holds the number. It can be the one Signal is registered on: Signal
used it once, for its code, and a text to it lands at Twilio, not Signal.
Outbound is a call to the Messages API; inbound is Twilio's webhook, a
signed form POST to /sms (production; `set_webhook` points the number at
it on start), or `poll`, which lists the number's inbound messages for
local dev, where there is no public URL. A message is named by its SID.

SMS is text and nothing else: no buttons, no edits, no deletes, no typing,
no reactions, no quoting. Choices go out as a line of labels to reply
with, and a reply that is one of them is the tap (the channel's rule for a
messenger without buttons). Markdown is flattened to plain text, and the
few characters a model likes that would push a message out of the GSM
alphabet, which halves what one segment holds, are swapped for their plain
forms. A long answer goes as several texts, split at paragraphs, each
within Twilio's limit. Deliberately framework-free, and this module knows
nothing about Norns; channel.py is where the two meet.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import threading
from collections.abc import Callable

import httpx

from fulla.transport import Buttons, Inbound, Mentions

logger = logging.getLogger("fulla.sms")

API = "https://api.twilio.com/2010-04-01"
MAX_BODY = 1600  # Twilio's ceiling for one message; carriers split it into segments

_FENCE = re.compile(r"```([\w+-]*)[ \t]*\n(.*?)```", re.DOTALL)
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+")
_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])|(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")

# Characters outside GSM-7 that prose picks up, and what to send instead.
# One of them in a message makes the whole message UCS-2: 70 characters a
# segment instead of 160. Emoji are left alone; the owner chose them.
_PLAIN = str.maketrans({
    "…": "...", "—": "-", "–": "-", "•": "-", "·": "-", "→": "->", "←": "<-",
    "“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", " ": " ", " ": " ", " ": " ",
    "✓": "ok", "✔": "ok", "×": "x",
})

# What an attachment is called when the manager is told about one.
_ATTACHMENTS = (
    ("image/", "a photo"),
    ("video/", "a video"),
    ("audio/", "a voice message"),
    ("text/vcard", "a contact"),
)


def _spans(text: str) -> str:
    text = _LINK.sub(lambda m: m.group(1) if m.group(1) == m.group(2) else f"{m.group(1)} ({m.group(2)})", text)
    text = _BOLD.sub(lambda m: m.group(1) or m.group(2), text)
    text = _STRIKE.sub(r"\1", text)
    text = _ITALIC.sub(lambda m: m.group(1) or m.group(2), text)
    return text


def _inline(line: str) -> str:
    """Flatten one line's Markdown, leaving what was inline code as typed."""
    out: list[str] = []
    pos = 0
    for m in _CODE.finditer(line):
        out.append(_spans(line[pos:m.start()]))
        out.append(m.group(1))
        pos = m.end()
    out.append(_spans(line[pos:]))
    return "".join(out)


def _block(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        h = _HEADING.match(line)
        if h:
            lines.append(_inline(h.group(1)))
            continue
        b = _BULLET.match(line)
        if b:
            line = f"{b.group(1)}- {line[b.end():]}"
        lines.append(_inline(line))
    return "\n".join(lines)


def render(text: str) -> str:
    """Turn the light Markdown a model writes into plain text: markers
    dropped, headings as their text, bullets as "-", links as "text (url)",
    fences as the code. Then `plain`."""
    out: list[str] = []
    pos = 0
    for m in _FENCE.finditer(text):
        out.append(_block(text[pos:m.start()]))
        out.append(m.group(2).rstrip("\n"))
        pos = m.end()
    out.append(_block(text[pos:]))
    return plain("".join(out))


def plain(text: str) -> str:
    """Swap the punctuation that would make a message UCS-2 for its GSM form."""
    return text.translate(_PLAIN)


def chunks(text: str, limit: int = MAX_BODY) -> list[str]:
    """`text` as messages of at most `limit` characters, split at paragraph
    breaks, then lines, then wherever it must. An empty text is one empty
    message, so a caller always has something to send."""
    text = text.strip()
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            out.append(current)
            current = ""
        if len(para) <= limit:
            current = para
            continue
        for line in para.split("\n"):
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                out.append(current)
                current = ""
            while len(line) > limit:
                cut = line.rfind(" ", 0, limit)
                cut = cut if cut > limit // 2 else limit
                out.append(line[:cut].rstrip())
                line = line[cut:].lstrip()
            current = line
    if current:
        out.append(current)
    return out


def choices(buttons: Buttons) -> str:
    """The line that stands in for buttons: what to reply with."""
    labels = [label for row in buttons for label, _ in row]
    if len(labels) == 1:
        return f'Reply "{labels[0]}".'
    return "Reply with one of: " + ", ".join(labels)


def verify_signature(auth_token: str, url: str, params: list[tuple[str, str]], header: str) -> bool:
    """Twilio's X-Twilio-Signature: base64 of an HMAC-SHA1, keyed by the
    account's auth token, over the URL it POSTed to followed by every form
    field, sorted by name, as name then value. `url` is the address Twilio
    was given, scheme and host included; a mismatch there is a bad
    signature, which is the point."""
    if not auth_token or not header:
        return False
    data = url + "".join(k + v for k, v in sorted(params))
    expected = base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(expected, header.strip())


def _attachment_text(params: dict) -> str | None:
    """The text for a message that carries media: a note of what it was,
    then its caption. None when there is nothing at all."""
    try:
        count = int(params.get("NumMedia") or 0)
    except ValueError:
        count = 0
    body = (params.get("Body") or "").strip()
    if count <= 0:
        return None
    kind = params.get("MediaContentType0") or ""
    what = next((name for prefix, name in _ATTACHMENTS if kind.startswith(prefix)), "a file")
    if count > 1:
        what = f"{count} files"
    note = f"[The owner sent {what}, which you can't see.]"
    return f"{note}\n{body}" if body else note


def parse_webhook(params: dict) -> Inbound | None:
    """Turn the form Twilio POSTs for an incoming message into an Inbound,
    or None if it is not one: a status callback, a request with no sender
    or no content. SMS has no groups, no quoting and no reactions, so an
    Inbound is always a text from one number to Fulla's."""
    sender = (params.get("From") or "").strip()
    sid = (params.get("MessageSid") or params.get("SmsSid") or "").strip()
    if not sender or not sid:
        return None
    text = _attachment_text(params) or (params.get("Body") or "").strip()
    if not text:
        return None
    return Inbound(kind="text", transport="sms", user_id=sender, chat_id=sender, text=text,
                   sender_name=params.get("ProfileName") or "", message_id=sid)


class Sms:
    name = "sms"
    buttons = False

    def __init__(self, account_sid: str, auth_token: str, number: str, owner: str):
        """`owner` is the only number whose texts reach Fulla. Everyone else
        is dropped by the channel."""
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.number = number
        self.owner = owner
        self._http = httpx.Client(timeout=30, auth=(account_sid, auth_token))
        self._number_sid: str | None = None

    def allowed(self, inbound: Inbound) -> bool:
        return str(inbound.user_id) == self.owner

    def _call(self, method: str, path: str, **fields) -> dict:
        resp = self._http.request(method, f"{API}/Accounts/{self.account_sid}{path}",
                                  data=fields or None)
        if resp.status_code >= 300:
            detail = ""
            try:
                detail = resp.json().get("message") or ""
            except Exception:
                detail = resp.text[:200]
            raise RuntimeError(f"twilio {method} {path}: {resp.status_code} {detail}")
        return resp.json() if resp.content else {}

    def _bodies(self, text: str, buttons: Buttons | None, plain: bool) -> list[str]:
        message = text if plain else render(text)
        if buttons:
            message = f"{message}\n\n{choices(buttons)}"
        return chunks(message)

    def send(self, chat_id: str, text: str, buttons: Buttons | None = None, mentions: Mentions | None = None, *,
             plain: bool = False) -> str:
        """Send text written in light Markdown as plain text, with the
        choices a button row would have offered spelled out, in as many
        messages as it takes. `plain` sends it as given. Mentions tag nobody:
        there are no groups here. Returns the SID of the last message sent,
        the one carrying the choices line."""
        sid = ""
        for body in self._bodies(text, buttons, plain):
            result = self._call("POST", "/Messages.json", To=chat_id, From=self.number, Body=body)
            sid = result.get("sid") or sid
        return sid

    def edit(self, chat_id: str, message_id, text: str, buttons: Buttons | None = None, *,
             plain: bool = False) -> None:
        """A text cannot be changed once sent. The owner has it as it was."""
        logger.debug("edit of %s dropped: SMS has no edits", message_id)

    def delete(self, chat_id: str, message_id) -> bool:
        """A text cannot be taken back. True: there is nothing left to edit
        in its place either."""
        return True

    def send_chat_action(self, chat_id: str, action: str = "typing") -> None:
        """SMS has no typing indicator."""

    def answer_callback(self, callback_id: str | None, text: str | None = None) -> None:
        """SMS has no buttons, so nothing taps one; never called."""

    def set_reaction(self, chat_id: str, message_id, emoji: str) -> None:
        """SMS has no reactions. Never called: `as_reaction` says so."""

    def as_reaction(self, text: str) -> str | None:
        """Nothing is a reaction here; an emoji goes as a text."""
        return None

    def _find_number_sid(self) -> str:
        if self._number_sid:
            return self._number_sid
        result = self._http.get(f"{API}/Accounts/{self.account_sid}/IncomingPhoneNumbers.json",
                                params={"PhoneNumber": self.number})
        if result.status_code >= 300:
            raise RuntimeError(f"twilio list numbers: {result.status_code} {result.text[:200]}")
        numbers = result.json().get("incoming_phone_numbers") or []
        if not numbers:
            raise RuntimeError(f"twilio: {self.number} is not a number on account {self.account_sid}")
        self._number_sid = numbers[0]["sid"]
        return self._number_sid  # pyright: ignore[reportReturnType]

    def set_webhook(self, url: str) -> None:
        """Have Twilio POST each incoming message to `url`, and POST it there
        again if the first attempt fails: Twilio does not retry on its own,
        only calls the fallback, so the fallback is the same address."""
        self._call("POST", f"/IncomingPhoneNumbers/{self._find_number_sid()}.json",
                   SmsUrl=url, SmsMethod="POST", SmsFallbackUrl=url, SmsFallbackMethod="POST")

    def poll(self, handler: Callable[[Inbound], None], stop: threading.Event, interval: float = 3.0) -> None:
        """Local dev: list the number's inbound messages every few seconds
        and hand the new ones to `handler`. What was there at start is
        taken as read. In production Twilio POSTs to /sms instead."""
        seen: set[str] = set()
        first = True
        while not stop.is_set():
            try:
                result = self._http.get(f"{API}/Accounts/{self.account_sid}/Messages.json",
                                        params={"To": self.number, "PageSize": 20})
                result.raise_for_status()
                messages = result.json().get("messages") or []
            except Exception as e:  # network blips are normal here
                logger.warning(f"listing messages failed: {e}")
                stop.wait(5)
                continue
            for m in reversed(messages):  # oldest first
                sid = m.get("sid")
                if not sid or sid in seen or not (m.get("direction") or "").startswith("inbound"):
                    continue
                seen.add(sid)
                if first:
                    continue
                inbound = parse_webhook({"From": m.get("from"), "MessageSid": sid, "Body": m.get("body"),
                                         "NumMedia": m.get("num_media")})
                if inbound is None:
                    continue
                try:
                    handler(inbound)
                except Exception:
                    logger.exception("handler failed")
            first = False
            stop.wait(interval)
