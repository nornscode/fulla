"""The SMS transport: rendering to plain text, splitting, Twilio's signature,
the webhook's form in, calls out."""

import base64
import hashlib
import hmac
import threading
from urllib.parse import parse_qs

import httpx

from fulla.sms import Sms, choices, chunks, parse_webhook, plain, render, verify_signature

OWNER = "+16475550100"
FULLA = "+16045550199"
SID, TOKEN = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "secret-token"
URL = "https://fulla.fly.dev/sms"


def form(**fields) -> dict:
    return {"MessageSid": "SM1", "AccountSid": SID, "From": OWNER, "To": FULLA, "NumMedia": "0", **fields}


# ------------------------------------------------------------------ render


def test_render_drops_markup_and_keeps_the_words():
    assert render("**hi** *there* `x` __u__ _i_ ~~gone~~") == "hi there x u i gone"
    assert render("snake_case stays and 2*3*4 stays") == "snake_case stays and 2*3*4 stays"


def test_render_headings_bullets_links_and_fences():
    assert render("# Today\n- a\n* b") == "Today\n- a\n- b"
    assert render("see [docs](https://x.y/z)") == "see docs (https://x.y/z)"
    assert render("[https://x.y](https://x.y)") == "https://x.y"
    assert render("```py\nprint(1)\n```") == "print(1)"


def test_render_swaps_what_would_make_the_text_ucs2_but_leaves_emoji():
    assert plain("wait… “ok” — fine • done") == 'wait... "ok" - fine - done'
    assert render("Done 👍 — all set") == "Done 👍 - all set"


# ------------------------------------------------------------------ chunks


def test_short_text_is_one_message_and_empty_is_one_too():
    assert chunks("hello") == ["hello"]
    assert chunks("") == [""]


def test_long_text_splits_at_paragraphs_then_lines_then_words():
    paras = [f"para {i} " + "x" * 60 for i in range(6)]
    out = chunks("\n\n".join(paras), limit=160)
    assert all(len(c) <= 160 for c in out) and len(out) > 1
    assert "".join(out).count("para") == 6 and "\n\n".join(out) == "\n\n".join(paras)
    assert out[0].startswith("para 0")

    lines = "\n".join(f"line {i}" for i in range(40))
    out = chunks(lines, limit=50)
    assert all(len(c) <= 50 for c in out) and "\n".join(out) == lines

    words = " ".join(["word"] * 100)
    out = chunks(words, limit=40)
    assert all(len(c) <= 40 for c in out) and " ".join(out) == words

    out = chunks("a" * 100, limit=40)  # nowhere to break
    assert out == ["a" * 40, "a" * 40, "a" * 20]


def test_choices_line_is_plain():
    assert choices([[("Cancel", "c:1")]]) == 'Reply "Cancel".'
    assert choices([[("Done", "a:1:done"), ("Snooze 1h", "a:1:snooze 1 hour")], [("Drop it", "a:1:drop")]]) == (
        "Reply with one of: Done, Snooze 1h, Drop it")


# ------------------------------------------------------------------ signature


