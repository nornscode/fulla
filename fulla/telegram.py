"""A small Telegram Bot API client.

Inbound arrives one of two ways: `poll` long-polls getUpdates (local dev, no
public URL needed) or Telegram POSTs each update to a webhook (`set_webhook`;
server.py receives it). Deliberately framework-free. This module knows
nothing about Norns; channel.py is where the two meet.
"""

from __future__ import annotations

import html
import logging
import re
import threading
from collections.abc import Callable
from typing import Any

import httpx

from fulla.transport import Buttons, Inbound, Mentions

logger = logging.getLogger("fulla.telegram")

_FENCE = re.compile(r"```([\w+-]*)[ \t]*\n(.*?)```", re.DOTALL)
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+")
_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])|(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")

# Telegram sends only the update types a webhook or getUpdates names, and a
# webhook keeps the list it was set with until it is set again.
ALLOWED_UPDATES = ["message", "callback_query", "message_reaction"]

# The emoji a bot may react with. Telegram's list is fixed and refuses anything
# else; spelled as Telegram spells them, without the U+FE0F variation selector.
_REACTIONS = (
    "👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 ❤‍🔥 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 "
    "🤨 😐 🍓 🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈 😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 🗿 🆒 💘 🙉 🦄 😘 "
    "💊 🙊 😎 👾 🤷‍♂ 🤷 🤷‍♀ 😡"
)
REACTIONS = frozenset(_REACTIONS.split())


def as_reaction(text: str) -> str | None:
    """The reaction a text is, if it is nothing but one emoji a bot may send."""
    emoji = text.strip().replace("️", "")
    return emoji if emoji in REACTIONS else None


def _inline(line: str) -> str:
    """Escape one line for Telegram HTML and turn its inline Markdown into tags."""
    out: list[str] = []
    pos = 0
    for m in _CODE.finditer(line):
        out.append(_spans(line[pos:m.start()]))
        out.append(f"<code>{html.escape(m.group(1), quote=False)}</code>")
        pos = m.end()
    out.append(_spans(line[pos:]))
    return "".join(out)


def _spans(text: str) -> str:
    text = html.escape(text, quote=False)
    text = _LINK.sub(r'<a href="\2">\1</a>', text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    text = _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)
    return text


def tag(rendered: str, mentions: Mentions) -> str:
    """Turn the first "@<name>" in rendered HTML into a link to that user,
    which Telegram shows as a mention and notifies them of. A name the text
    does not carry is left out; the message still reads."""
    for user_id, name in mentions:
        needle = f"@{html.escape(str(name), quote=False)}"
        if needle in rendered:
            rendered = rendered.replace(needle, f'<a href="tg://user?id={user_id}">{needle}</a>', 1)
    return rendered


def render(text: str) -> str:
    """Turn the light Markdown a model writes into Telegram HTML.

    Bold, italics, strikethrough, inline code, fenced code, links, headings
    (shown bold) and bullets (shown as "•"). Everything else is escaped, so
    stray angle brackets in error text survive. Telegram has no block
    structure, so lists and headings keep their line breaks and lose nothing
    when it cannot be parsed: `send` falls back to the plain text."""
    out: list[str] = []
    pos = 0
    for m in _FENCE.finditer(text):
        out.append(_block(text[pos:m.start()]))
        lang, code = m.group(1), html.escape(m.group(2).rstrip("\n"), quote=False)
        inner = f'<code class="language-{lang}">{code}</code>' if lang else code
        out.append(f"<pre>{inner}</pre>")
        pos = m.end()
    out.append(_block(text[pos:]))
    return "".join(out)


def _block(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        h = _HEADING.match(line)
        if h:
            lines.append(f"<b>{_inline(h.group(1))}</b>")
            continue
        b = _BULLET.match(line)
        if b:
            line = f"{b.group(1)}• {line[b.end():]}"
        lines.append(_inline(line))
    return "\n".join(lines)


# What a message without text carries, as the manager is told it. Fulla can
# see none of it yet, so the note says so and the caption goes with it.
# Animation before document: Telegram sends a GIF with both keys.
_ATTACHMENTS = (
    ("photo", "a photo"),
    ("animation", "a GIF"),
    ("video", "a video"),
    ("video_note", "a video message"),
    ("voice", "a voice message"),
    ("audio", "an audio file"),
    ("document", "a file"),
    ("sticker", "a sticker"),
    ("location", "a location"),
    ("contact", "a contact"),
    ("poll", "a poll"),
)


def _attachment_text(m: dict) -> str | None:
    """The text for a message that carries something other than text: a note
    of what it was, then its caption. None for a service message."""
    for key, what in _ATTACHMENTS:
        if key in m:
            break
    else:
        return None
    if key == "document" and m["document"].get("file_name"):
        what += f" ({m['document']['file_name']})"
    elif key == "sticker" and m["sticker"].get("emoji"):
        what += f" ({m['sticker']['emoji']})"
    note = f"[The owner sent {what}, which you can't see.]"
    caption = (m.get("caption") or "").strip()
    return f"{note}\n{caption}" if caption else note


def parse_update(update: dict) -> Inbound | None:
    """Turn one Bot API update into an Inbound, or None if it's not something we handle."""
    if "message" in update:
        m = update["message"]
        text = m.get("text") or _attachment_text(m)
        if not text:
            return None
        reply_to = m.get("reply_to_message") or {}
        chat, sender = m["chat"], m.get("from", {})
        raw = m.get("text") or m.get("caption") or ""
        mentions = [raw[e["offset"]:e["offset"] + e["length"]] for e in (m.get("entities") or [])
                    if e.get("type") == "mention" and isinstance(e.get("offset"), int) and isinstance(e.get("length"), int)]
        return Inbound(
            kind="text",
            transport="telegram",
            user_id=sender.get("id", 0),
            chat_id=chat["id"],
            group=chat.get("type") in ("group", "supergroup"),
            chat_title=chat.get("title") or "",
            sender_name=" ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x),
            mentions=mentions,
            text=text,
            message_id=m.get("message_id"),
            reply_to_message_id=reply_to.get("message_id"),
        )
    if "callback_query" in update:
        q = update["callback_query"]
        msg = q.get("message") or {}
        return Inbound(
            kind="callback",
            transport="telegram",
            user_id=q.get("from", {}).get("id", 0),
            chat_id=msg.get("chat", {}).get("id", 0),
            group=msg.get("chat", {}).get("type") in ("group", "supergroup"),
            message_id=msg.get("message_id"),
            callback_id=q.get("id"),
            data=q.get("data"),
        )
    if "message_reaction" in update:
        r = update["message_reaction"]
        old = {x.get("emoji") for x in r.get("old_reaction", [])}
        added = [x["emoji"] for x in r.get("new_reaction", [])
                 if x.get("type") == "emoji" and x.get("emoji") not in old]
        if not added:
            return None  # a reaction taken back, or a custom one with no emoji
        return Inbound(
            kind="reaction",
            transport="telegram",
            user_id=r.get("user", {}).get("id", 0),
            chat_id=r.get("chat", {}).get("id", 0),
            group=r.get("chat", {}).get("type") in ("group", "supergroup"),
            text=added[-1],
            message_id=r.get("message_id"),
            date=r.get("date"),
        )
    return None


def _keyboard(buttons: Buttons) -> dict:
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row]
                                for row in buttons]}


