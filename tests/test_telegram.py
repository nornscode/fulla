import threading

from fulla.telegram import Telegram, as_reaction, parse_update, render


def test_text_message():
    u = {"update_id": 1, "message": {"message_id": 10, "text": "hi", "from": {"id": 7},
                                      "chat": {"id": 7}}}
    i = parse_update(u)
    assert i.kind == "text" and i.text == "hi" and i.user_id == 7 and i.chat_id == 7
    assert i.reply_to_message_id is None


def test_reply_to_question():
    u = {"update_id": 1, "message": {"message_id": 11, "text": "yes", "from": {"id": 7},
                                      "chat": {"id": 7}, "reply_to_message": {"message_id": 10}}}
    assert parse_update(u).reply_to_message_id == 10


def test_callback():
    u = {"update_id": 2, "callback_query": {"id": "cb1", "data": "a:42:yes", "from": {"id": 7},
                                             "message": {"message_id": 10, "chat": {"id": 7}}}}
    i = parse_update(u)
    assert i.kind == "callback" and i.data == "a:42:yes" and i.message_id == 10
    assert i.callback_id == "cb1"


def _message(**fields):
    return {"update_id": 3, "message": {"message_id": 12, "from": {"id": 7}, "chat": {"id": 7}, **fields}}


def test_photo_with_caption_is_passed_on_with_a_note():
    i = parse_update(_message(photo=[{"file_id": "a"}], caption="Why did my cookies burn"))
    assert i.kind == "text" and i.message_id == 12
    assert i.text == "[The owner sent a photo, which you can't see.]\nWhy did my cookies burn"


def test_attachment_without_caption_is_still_passed_on():
    assert parse_update(_message(photo=[{"file_id": "a"}])).text == (
        "[The owner sent a photo, which you can't see.]")
    assert parse_update(_message(voice={"file_id": "v"})).text == (
        "[The owner sent a voice message, which you can't see.]")


def test_attachment_kinds_are_named():
    doc = parse_update(_message(document={"file_name": "receipt.pdf"}, caption=" file this "))
    assert doc.text == "[The owner sent a file (receipt.pdf), which you can't see.]\nfile this"
    gif = parse_update(_message(animation={}, document={"file_name": "x.mp4"}))
    assert gif.text == "[The owner sent a GIF, which you can't see.]"
    assert parse_update(_message(sticker={"emoji": "😂"})).text == (
        "[The owner sent a sticker (😂), which you can't see.]")


def test_attachment_reply_keeps_reply_to():
    i = parse_update(_message(photo=[{}], reply_to_message={"message_id": 10}))
    assert i.reply_to_message_id == 10


def test_service_message_ignored():
    assert parse_update(_message(new_chat_title="x")) is None


def test_unknown_update_ignored():
    assert parse_update({"update_id": 4, "edited_message": {}}) is None


# --- rendering ---------------------------------------------------------------


def test_render_bold_and_bullets():
    text = "Here's what I can do:\n\n- **Calendar**: show your schedule\n- **Memory**: recall"
    assert render(text) == (
        "Here's what I can do:\n\n• <b>Calendar</b>: show your schedule\n• <b>Memory</b>: recall"
    )


def test_render_inline_code_is_escaped_not_formatted():
    assert render("run `a **b** < c`") == "run <code>a **b** &lt; c</code>"


def test_render_escapes_html_in_prose():
    assert render("Run 3 failed: <urlopen error> & more") == "Run 3 failed: &lt;urlopen error&gt; &amp; more"


def test_render_underscores_inside_words_stay():
    assert render("use schedule_reminder, not list_reminders") == "use schedule_reminder, not list_reminders"
    assert render("_really_ do it") == "<i>really</i> do it"


def test_render_italic_and_strike_and_link():
    assert render("*soon* ~~never~~ [docs](https://x.y/z)") == (
        '<i>soon</i> <s>never</s> <a href="https://x.y/z">docs</a>'
    )
    assert render("2 * 3 * 4") == "2 * 3 * 4"


def test_render_heading_and_fence():
    text = "# Today\nx\n```sh\nls <dir>\n```\ndone"
    assert render(text) == '<b>Today</b>\nx\n<pre><code class="language-sh">ls &lt;dir&gt;</code></pre>\ndone'
    assert render("```\na\n```") == "<pre>a</pre>"


class _Calls:
    def __init__(self, fail_first=False):
        self.calls = []
        self.fail_first = fail_first

    def __call__(self, method, **params):
        self.calls.append((method, params))
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("telegram sendMessage: Bad Request: can't parse entities: boo")
        return {"message_id": 5}


