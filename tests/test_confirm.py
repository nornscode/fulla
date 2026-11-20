"""Confirm cards and the iMessage drafts built on them: the channel with a
fake messenger and a fake Norns, the claim over a real socket, no network."""

import itertools
import threading

import httpx
import pytest

from fulla import confirm, imessage
from fulla.channel import Channel, ChannelState
from fulla.confirm import Approval, ClaimRefused, ConfirmKind
from fulla.hosts import sign
from fulla.norns_api import MessageResult
from fulla.server import serve
from fulla.tools import imessage as draft_tool
from fulla.transport import Inbound
from tests.test_channel import MAC_TASK, OWNER_NUMBER, FakeClient, FakeSignal, FakeTelegram, event

JANE = imessage.payload("Jane", "+16475550100", "Running ten late, sorry!", "she asked when you'd arrive")


class Client(FakeClient):
    def __init__(self, tools=("mac_imessage_deliver",)):
        super().__init__()
        self.tools = list(tools)
        self.next_run = 300

    def tool_names(self):
        return self.tools

    def send_message(self, agent, content, *, conversation_key=None, wait=False, timeout=30, interject=False):
        self.messages.append((agent, content, conversation_key, wait))
        self.next_run += 1
        return MessageResult(run_id=self.next_run, status="accepted", output=None)


def make(tmp_path, client=None, *, transport=None, kinds=None, **extra):
    """The channel on one messenger, Telegram unless `transport` is given."""
    client = client or Client()
    msgr = transport or FakeTelegram()
    owner = 7 if msgr.name == "telegram" else OWNER_NUMBER
    ch = Channel({msgr.name: msgr}, client, ChannelState(tmp_path / "channel.json"),
                 owners={msgr.name: owner}, primary=msgr.name, conversation_key="telegram:7",
                 typing_interval=0, confirm_kinds=kinds or [imessage.kind(client)], **extra)
    return ch, msgr, client


def _owner(ch):
    """The transport and chat of the channel's one owner address."""
    address, chat = next(iter(ch.owner_addresses.items()))
    return address.split(":", 1)[0], chat


def tap(ch, conf, action, digest=None):
    data = f"k:{conf.id}:{(digest or conf.digest)[:confirm.DIGEST_SHOWN]}:{action}"
    transport, chat = _owner(ch)
    ch.handle(Inbound(kind="callback", transport=transport, user_id=chat, chat_id=chat, data=data,
                      callback_id="cb", message_id=conf.message_id))


_ids = itertools.count(1)


def text(ch, words, reply_to=None):
    # Each message its own id: the channel ignores one it has already handled.
    transport, chat = _owner(ch)
    ch.handle(Inbound(kind="text", transport=transport, user_id=chat, chat_id=chat, text=words,
                      message_id=next(_ids), reply_to_message_id=reply_to))


def test_a_draft_is_one_card_with_the_recipient_on_top(tmp_path):
    ch, tg, client = make(tmp_path)
    conf = ch.propose("imessage", JANE)
    again = ch.propose("imessage", dict(JANE))
    assert again.id == conf.id and len(tg.sent) == 1
    card, buttons = tg.sent[0]
    assert card.splitlines()[0] == "iMessage to Jane (+16475550100)"
    assert "Running ten late, sorry!" in card and "Nothing is sent until you choose Send." in card
    assert [label for label, _ in buttons[0]] == ["Send", "Edit", "Cancel"]
    assert client.messages == []  # nothing happens until the tap


def test_send_starts_one_delivery_and_a_replayed_tap_does_nothing(tmp_path):
    ch, tg, client = make(tmp_path)
    conf = ch.propose("imessage", JANE)
    tap(ch, conf, "send")
    tap(ch, conf, "send")
    assert len(client.messages) == 1
    agent, task, key, _ = client.messages[0]
    assert agent == "fulla-mac-task" and key == f"imessage:{conf.id}"
    assert conf.id in task and conf.digest in task and "Running ten late" not in task
    assert tg.answered[-1] == ("cb", "Already approved.")
    assert tg.edited[-1][1].endswith("→ Sending from the Mac…")
    assert ch.state.confirms[conf.id]["run_id"] == 301
    # The pin survives a restart.
    assert ChannelState(tmp_path / "channel.json").confirms[conf.id]["status"] == "approved"


