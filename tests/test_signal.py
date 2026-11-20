"""The Signal transport: rendering, envelopes in, calls out."""

import json
import threading

import httpx

from fulla.signal import Signal, choices, is_emoji, mention_ranges, parse_envelope, render

OWNER = "+16475550100"
FULLA = "+16475550199"


def envelope(dm: dict, *, sender: str = OWNER, ts: int = 1700000000000) -> dict:
    return {"envelope": {"source": sender, "sourceNumber": sender, "sourceUuid": "u-1",
                         "timestamp": ts, "dataMessage": {"timestamp": ts, **dm}}}


# ------------------------------------------------------------------ render


def test_render_keeps_bold_italic_and_code_and_rewrites_the_rest():
    assert render("**hi** *there* `x`") == "**hi** *there* `x`"
    assert render("__hi__ _there_ ~~gone~~") == "**hi** *there* ~gone~"
    assert render("snake_case stays") == "snake_case stays"


def test_render_headings_bullets_links_and_fences():
    assert render("# Today\n- a\n* b") == "**Today**\n• a\n• b"
    assert render("see [docs](https://x.y/z)") == "see docs (https://x.y/z)"
    assert render("[https://x.y](https://x.y)") == "https://x.y"
    assert render("```py\nprint(1)\n```") == "`print(1)`"


def test_render_leaves_inline_code_alone():
    assert render("`a_b_c` and _i_") == "`a_b_c` and *i*"


def test_choices_line():
    assert choices([[("Cancel", "c:1")]]) == "Reply to this with “Cancel”."
    assert choices([[("Done", "a:1:done"), ("Snooze 1h", "a:1:snooze 1 hour")], [("Drop it", "a:1:drop")]]) == (
        "Reply to this with one of: Done · Snooze 1h · Drop it")


def test_is_emoji():
    assert is_emoji("👍") and is_emoji(" ❤️ ") and is_emoji("👍🏽") and is_emoji("👨‍💻") and is_emoji("🇨🇦")
    assert is_emoji("✅") and is_emoji("🫡")
    assert not is_emoji("ok") and not is_emoji("👍 thanks") and not is_emoji("") and not is_emoji("1")


# ------------------------------------------------------------------ inbound


def test_text_message():
    inbound = parse_envelope(envelope({"message": "hello"}))
    assert inbound.kind == "text" and inbound.text == "hello"
    assert inbound.user_id == OWNER and inbound.chat_id == OWNER
    assert inbound.message_id == 1700000000000 and inbound.reply_to_message_id is None


def test_quoted_reply_names_the_quoted_message():
    inbound = parse_envelope(envelope({"message": "done", "quote": {"id": 1699999999000, "author": FULLA}}))
    assert inbound.reply_to_message_id == 1699999999000


def test_reaction_and_reaction_taken_back():
    added = envelope({"reaction": {"emoji": "👍", "targetSentTimestamp": 1699999999000, "isRemove": False}})
    inbound = parse_envelope(added)
    assert inbound.kind == "reaction" and inbound.text == "👍"
    assert inbound.message_id == 1699999999000 and inbound.date == 1700000000000
    removed = envelope({"reaction": {"emoji": "👍", "targetSentTimestamp": 1699999999000, "isRemove": True}})
    assert parse_envelope(removed) is None


def test_attachments_are_passed_on_with_a_note():
    photo = envelope({"message": "look", "attachments": [{"contentType": "image/jpeg"}]})
    assert parse_envelope(photo).text == "[The owner sent a photo, which you can't see.]\nlook"
    voice = envelope({"attachments": [{"contentType": "audio/aac", "voiceNote": True}]})
    assert parse_envelope(voice).text == "[The owner sent a voice message, which you can't see.]"
    doc = envelope({"attachments": [{"contentType": "application/pdf", "filename": "a.pdf"}]})
    assert parse_envelope(doc).text == "[The owner sent a file (a.pdf), which you can't see.]"
    sticker = envelope({"sticker": {"packId": "x"}})
    assert parse_envelope(sticker).text == "[The owner sent a sticker, which you can't see.]"