def test_send_renders_html():
    tg = Telegram("t", 1)
    tg._call = calls = _Calls()
    assert tg.send(1, "**hi**", buttons=[[("Ok", "a:1:ok")]]) == 5
    method, params = calls.calls[0]
    assert method == "sendMessage" and params["text"] == "<b>hi</b>" and params["parse_mode"] == "HTML"
    assert params["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "a:1:ok"


def test_send_falls_back_to_plain_text():
    tg = Telegram("t", 1)
    tg._call = calls = _Calls(fail_first=True)
    assert tg.send(1, "**hi**") == 5
    assert calls.calls[1] == ("sendMessage", {"chat_id": 1, "text": "**hi**"})


def test_edit_renders_and_ignores_not_modified():
    tg = Telegram("t", 1)
    tg._call = calls = _Calls()
    tg.edit(1, 9, "- a")
    assert calls.calls[0][1]["text"] == "• a"

    def not_modified(method, **params):
        raise RuntimeError("telegram editMessageText: Bad Request: message is not modified")

    tg._call = not_modified
    tg.edit(1, 9, "same")


def test_plain_send_and_edit_show_text_verbatim_with_buttons():
    tg = Telegram("t", 1)
    tg._call = calls = _Calls()
    tg.send(1, "**not bold** <b>", buttons=[[("Send", "k:a:b:send")]], plain=True)
    assert calls.calls[0][1]["text"] == "**not bold** <b>" and "parse_mode" not in calls.calls[0][1]
    tg.edit(1, 9, "*x*", buttons=[[("Cancel", "k:a:b:cancel")]], plain=True)
    method, params = calls.calls[1]
    assert method == "editMessageText" and params["text"] == "*x*" and "parse_mode" not in params
    assert params["reply_markup"]["inline_keyboard"][0][0]["text"] == "Cancel"


# --- reactions ---------------------------------------------------------------


def _reaction(old, new):
    return {"update_id": 5, "message_reaction": {
        "chat": {"id": 7}, "message_id": 10, "user": {"id": 7}, "date": 1700000000,
        "old_reaction": [{"type": "emoji", "emoji": e} for e in old],
        "new_reaction": [{"type": "emoji", "emoji": e} for e in new]}}


def test_reaction_added():
    i = parse_update(_reaction([], ["👍"]))
    assert i.kind == "reaction" and i.text == "👍" and i.message_id == 10
    assert i.user_id == 7 and i.chat_id == 7 and i.date == 1700000000


def test_reaction_taken_back_is_ignored():
    assert parse_update(_reaction(["👍"], [])) is None
    assert parse_update(_reaction(["👍", "🔥"], ["👍"])) is None


def test_reaction_reports_the_one_added():
    assert parse_update(_reaction(["👍"], ["👍", "🔥"])).text == "🔥"


def test_webhook_and_polling_ask_for_reactions():
    calls = []
    tg = Telegram("t", 7)
    tg._call = lambda method, **params: calls.append(params) or []
    tg.set_webhook("https://x/telegram", "s")
    stop = threading.Event()
    tg._call = lambda method, **params: calls.append(params) or stop.set() or []
    tg.poll(lambda inbound: None, stop)
    assert all("message_reaction" in c["allowed_updates"] for c in calls) and len(calls) == 2


def test_set_reaction():
    calls = []
    tg = Telegram("t", 7)
    tg._call = lambda method, **params: calls.append((method, params))
    tg.set_reaction(7, 10, "👀")
    assert calls == [("setMessageReaction",
                      {"chat_id": 7, "message_id": 10, "reaction": [{"type": "emoji", "emoji": "👀"}]})]


def test_as_reaction():
    assert as_reaction(" 👍 ") == "👍"
    assert as_reaction("❤️") == "❤"
    assert as_reaction("✅") is None
    assert as_reaction("👍 thanks") is None


def test_mentions_become_user_links_where_the_text_names_them():
    from fulla.telegram import render, tag
    assert tag(render("⏰ @Sam: stretch"), [(42, "Sam"), (43, "Kim")]) == \
        '⏰ <a href="tg://user?id=42">@Sam</a>: stretch'
    tg = Telegram("t", 1)
    tg._call = calls = _Calls()
    tg.send(1, "⏰ @Sam: go", mentions=[(42, "Sam")])
    assert calls.calls[0][1]["text"] == '⏰ <a href="tg://user?id=42">@Sam</a>: go'