def sign(url: str, params: list[tuple[str, str]], token: str = TOKEN) -> str:
    data = url + "".join(k + v for k, v in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def test_signature_covers_url_and_sorted_fields():
    params = [("To", FULLA), ("From", OWNER), ("Body", "hi"), ("MessageSid", "SM1")]
    header = sign(URL, params)
    assert verify_signature(TOKEN, URL, params, header)
    assert verify_signature(TOKEN, URL, list(reversed(params)), header)  # order on the wire does not matter
    assert not verify_signature(TOKEN, URL, params + [("Body", "hi!")], header)
    assert not verify_signature(TOKEN, "https://elsewhere.example/sms", params, header)
    assert not verify_signature("other-token", URL, params, header)
    assert not verify_signature(TOKEN, URL, params, "")
    assert not verify_signature("", URL, params, header)


# ------------------------------------------------------------------ inbound


def test_text_message():
    inbound = parse_webhook(form(Body="hello"))
    assert inbound.kind == "text" and inbound.text == "hello" and inbound.transport == "sms"
    assert inbound.user_id == OWNER and inbound.chat_id == OWNER and inbound.message_id == "SM1"
    assert inbound.reply_to_message_id is None and not inbound.group


def test_media_is_passed_on_with_a_note():
    photo = parse_webhook(form(Body="look", NumMedia="1", MediaContentType0="image/jpeg", MediaUrl0="https://x"))
    assert photo.text == "[The owner sent a photo, which you can't see.]\nlook"
    voice = parse_webhook(form(Body="", NumMedia="1", MediaContentType0="audio/amr"))
    assert voice.text == "[The owner sent a voice message, which you can't see.]"
    many = parse_webhook(form(Body="", NumMedia="2", MediaContentType0="image/png"))
    assert many.text == "[The owner sent 2 files, which you can't see.]"


def test_what_is_not_a_message_is_nothing():
    assert parse_webhook({}) is None
    assert parse_webhook(form(Body="")) is None  # no text, no media
    assert parse_webhook({"MessageSid": "SM1", "MessageStatus": "delivered"}) is None  # a status callback


# ------------------------------------------------------------------ outbound


def twilio(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        body = parse_qs(request.content.decode()) if request.content else {}
        calls.append((request.method, request.url.path, request.url.params, {k: v[0] for k, v in body.items()},
                      request.headers.get("authorization")))
        if request.url.path.endswith("/Messages.json") and request.method == "POST":
            return httpx.Response(201, json={"sid": f"SM{len(calls)}", "status": "queued"})
        if request.url.path.endswith("/IncomingPhoneNumbers.json"):
            return httpx.Response(200, json={"incoming_phone_numbers": [{"sid": "PN1", "phone_number": FULLA}]})
        if "/IncomingPhoneNumbers/PN1.json" in request.url.path:
            return httpx.Response(200, json={"sid": "PN1"})
        return httpx.Response(404, json={"message": "not found"})
    return handler


def make():
    calls: list = []
    texts = Sms(SID, TOKEN, FULLA, OWNER)
    texts._http = httpx.Client(transport=httpx.MockTransport(twilio(calls)), auth=(SID, TOKEN))
    return texts, calls


def test_send_renders_plain_adds_the_choices_and_returns_the_last_sid():
    texts, calls = make()
    sid = texts.send(OWNER, "**Did you?**", buttons=[[("Done", "a:1:done"), ("Drop it", "a:1:drop")]])
    assert sid == "SM1"
    method, path, _, body, auth = calls[0]
    assert method == "POST" and path == f"/2010-04-01/Accounts/{SID}/Messages.json"
    assert body == {"To": OWNER, "From": FULLA, "Body": "Did you?\n\nReply with one of: Done, Drop it"}
    assert auth == "Basic " + base64.b64encode(f"{SID}:{TOKEN}".encode()).decode()


def test_long_text_goes_as_several_messages_in_order():
    texts, calls = make()
    text = "\n\n".join(f"Paragraph {i}. " + "words " * 150 for i in range(4))
    sid = texts.send(OWNER, text)
    bodies = [c[3]["Body"] for c in calls]
    assert len(bodies) > 1 and all(len(b) <= 1600 for b in bodies)
    assert bodies[0].startswith("Paragraph 0") and bodies[-1].rstrip().endswith("words")
    assert sid == f"SM{len(calls)}"


def test_plain_is_sent_as_given():
    texts, calls = make()
    texts.send(OWNER, "*literally* this", plain=True)
    assert calls[0][3]["Body"] == "*literally* this"


def test_a_refusal_is_an_error_with_twilios_reason():
    texts = Sms(SID, TOKEN, FULLA, OWNER)
    texts._http = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"code": 21211, "message": "Invalid 'To' Phone Number"})))
    try:
        texts.send("nonsense", "hi")
    except RuntimeError as e:
        assert "Invalid 'To' Phone Number" in str(e)
    else:
        raise AssertionError("no error")


def test_what_sms_cannot_do_does_nothing():
    texts, calls = make()
    texts.edit(OWNER, "SM1", "changed")
    assert texts.delete(OWNER, "SM1") is True
    texts.send_chat_action(OWNER)
    texts.set_reaction(OWNER, "SM1", "👍")
    texts.answer_callback(None)
    assert texts.as_reaction("👍") is None
    assert calls == []
    assert texts.allowed(parse_webhook(form(Body="hi"))) and not texts.allowed(parse_webhook(form(Body="hi", From="+1555")))


def test_set_webhook_finds_the_number_and_points_it_and_its_fallback_here():
    texts, calls = make()
    texts.set_webhook(URL)
    assert calls[0][0] == "GET" and calls[0][2]["PhoneNumber"] == FULLA
    method, path, _, body, _ = calls[1]
    assert method == "POST" and path.endswith("/IncomingPhoneNumbers/PN1.json")
    assert body == {"SmsUrl": URL, "SmsMethod": "POST", "SmsFallbackUrl": URL, "SmsFallbackMethod": "POST"}
    texts.set_webhook(URL)
    assert len(calls) == 3  # the number's sid is remembered


def test_poll_hands_over_only_what_arrives_after_start():
    pages = [
        [{"sid": "SM0", "direction": "inbound", "from": OWNER, "body": "old"}],
        [{"sid": "SM9", "direction": "outbound-api", "from": FULLA, "body": "ours"},
         {"sid": "SM2", "direction": "inbound", "from": OWNER, "body": "new"},
         {"sid": "SM0", "direction": "inbound", "from": OWNER, "body": "old"}],
    ]
    stop = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        page = pages.pop(0) if pages else []
        if not pages:
            stop.set()
        return httpx.Response(200, json={"messages": page})

    texts = Sms(SID, TOKEN, FULLA, OWNER)
    texts._http = httpx.Client(transport=httpx.MockTransport(handler))
    got = []
    texts.poll(got.append, stop, interval=0)
    assert [(i.text, i.message_id) for i in got] == [("new", "SM2")]