def test_sender_falls_back_to_the_account_id_when_the_number_is_hidden():
    env = envelope({"message": "hi"})
    env["envelope"]["sourceNumber"] = None
    assert parse_envelope(env).user_id == "u-1"


def test_groups_receipts_typing_and_empty_are_ignored():
    in_group = parse_envelope(envelope({"message": "hi", "groupInfo": {"groupId": "g"}}))
    assert in_group.group and in_group.chat_id == "group:g"  # the channel decides whether it may speak there
    assert parse_envelope({"envelope": {"sourceNumber": OWNER, "receiptMessage": {"isDelivery": True}}}) is None
    assert parse_envelope({"envelope": {"sourceNumber": OWNER, "typingMessage": {"action": "STARTED"}}}) is None
    assert parse_envelope(envelope({"message": ""})) is None
    assert parse_envelope({}) is None


def test_allowed_by_number_or_account_id():
    sig = Signal("http://signal", FULLA, f"{OWNER}, u-1")
    assert sig.allowed(parse_envelope(envelope({"message": "hi"})))
    hidden = envelope({"message": "hi"})
    hidden["envelope"]["sourceNumber"] = None
    assert sig.allowed(parse_envelope(hidden))
    assert not sig.allowed(parse_envelope(envelope({"message": "hi"}, sender="+15550001111")))


# ----------------------------------------------------------------- outbound


def make(responder=None):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if responder:
            return responder(request)
        return httpx.Response(201, json=[{"timestamp": "1700000000123"}])  # one entry per recipient

    sig = Signal("http://signal", FULLA, OWNER)
    sig._http = httpx.Client(transport=httpx.MockTransport(handler))
    return sig, calls


def test_send_renders_styled_and_spells_out_buttons():
    sig, calls = make()
    assert sig.send(OWNER, "**hi**", buttons=[[("Cancel", "c:9")]]) == 1700000000123
    assert calls == [("POST", "/v2/send", {
        "number": FULLA, "recipients": [OWNER], "text_mode": "styled",
        "message": "**hi**\n\nReply to this with “Cancel”.",
    })]


def test_edit_sends_with_the_original_timestamp():
    sig, calls = make()
    sig.edit(OWNER, 1700000000123, "- done")
    assert calls[0][2]["edit_timestamp"] == 1700000000123 and calls[0][2]["message"] == "• done"


def test_a_plain_card_is_sent_verbatim_with_its_choices_and_edited_with_new_ones():
    sig, calls = make()
    sig.send(OWNER, "*Jane* - hi", buttons=[[("Send", "k:1:a:send"), ("Edit", "k:1:a:edit"),
                                          ("Cancel", "k:1:a:cancel")]], plain=True)
    assert calls[0][2]["text_mode"] == "normal"
    assert calls[0][2]["message"] == "*Jane* - hi\n\nReply to this with one of: Send · Edit · Cancel"
    sig.edit(OWNER, 1700000000123, "*Jane* - hi\n\nSend me the new text.",
             buttons=[[("Cancel", "k:1:a:cancel")]], plain=True)
    assert calls[1][2]["edit_timestamp"] == 1700000000123 and calls[1][2]["text_mode"] == "normal"
    assert calls[1][2]["message"].endswith("Send me the new text.\n\nReply to this with “Cancel”.")


def test_delete_is_a_remote_delete_and_reports_refusal():
    sig, calls = make()
    assert sig.delete(OWNER, 1700000000123) is True
    assert calls == [("DELETE", f"/v1/remote-delete/{FULLA}", {"recipient": OWNER, "timestamp": 1700000000123})]
    sig, _ = make(lambda req: httpx.Response(400, json={"error": "too old"}))
    assert sig.delete(OWNER, 1) is False


def test_typing_reaction_and_callback():
    sig, calls = make()
    sig.send_chat_action(OWNER)
    sig.set_reaction(OWNER, 1700000000123, "👀")
    sig.answer_callback("never", "nothing")
    assert calls == [
        ("PUT", f"/v1/typing-indicator/{FULLA}", {"recipient": OWNER}),
        ("POST", f"/v1/reactions/{FULLA}",
         {"recipient": OWNER, "reaction": "👀", "target_author": OWNER, "timestamp": 1700000000123}),
    ]