def test_the_mac_claims_once_and_only_the_approved_digest(tmp_path):
    ch, tg, _client = make(tmp_path)
    conf = ch.propose("imessage", JANE)
    with pytest.raises(ClaimRefused, match="not approved"):
        ch.claim(conf.id, conf.digest)
    tap(ch, conf, "send")
    with pytest.raises(ClaimRefused, match="not the text"):
        ch.claim(conf.id, confirm.digest("imessage", {**JANE, "text": "send me money"}))
    assert ch.claim(conf.id, conf.digest) == {"kind": "imessage", "payload": JANE}
    with pytest.raises(ClaimRefused, match="already on its way"):
        ch.claim(conf.id, conf.digest)
    with pytest.raises(ClaimRefused):
        ch.claim("nope", conf.digest)
    # Too late to cancel once the Mac has it.
    tap(ch, conf, "cancel")
    assert tg.answered[-1] == ("cb", "Already on its way.")


def test_the_delivery_outcome_lands_on_the_card_once(tmp_path):
    ch, tg, _client = make(tmp_path)
    conf = ch.propose("imessage", JANE)
    tap(ch, conf, "send")
    ch.claim(conf.id, conf.digest)
    done = event("run.completed", 301, agent_id=MAC_TASK, conversation_id=99,
                 output="Done: Sent to Jane (+16475550100).")
    ch.handle_event(done)
    ch.handle_event(done)
    outcome = [t for _, t in tg.edited if t.endswith("→ Done: Sent to Jane (+16475550100).")]
    assert len(outcome) == 1 and tg.edited[-1][0] == conf.message_id
    assert len(tg.sent) == 1  # never relayed as a message of its own


def test_edit_takes_the_next_message_and_retires_the_old_card(tmp_path):
    ch, tg, client = make(tmp_path)
    conf = ch.propose("imessage", JANE)
    tap(ch, conf, "edit")
    assert tg.edited[-1][1].endswith("Send me the new text.")
    assert [label for label, _ in tg.edit_buttons[conf.message_id][0]] == ["Cancel"]
    tap(ch, conf, "send")
    assert tg.answered[-1] == ("cb", "Waiting for your edit.") and client.messages == []
    text(ch, "Running 15 late!")
    assert client.messages == []  # the edit is not chat
    new = ch._get_confirm(conf.id)
    assert new.payload["text"] == "Running 15 late!" and new.digest != conf.digest
    assert new.message_id != conf.message_id and tg.sent[-1][0].splitlines()[2] == "Running 15 late!"
    assert any(mid == conf.message_id and "Edited; the new draft is below." in t for mid, t in tg.edited)
    # A tap on the old card is stale; the new one sends the new text.
    tap(ch, conf, "send")
    assert tg.answered[-1] == ("cb", "That draft has changed; use the newest card.")
    tap(ch, new, "send")
    assert ch.claim(conf.id, new.digest)["payload"]["text"] == "Running 15 late!"
    # Chat is chat again.
    text(ch, "thanks")
    assert client.messages[-1][1] == "thanks"


def test_a_reply_to_the_card_is_an_edit(tmp_path):
    ch, _tg, client = make(tmp_path)
    conf = ch.propose("imessage", JANE)
    text(ch, "On my way", reply_to=conf.message_id)
    assert ch._get_confirm(conf.id).payload["text"] == "On my way" and client.messages == []


def test_cancel_drops_it_and_an_approved_one_cannot_be_claimed(tmp_path):
    ch, tg, client = make(tmp_path)
    conf = ch.propose("imessage", JANE)
    tap(ch, conf, "cancel")
    tap(ch, conf, "send")
    assert client.messages == [] and tg.answered[-1] == ("cb", "Already cancelled.")
    other = ch.propose("imessage", {**JANE, "text": "Other"})
    tap(ch, other, "send")
    tap(ch, other, "cancel")
    assert client.cancelled == [301]
    with pytest.raises(ClaimRefused, match="cancelled"):
        ch.claim(other.id, other.digest)
    # The cancelled delivery run's end does not overwrite "Cancelled."
    ch.handle_event(event("run.completed", 301, agent_id=MAC_TASK, output="Could not: Refused"))
    assert tg.edited[-1][1].endswith("→ Cancelled.")