class Telegram:
    name = "telegram"
    buttons = True

    def __init__(self, bot_token: str, allowed_user_id: int = 0, *, base_url: str | None = None):
        """`allowed_user_id` is the owner; kept for the record, since who may
        talk is the channel's decision (people.py), not the transport's."""
        self._base = base_url or f"https://api.telegram.org/bot{bot_token}"
        self._allowed = allowed_user_id
        self._http = httpx.Client(timeout=40)
        self._username: str | None = None

    def username(self) -> str:
        """The bot's @username, for spotting a mention in a group. Asked of
        Telegram once; "" if it would not say."""
        if self._username is None:
            try:
                self._username = self._call("getMe").get("username") or ""
            except Exception as e:
                logger.info(f"getMe failed: {e}")
                return ""
        return self._username  # pyright: ignore[reportReturnType]

    def _call(self, method: str, **params) -> Any:
        resp = self._http.post(f"{self._base}/{method}", json=params)
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"telegram {method}: {body.get('description')}")
        return body["result"]

    def _formatted(self, method: str, text: str, mentions: Mentions | None = None, **params) -> dict:
        """Call a text-bearing method with the Markdown rendered as HTML, and
        once more as plain text if Telegram could not parse the markup."""
        try:
            return self._call(method, text=tag(render(text), mentions or []), parse_mode="HTML", **params)
        except RuntimeError as e:
            if "can't parse" not in str(e):
                raise
            logger.info(f"{method}: {e}; sending plain")
            return self._call(method, text=text, **params)

    def send(self, chat_id: int, text: str, buttons: Buttons | None = None, mentions: Mentions | None = None, *,
             plain: bool = False) -> int:
        """Send text written in light Markdown, optionally with inline buttons
        as rows of (label, callback_data), tagging `mentions` where the text
        says "@<name>" (a user link, which works for anyone the bot has seen).
        `plain` sends the text exactly as given, for something the owner must
        see verbatim (a draft to approve); it tags no one.

        Returns the message id.
        """
        params: dict = {"chat_id": chat_id}
        if buttons:
            params["reply_markup"] = _keyboard(buttons)
        if plain:
            return self._call("sendMessage", text=text, **params)["message_id"]
        return self._formatted("sendMessage", text, mentions=mentions, **params)["message_id"]

    def edit(self, chat_id: int, message_id: int, text: str,
             buttons: Buttons | None = None, *, plain: bool = False) -> None:
        """Replace a message's text; its buttons become `buttons`, none by default."""
        params: dict = {"chat_id": chat_id, "message_id": message_id}
        if buttons:
            params["reply_markup"] = _keyboard(buttons)
        try:
            if plain:
                self._call("editMessageText", text=text, **params)
            else:
                self._formatted("editMessageText", text, **params)
        except RuntimeError as e:
            # "message is not modified" is Telegram being literal, not a failure.
            if "not modified" not in str(e):
                raise

    def delete(self, chat_id: int, message_id: int) -> bool:
        """Delete a message. False if Telegram would not (too old, already
        gone); the caller edits it instead."""
        try:
            self._call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except RuntimeError as e:
            logger.info(f"deleteMessage {message_id}: {e}")
            return False
        return True

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        """Show "typing…" in the chat. Telegram clears it after about five
        seconds or at the next message, so a long wait re-sends it."""
        self._call("sendChatAction", chat_id=chat_id, action=action)

    def as_reaction(self, text: str) -> str | None:
        return as_reaction(text)

    def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        """React to a message, replacing the bot's reaction on it if it had one.
        `emoji` must be one of REACTIONS."""
        self._call("setMessageReaction", chat_id=chat_id, message_id=message_id,
                   reaction=[{"type": "emoji", "emoji": emoji}])

    def set_webhook(self, url: str, secret_token: str) -> None:
        """Have Telegram POST updates to `url`, each carrying `secret_token` in
        X-Telegram-Bot-Api-Secret-Token. Disables getUpdates for this bot."""
        self._call(
            "setWebhook",
            url=url,
            secret_token=secret_token,
            allowed_updates=ALLOWED_UPDATES,
            drop_pending_updates=False,
        )

    def delete_webhook(self) -> None:
        self._call("deleteWebhook")

    def answer_callback(self, callback_id: str | None, text: str | None = None) -> None:
        params: dict = {"callback_query_id": callback_id}
        if text:
            params["text"] = text
        self._call("answerCallbackQuery", **params)

    def poll(self, handler: Callable[[Inbound], None], stop: threading.Event) -> None:
        """Long-poll getUpdates until `stop` is set, handing each update to
        `handler`, which decides who may talk."""
        offset = None
        while not stop.is_set():
            try:
                params: dict = {"timeout": 30, "allowed_updates": ALLOWED_UPDATES}
                if offset is not None:
                    params["offset"] = offset
                updates = self._call("getUpdates", **params)
            except Exception as e:  # network blips are normal here
                logger.warning(f"getUpdates failed: {e}")
                stop.wait(5)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                inbound = parse_update(update)
                if inbound is None:
                    continue
                try:
                    handler(inbound)
                except Exception:
                    logger.exception("handler failed")
