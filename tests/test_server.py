"""The HTTP receiver over a real socket, with a fake channel behind it."""

import hashlib
import hmac
import json
import threading
import time

import httpx
import pytest

from fulla.channel import NotTaken
from fulla.server import serve

NORNS_SECRET = "norns-secret"
TG_SECRET = "tg-secret"
SIGNAL_SECRET = "signal-secret"
OWNER = "+16475550100"


class FakeChannel:
    def __init__(self):
        self.inbound = []
        self.events = []
        self.told = []
        self.norns_down = False

    def tell_owner(self, text):
        self.told.append(text)

    def handle(self, inbound):
        if self.norns_down:
            raise NotTaken("connection refused")
        self.inbound.append(inbound)

    def handle_event(self, event):
        self.events.append(event)


@pytest.fixture
def server():
    channel = FakeChannel()
    srv = serve(channel, port=0, telegram_secret=TG_SECRET, norns_secret=NORNS_SECRET)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield channel, httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")
    srv.shutdown()


def signed(body: bytes, secret: str = NORNS_SECRET) -> dict:
    t = int(time.time())
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Norns-Signature": f"t={t},v1={mac}", "Content-Type": "application/json"}


def envelope(sender: str, text: str) -> bytes:
    return json.dumps({"envelope": {"sourceNumber": sender, "timestamp": 1700000000000,
                                    "dataMessage": {"timestamp": 1700000000000, "message": text}}}).encode()


@pytest.fixture
def signal_server():
    channel = FakeChannel()
    srv = serve(channel, port=0, telegram_secret="", norns_secret=NORNS_SECRET, signal_secret=SIGNAL_SECRET)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield channel, httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")
    srv.shutdown()


EGDIR_SECRET = "egdir-secret"


@pytest.fixture
def egdir_server():
    channel = FakeChannel()
    srv = serve(channel, port=0, telegram_secret="", norns_secret=NORNS_SECRET, egdir_secret=EGDIR_SECRET)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield channel, httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}")
    srv.shutdown()


def hook(**fields) -> dict:
    base = {"host": "box", "window": "norns", "at": "2026-10-27T18:02:11Z", "event": "Stop",
            "session_id": "abc", "cwd": "/home/robin/projects/norns", "message": None, "title": None,
            "notification_type": None, "state": "idle"}
    return {**base, **fields}


def test_egdir_question_becomes_one_line_to_the_owner(egdir_server):
    channel, http = egdir_server
    event = hook(event="Notification", notification_type="permission_prompt",
                 message="Claude needs your permission to use Bash", state="waiting")
    resp = http.post("/egdir/hook", json=event, headers={"Authorization": f"Bearer {EGDIR_SECRET}"})
    assert resp.status_code == 200
    assert channel.told == ["norns on box is waiting: Claude needs your permission to use Bash"]


def test_egdir_relays_only_questions(egdir_server):
    """Stop fires for every Claude turn in tmux on the box, the owner's own included."""
    channel, http = egdir_server
    auth = {"Authorization": f"Bearer {EGDIR_SECRET}"}
    assert http.post("/egdir/hook", json=hook(), headers=auth).json() == {"ok": True, "ignored": True}
    idle = hook(event="Notification", notification_type="idle_prompt", message="Claude is waiting for your input")
    assert http.post("/egdir/hook", json=idle, headers=auth).json() == {"ok": True, "ignored": True}
    assert http.post("/egdir/hook", json=hook(event="SessionEnd", state="done"), headers=auth).json() == {"ok": True, "ignored": True}
    assert http.post("/egdir/hook", json=hook(event="PermissionRequest", state="waiting"), headers=auth).status_code == 200
    assert channel.told == ["norns on box is waiting for an answer"]