def test_as_reaction_is_any_one_emoji():
    sig, _ = make()
    assert sig.as_reaction(" ✅ ") == "✅"
    assert sig.as_reaction("✅ done") is None


def test_errors_name_the_call():
    sig, _ = make(lambda req: httpx.Response(500, text="boom"))
    try:
        sig.send(OWNER, "hi")
    except RuntimeError as e:
        assert "POST /v2/send: 500 boom" in str(e)
    else:
        raise AssertionError("expected an error")


def test_poll_hands_every_message_over_and_skips_receipts(monkeypatch):
    frames = [json.dumps(envelope({"message": "yo"}, sender="+15550001111")),
              json.dumps(envelope({"message": "hi"})),
              json.dumps({"envelope": {"sourceNumber": OWNER, "receiptMessage": {}}})]
    stop = threading.Event()

    class FakeWs:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def recv(self, timeout=None):
            if not frames:
                stop.set()
                raise TimeoutError
            return frames.pop(0)

    seen = []
    import websockets.sync.client as wsc
    monkeypatch.setattr(wsc, "connect", lambda url: seen.append(url) or FakeWs())
    sig = Signal("http://signal:8080", FULLA, OWNER)
    got = []
    sig.poll(got.append, stop)
    assert seen == [f"ws://signal:8080/v1/receive/{FULLA}"]
    assert [i.text for i in got] == ["yo", "hi"]  # who may talk is the channel's call


def test_mentions_are_listed_and_put_back_into_the_text():
    payload = envelope({"message": "￼ you alive? ask ￼ too",
                        "mentions": [{"name": "Fulla", "number": FULLA, "uuid": "u-f", "start": 0, "length": 1},
                                     {"name": "Sam", "number": "+15550001111", "uuid": "u-s", "start": 17, "length": 1}]})
    inbound = parse_envelope(payload)
    assert inbound.text == "@Fulla you alive? ask @Sam too"
    assert inbound.mentions == [FULLA, "u-f", "+15550001111", "u-s"]


def test_group_messages_name_the_group_and_send_goes_to_its_api_id():
    from fulla.signal import group_recipient

    payload = envelope({"message": "Fulla, settle this", "groupInfo": {"groupId": "abc123==", "type": "DELIVER"}})
    payload["envelope"]["sourceName"] = "Sam"
    inbound = parse_envelope(payload)
    assert inbound.group and inbound.chat_id == "group:abc123==" and inbound.user_id == OWNER
    assert inbound.sender_name == "Sam"
    assert group_recipient("group:abc123==") == "group.YWJjMTIzPT0="

    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(201, json=[{"timestamp": "1700000000123"}])  # one entry per recipient

    sig = Signal("http://signal:8080", FULLA, OWNER)
    sig._http = httpx.Client(transport=httpx.MockTransport(handler))
    sig.send("group:abc123==", "Settled.")
    assert sent[0]["recipients"] == ["group.YWJjMTIzPT0="]


def test_mentions_are_ranges_in_utf16_units_over_the_name_in_the_text():
    assert mention_ranges("⏰ @Sam: stretch", [("+16045550111", "Sam")]) == [
        {"author": "+16045550111", "start": 2, "length": 4}]
    assert mention_ranges("🎉 @Sam", [("u-sam", "Sam")]) == [{"author": "u-sam", "start": 3, "length": 4}]  # 🎉 is two units
    assert mention_ranges("no tag here", [("u-sam", "Sam")]) == []


def test_send_carries_mentions_only_when_the_text_names_them():
    sig, calls = make()
    sig.send("group:abc", "⏰ @Sam: stretch", mentions=[("+16045550111", "Sam"), ("u-kim", "Kim")])
    assert calls[0][2]["mentions"] == [{"author": "+16045550111", "start": 2, "length": 4}]
    sig.send(OWNER, "hi", mentions=[("u-kim", "Kim")])
    assert "mentions" not in calls[1][2]
