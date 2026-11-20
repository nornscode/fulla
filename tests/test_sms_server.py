"""The /sms route: Twilio's signed form in, empty TwiML out."""

import base64
import hashlib
import hmac
import threading

import httpx
import pytest

from fulla.channel import NotTaken
from fulla.server import serve

NORNS_SECRET = "norns-secret"
SMS_TOKEN = "twilio-auth-token"
SMS_URL = "https://fulla.example/sms"
OWNER = "+16475550100"


class FakeChannel:
    def __init__(self):
        self.inbound = []
        self.norns_down = False

    def handle(self, inbound):
        if self.norns_down:
            raise NotTaken("connection refused")
        self.inbound.append(inbound)

    def handle_event(self, event):
        raise AssertionError("not expected here")


@pytest.fixture
def sms_server():
    channel = FakeChannel()
    srv = serve(channel, port=0, telegram_secret="", norns_secret=NORNS_SECRET,
                sms_auth_token=SMS_TOKEN, sms_url=SMS_URL)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield channel, httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")
    srv.shutdown()


@pytest.fixture
def server():
    channel = FakeChannel()
    srv = serve(channel, port=0, telegram_secret="tg", norns_secret=NORNS_SECRET)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield channel, httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")
    srv.shutdown()


def twilio_form(sender: str, text: str, sid: str = "SM1", **extra) -> tuple[dict, dict]:
    """A Twilio webhook form and its headers, signed over the URL it was told."""
    fields = {"MessageSid": sid, "From": sender, "To": "+16045550199", "Body": text, "NumMedia": "0", **extra}
    data = SMS_URL + "".join(k + v for k, v in sorted(fields.items()))
    sig = base64.b64encode(hmac.new(SMS_TOKEN.encode(), data.encode(), hashlib.sha1).digest()).decode()
    return fields, {"X-Twilio-Signature": sig}


def test_sms_with_good_signature_reaches_channel_and_is_answered_with_empty_twiml(sms_server):
    channel, http = sms_server
    fields, headers = twilio_form(OWNER, "hi")
    resp = http.post("/sms", data=fields, headers=headers)
    assert resp.status_code == 200 and resp.headers["content-type"] == "text/xml"
    assert resp.text.endswith("<Response></Response>")
    assert [(i.kind, i.text, i.user_id, i.message_id, i.transport) for i in channel.inbound] == [
        ("text", "hi", OWNER, "SM1", "sms")]


def test_sms_badly_signed_is_refused_and_a_stranger_is_the_channels_call(sms_server):
    channel, http = sms_server
    fields, headers = twilio_form(OWNER, "hi")
    assert http.post("/sms", data=fields).status_code == 403
    assert http.post("/sms", data={**fields, "Body": "hi!"}, headers=headers).status_code == 403
    status, status_headers = twilio_form(OWNER, "", MessageStatus="delivered")
    resp = http.post("/sms", data=status, headers=status_headers)
    assert resp.status_code == 200 and channel.inbound == []
    stranger, headers = twilio_form("+15550001111", "yo", sid="SM2")
    assert http.post("/sms", data=stranger, headers=headers).status_code == 200
    assert [i.user_id for i in channel.inbound] == ["+15550001111"]  # the channel decides


def test_sms_norns_could_not_take_is_answered_503_so_twilios_fallback_brings_it_again(sms_server):
    channel, http = sms_server
    channel.norns_down = True
    fields, headers = twilio_form(OWNER, "hi")
    resp = http.post("/sms", data=fields, headers=headers)
    assert resp.status_code == 503 and resp.json()["retry"] is True
    channel.norns_down = False
    assert http.post("/sms", data=fields, headers=headers).status_code == 200
    assert [i.text for i in channel.inbound] == ["hi"]


def test_sms_route_absent_without_twilio(server):
    _, http = server
    fields, headers = twilio_form(OWNER, "hi")
    assert http.post("/sms", data=fields, headers=headers).status_code == 404