def test_egdir_hook_needs_the_bearer_and_is_absent_without_a_secret(egdir_server, server):
    channel, http = egdir_server
    event = hook(event="Notification", message="a question", state="waiting")
    assert http.post("/egdir/hook", json=event).status_code == 401
    assert http.post("/egdir/hook", json=event, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert http.post("/egdir/hook", content=b"not json", headers={"Authorization": f"Bearer {EGDIR_SECRET}"}).status_code == 400
    assert channel.told == []
    _, plain = server
    assert plain.post("/egdir/hook", json=event, headers={"Authorization": f"Bearer {EGDIR_SECRET}"}).status_code == 404


def test_health(server):
    _, http = server
    body = http.get("/").json()
    assert body["ok"] is True and body["awake"] is False


def test_norns_event_with_good_signature_reaches_channel(server):
    channel, http = server
    body = json.dumps({"type": "run.completed", "run_id": 3, "agent_id": 1, "output": "hi"}).encode()
    assert http.post("/norns/events", content=body, headers=signed(body)).status_code == 200
    assert channel.events == [json.loads(body)]


def test_norns_event_with_bad_signature_is_refused(server):
    channel, http = server
    body = b'{"type":"run.completed","run_id":3}'
    headers = signed(body)
    assert http.post("/norns/events", content=body + b" ", headers=headers).status_code == 401
    assert http.post("/norns/events", content=body).status_code == 401
    assert channel.events == []


def test_telegram_update_with_secret_token_reaches_channel(server):
    channel, http = server
    update = {"update_id": 1, "message": {"message_id": 10, "text": "hi", "from": {"id": 7}, "chat": {"id": 7}}}
    resp = http.post("/telegram", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": TG_SECRET})
    assert resp.status_code == 200
    assert [i.text for i in channel.inbound] == ["hi"]


def test_telegram_update_without_token_is_refused_and_a_stranger_is_the_channels_call(server):
    channel, http = server
    update = {"update_id": 1, "message": {"message_id": 10, "text": "hi", "from": {"id": 7}, "chat": {"id": 7}}}
    assert http.post("/telegram", json=update).status_code == 403
    stranger = {"update_id": 2, "message": {"message_id": 11, "text": "yo", "from": {"id": 8}, "chat": {"id": 8}}}
    resp = http.post("/telegram", json=stranger, headers={"X-Telegram-Bot-Api-Secret-Token": TG_SECRET})
    assert resp.status_code == 200
    assert [(i.user_id, i.text) for i in channel.inbound] == [(8, "yo")]  # the channel drops or admits them


def test_signal_envelope_with_good_signature_reaches_channel(signal_server):
    channel, http = signal_server
    body = envelope(OWNER, "hi")
    assert http.post("/signal", content=body, headers=signed(body, SIGNAL_SECRET)).status_code == 200
    assert [(i.kind, i.text, i.user_id) for i in channel.inbound] == [("text", "hi", OWNER)]


def test_message_norns_could_not_take_is_answered_503_so_it_comes_again(signal_server):
    channel, http = signal_server
    channel.norns_down = True
    body = envelope(OWNER, "hi")
    resp = http.post("/signal", content=body, headers=signed(body, SIGNAL_SECRET))
    assert resp.status_code == 503 and resp.json()["retry"] is True
    channel.norns_down = False
    assert http.post("/signal", content=body, headers=signed(body, SIGNAL_SECRET)).status_code == 200
    assert [i.text for i in channel.inbound] == ["hi"]


def test_signal_envelope_badly_signed_or_from_stranger_is_dropped(signal_server):
    channel, http = signal_server
    body = envelope(OWNER, "hi")
    assert http.post("/signal", content=body, headers=signed(body)).status_code == 401
    assert http.post("/signal", content=body).status_code == 401
    receipt = json.dumps({"envelope": {"sourceNumber": OWNER, "receiptMessage": {}}}).encode()
    resp = http.post("/signal", content=receipt, headers=signed(receipt, SIGNAL_SECRET))
    assert resp.status_code == 200 and resp.json()["ignored"] is True
    assert channel.inbound == []
    stranger = envelope("+15550001111", "yo")
    resp = http.post("/signal", content=stranger, headers=signed(stranger, SIGNAL_SECRET))
    assert resp.status_code == 200
    assert [i.user_id for i in channel.inbound] == ["+15550001111"]  # the channel decides


def test_signal_route_absent_on_telegram(server):
    _, http = server
    body = envelope(OWNER, "hi")
    assert http.post("/signal", content=body, headers=signed(body, SIGNAL_SECRET)).status_code == 404


def test_unknown_path(server):
    _, http = server
    assert http.post("/nope", content=b"").status_code == 404


def test_wake_route_absent_without_incarnations(server):
    _, http = server
    assert http.post("/norns/wake", content=b"{}").status_code == 404


def test_wake_route_verifies_and_holds_until_incarnation_ends():
    from fulla.wake import Incarnations

    WAKE_SECRET = "wake-secret"
    channel = FakeChannel()
    inc = Incarnations()
    srv = serve(channel, port=0, telegram_secret=TG_SECRET, norns_secret=NORNS_SECRET,
                wake_secret=WAKE_SECRET, incarnations=inc)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    http = httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}", timeout=10)
    try:
        body = b'{"type":"wake","worker_id":"fulla"}'
        t = int(time.time())
        bad = hmac.new(b"other", f"{t}.".encode() + body, hashlib.sha256).hexdigest()
        assert http.post("/norns/wake", content=body,
                         headers={"X-Norns-Signature": f"t={t},v1={bad}"}).status_code == 401

        good = hmac.new(WAKE_SECRET.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
        result = {}

        def wake():
            result["status"] = http.post("/norns/wake", content=body,
                                         headers={"X-Norns-Signature": f"t={t},v1={good}"}).status_code

        th = threading.Thread(target=wake)
        th.start()
        assert inc.requested.wait(5)  # the wake asked for an incarnation
        th.join(0.5)
        assert th.is_alive()  # and is held open while it "serves"
        inc.finish()
        th.join(5)
        assert result["status"] == 200
    finally:
        srv.shutdown()