def test_mac_off_says_so_and_a_failed_start_can_be_retried(tmp_path):
    client = Client(tools=())
    ch, tg, _ = make(tmp_path, client)
    conf = ch.propose("imessage", JANE)
    real = client.send_message
    client.send_message = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("norns down"))
    tap(ch, conf, "send")
    assert ch._get_confirm(conf.id).status == "pending" and "couldn't start it" in tg.edited[-1][1]
    client.send_message = real
    tap(ch, conf, "send")
    assert tg.edited[-1][1].endswith("→ The Mac is off; it goes when the Mac is back.")


def test_reconcile_finishes_a_card_whose_run_ended_while_asleep(tmp_path):
    from tests.test_channel import run
    ch, tg, client = make(tmp_path)
    conf = ch.propose("imessage", JANE)
    tap(ch, conf, "send")
    client.runs = [run(301, "completed", agent_id=MAC_TASK, output="Done: Sent to Jane.")]
    ch.reconcile()
    assert tg.edited[-1][1].endswith("→ Done: Sent to Jane.")


def test_a_kind_that_finishes_at_once_is_done_on_approval(tmp_path):
    done = []
    merge = ConfirmKind(name="merge", render=lambda p: f"Merge {p['pr']}?",
                        approve=lambda c: done.append(c.payload) or Approval(note="Merged."))
    ch, tg, _client = make(tmp_path, kinds=[merge])
    conf = ch.propose("merge", {"pr": 31})
    assert [label for label, _ in tg.sent[0][1][0]] == ["Send", "Cancel"]  # no revise, no Edit
    tap(ch, conf, "send")
    tap(ch, conf, "send")
    assert done == [{"pr": 31}] and ch._get_confirm(conf.id).status == "done"
    with pytest.raises(ClaimRefused):  # not a claimable kind
        ch.claim(conf.id, conf.digest)


# --- the draft tool ------------------------------------------------------------

def test_draft_imessage_proposes_only_to_allowed_people():
    proposed = []
    draft_tool.init(propose=lambda kind, p: proposed.append((kind, p)),
                    allow=lambda: {"Jane": "+16475550100", "Mom": "mom@icloud.com"})
    out = draft_tool.draft_imessage.handler("jane", " hi ", "why not")
    assert out.startswith("Drafted for Jane (+16475550100)") and "Nothing is sent" in out
    assert proposed == [("imessage", imessage.payload("Jane", "+16475550100", "hi", "why not"))]
    assert draft_tool.draft_imessage.handler("+1 647 555 0199", "hi").startswith("Refused: +1 647")
    assert draft_tool.draft_imessage.handler("Mom", "  ").startswith("Refused: the message is empty")
    draft_tool.init(propose=lambda kind, p: proposed.append(p), allow=dict)
    assert draft_tool.draft_imessage.handler("Jane", "hi").startswith("Refused: no one is allowed")
    assert len(proposed) == 1


# --- the claim over HTTP ---------------------------------------------------------

KEY = "norns-api-key"


@pytest.fixture
def fly(tmp_path):
    ch, _tg, _client = make(tmp_path)
    srv = serve(ch, port=0, telegram_secret="t", norns_secret="n", host_secret=KEY)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield ch, f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_claim_over_http_is_signed_and_one_shot(fly):
    ch, url = fly
    conf = ch.propose("imessage", JANE)
    with pytest.raises(ClaimRefused, match="not approved"):
        confirm.claim(url, KEY, conf.id, conf.digest)
    tap(ch, conf, "send")
    with pytest.raises(ClaimRefused, match="bad signature"):
        confirm.claim(url, "wrong-key", conf.id, conf.digest)
    assert confirm.claim(url, KEY, conf.id, conf.digest) == JANE
    with pytest.raises(ClaimRefused, match="already"):
        confirm.claim(url, KEY, conf.id, conf.digest)
    body = b"not json"
    resp = httpx.post(url + "/confirm/claim", content=body, headers={"X-Norns-Signature": sign(KEY, body)})
    assert resp.status_code == 400


def test_claim_checks_the_payload_against_the_digest(monkeypatch):
    def tampered(url, *, content, headers, timeout):
        return httpx.Response(200, json={"kind": "imessage", "payload": {**JANE, "text": "evil"}},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(confirm.httpx, "post", tampered)
    with pytest.raises(ClaimRefused, match="does not match"):
        confirm.claim("https://fulla", KEY, "abc", confirm.digest("imessage", JANE))


# ------------------------------------------------- Signal: no buttons to tap


def make_signal(tmp_path, **extra):
    return make(tmp_path, transport=FakeSignal(), **extra)


def react(ch, mid, emoji):
    transport, chat = _owner(ch)
    ch.handle(Inbound(kind="reaction", transport=transport, user_id=chat, chat_id=chat, text=emoji,
                      message_id=mid, date=next(_ids)))


def test_a_quoted_reply_naming_a_button_is_that_tap_and_is_one_shot(tmp_path):
    ch, sig, client = make_signal(tmp_path)
    conf = ch.propose("imessage", JANE)
    text(ch, "Send", reply_to=conf.message_id)
    text(ch, "send.", reply_to=conf.message_id)
    assert len(client.messages) == 1 and client.messages[0][0] == "fulla-mac-task"
    assert ch.state.confirms[conf.id]["status"] == "approved"
    assert sig.answered == []  # no button to acknowledge
    assert sig.sent[-1] == ("Already approved.", None)  # said as a message instead
    assert ch.claim(conf.id, conf.digest)["payload"]["text"] == JANE["text"]


def test_a_reaction_on_a_card_cancels_but_never_sends(tmp_path):
    ch, sig, client = make_signal(tmp_path)
    conf = ch.propose("imessage", JANE)
    react(ch, conf.message_id, "👍")
    assert ch.state.confirms[conf.id]["status"] == "pending" and client.messages == []
    assert sig.sent[-1] == ("Reactions don't send. Reply Send to the card to send it.", None)
    other = ch.propose("imessage", imessage.payload("Jane", "+16475550100", "On my way", ""))
    react(ch, other.message_id, "❌")
    assert ch.state.confirms[other.id]["status"] == "cancelled"
    react(ch, other.message_id, "😂")  # anything else on a card means nothing
    assert client.messages == []


def test_edit_by_reply_then_send_the_new_card_and_the_old_one_is_stale(tmp_path):
    ch, sig, client = make_signal(tmp_path)
    conf = ch.propose("imessage", JANE)
    text(ch, "Edit", reply_to=conf.message_id)
    assert ch.state.confirms[conf.id]["status"] == "editing"
    assert [label for label, _ in sig.edit_buttons[conf.message_id][0]] == ["Cancel"]
    text(ch, "Running 15 late!")  # the next message is the new text
    new = ch._get_confirm(conf.id)
    assert new.payload["text"] == "Running 15 late!" and new.message_id != conf.message_id
    assert client.messages == []
    text(ch, "Send", reply_to=conf.message_id)
    assert sig.sent[-1] == ("That draft has changed; use the newest card.", None)
    assert client.messages == []
    text(ch, "Send", reply_to=new.message_id)
    assert ch.claim(conf.id, new.digest)["payload"]["text"] == "Running 15 late!"


def test_any_other_quoted_reply_to_an_undecided_card_is_its_new_text(tmp_path):
    ch, _sig, client = make_signal(tmp_path)
    conf = ch.propose("imessage", JANE)
    text(ch, "Running 20 late, sorry", reply_to=conf.message_id)
    assert ch._get_confirm(conf.id).payload["text"] == "Running 20 late, sorry"
    assert client.messages == []


# ------------------------------------------- only the owner, one to one, decides

SAM = "+16045550111"


def make_world(tmp_path, primary="signal"):
    """Both messengers, the owner on each, Sam a listed friend, a Friends
    group the owner allowed."""
    from fulla.people import PeopleStore
    tg, sg, client = FakeTelegram(), FakeSignal(), Client()
    people = PeopleStore(tmp_path / "people.json")
    people.allow_person("Sam", {"signal": SAM, "telegram": "55"})
    people.remember_group("signal:group:abc", "Friends")
    people.allow_group("signal:group:abc")
    ch = Channel({"telegram": tg, "signal": sg}, client, ChannelState(tmp_path / "channel.json"),
                 owners={"telegram": 7, "signal": OWNER_NUMBER}, primary=primary, conversation_key="telegram:7",
                 people=people, owner_name="Robin", typing_interval=0, confirm_kinds=[imessage.kind(client)])
    return ch, tg, sg, client


def on_signal(user, words, *, reply_to=None, group=False):
    chat = "group:abc" if group else user
    return Inbound(kind="text", transport="signal", user_id=user, chat_id=chat, group=group, text=words,
                   message_id=next(_ids), reply_to_message_id=reply_to)


def deliveries(client):
    return [m for m in client.messages if m[0] == "fulla-mac-task"]


def test_a_card_goes_to_the_primary_or_where_the_owner_is_talking(tmp_path):
    ch, tg, sg, _client = make_world(tmp_path, primary="signal")
    first = ch.propose("imessage", JANE)  # nobody talking: a scheduled turn's draft
    assert first.address == f"signal:{OWNER_NUMBER}" and sg.sent[-1][0].startswith("iMessage to Jane")
    ch.handle(Inbound(kind="text", transport="telegram", user_id=7, chat_id=7, text="tell Jane I'm late",
                      message_id=next(_ids)))
    second = ch.propose("imessage", {**JANE, "text": "Late!"})
    assert second.address == "telegram:7" and tg.sent[-1][0].startswith("iMessage to Jane")


def test_a_friends_quoted_send_or_tap_or_reaction_moves_nothing(tmp_path):
    ch, tg, _sg, client = make_world(tmp_path)
    conf = ch.propose("imessage", JANE)
    ch.handle(on_signal(SAM, "Send", reply_to=conf.message_id))  # quoting the card's timestamp
    ch.handle(on_signal(SAM, "cancel", reply_to=conf.message_id))
    ch.handle(Inbound(kind="reaction", transport="signal", user_id=SAM, chat_id=SAM, text="👍",
                      message_id=conf.message_id, date=1))
    # on Telegram, a tap with the card's own data (a forwarded card, say)
    data = f"k:{conf.id}:{conf.digest[:confirm.DIGEST_SHOWN]}:send"
    ch.handle(Inbound(kind="callback", transport="telegram", user_id=55, chat_id=55, data=data,
                      callback_id="cb", message_id=conf.message_id))
    assert ch._get_confirm(conf.id).status == "pending" and deliveries(client) == []
    assert tg.answered == [("cb", None)]
    with pytest.raises(ClaimRefused, match="not approved"):
        ch.claim(conf.id, conf.digest)


def test_the_owner_in_a_group_cannot_approve_a_card(tmp_path):
    ch, _tg, _sg, client = make_world(tmp_path)
    conf = ch.propose("imessage", JANE)
    ch.handle(on_signal(OWNER_NUMBER, "Fulla, send", reply_to=conf.message_id, group=True))
    ch.handle(on_signal(OWNER_NUMBER, "Send", reply_to=conf.message_id, group=True))
    ch.handle(on_signal(SAM, "Fulla Send", reply_to=conf.message_id, group=True))
    ch.handle(Inbound(kind="reaction", transport="signal", user_id=OWNER_NUMBER, chat_id="group:abc", group=True,
                      text="👍", message_id=conf.message_id, date=2))
    assert ch._get_confirm(conf.id).status == "pending" and deliveries(client) == []


def test_the_owner_on_the_other_messenger_does_not_reach_the_card(tmp_path):
    ch, _tg, _sg, client = make_world(tmp_path)
    conf = ch.propose("imessage", JANE)  # on Signal
    ch.handle(Inbound(kind="text", transport="telegram", user_id=7, chat_id=7, text="Send",
                      message_id=next(_ids), reply_to_message_id=conf.message_id))
    assert ch._get_confirm(conf.id).status == "pending" and deliveries(client) == []


def test_the_card_checks_who_even_if_the_channel_let_someone_through(tmp_path):
    ch, _tg, _sg, client = make_world(tmp_path)
    conf = ch.propose("imessage", JANE)
    not_owner = Inbound(kind="reaction", transport="signal", user_id=SAM, chat_id=OWNER_NUMBER, text="👍",
                        message_id=conf.message_id, date=3)
    assert ch._card_reaction(not_owner, f"signal:{OWNER_NUMBER}", "👍") is True
    quoted = Inbound(kind="text", transport="signal", user_id=SAM, chat_id=OWNER_NUMBER, text="Send",
                     message_id=next(_ids), reply_to_message_id=conf.message_id)
    assert ch._card_reply(quoted, f"signal:{OWNER_NUMBER}", "Send") is True
    assert ch._edit_target(quoted, f"signal:{OWNER_NUMBER}", "Send") is None
    assert ch._get_confirm(conf.id).status == "pending" and deliveries(client) == []


# ------------------------------------------------- cancel words and cards


def test_a_quoted_cancel_word_on_a_card_cancels_the_card_not_the_run(tmp_path):
    ch, _sig, client = make_signal(tmp_path)
    text(ch, "draft Jane a note")  # a manager run in flight here
    run = client.next_run
    conf = ch.propose("imessage", JANE)
    text(ch, "nvm", reply_to=conf.message_id)
    assert ch._get_confirm(conf.id).status == "cancelled"
    assert client.cancelled == [] and str(run) in ch.state.working
    other = ch.propose("imessage", {**JANE, "text": "Other"})
    text(ch, "Cancel", reply_to=other.message_id)
    assert ch._get_confirm(other.id).status == "cancelled" and client.cancelled == []


def test_a_cancel_word_while_editing_stops_the_run_and_the_edit_still_waits(tmp_path):
    ch, _sig, client = make_signal(tmp_path)
    conf = ch.propose("imessage", JANE)
    text(ch, "Edit", reply_to=conf.message_id)
    ch.state.working["400"] = f"signal:{OWNER_NUMBER}#-"  # a run of the owner's in flight here
    text(ch, "wait")
    assert client.cancelled == [400]
    assert ch._get_confirm(conf.id).status == "editing"  # "wait" was not the new text
    text(ch, "Running 20 late")
    assert ch._get_confirm(conf.id).payload["text"] == "Running 20 late"


def test_a_cancel_word_while_editing_with_nothing_running_drops_the_edit(tmp_path):
    ch, sig, client = make_signal(tmp_path)
    conf = ch.propose("imessage", JANE)
    text(ch, "Edit", reply_to=conf.message_id)
    text(ch, "never mind")
    got = ch._get_confirm(conf.id)
    assert got.status == "pending" and got.payload == JANE and ch.state.editing is None
    assert client.messages == []  # not chat either
    assert sig.edited[-1][1].endswith("Edit dropped; the draft is as it was.")
    assert [label for label, _ in sig.edit_buttons[conf.message_id][0]] == ["Send", "Edit", "Cancel"]
    text(ch, "Send", reply_to=conf.message_id)
    assert ch._get_confirm(conf.id).status == "approved"


def test_an_edit_waits_only_at_the_cards_address(tmp_path):
    ch, _tg, _sg, client = make_world(tmp_path, primary="signal")
    conf = ch.propose("imessage", JANE)  # on Signal
    ch.handle(on_signal(OWNER_NUMBER, "Edit", reply_to=conf.message_id))
    ch.handle(Inbound(kind="text", transport="telegram", user_id=7, chat_id=7, text="what's on today?",
                      message_id=next(_ids)))
    assert client.messages[-1][1] == "what's on today?"  # chat on Telegram, not the edit
    assert ch._get_confirm(conf.id).status == "editing"


def test_an_answer_to_a_question_while_editing_answers_the_question(tmp_path):
    ch, _sig, client = make_signal(tmp_path)
    conf = ch.propose("imessage", JANE)
    text(ch, "Edit", reply_to=conf.message_id)
    ch.handle_event(event("run.waiting", 9, question="Which Jane?", tool_call_id="tc9", conversation_id=1))
    question = next(iter(ch.state.message_runs))
    text(ch, "Jane Doe", reply_to=int(question.rsplit("#", 1)[1]))
    assert client.replies == [(9, "Jane Doe")]
    assert ch._get_confirm(conf.id).status == "editing"
