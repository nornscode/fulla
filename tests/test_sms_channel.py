"""What SMS asks of the channel: a messenger without buttons or quoting, so a
bare label is the button. The rule is the transport flag's, so Signal's
fakes stand in."""

from tests.test_channel import REMINDER, event, make_two, on_signal, on_telegram


def test_a_bare_label_answers_the_open_question_without_quoting(tmp_path):
    ch, _tg, sg, client = make_two(tmp_path, primary="signal")
    ch.handle_event(event("run.waiting", 42, agent_id=REMINDER, question="Did you call the dentist?",
                          tool_call_id="tc42"))
    assert sg.sent[-1][0] == "Did you call the dentist?"
    ch.handle(on_signal("Snooze 1h.", message_id=1))
    assert client.replies == [(42, "snooze 1 hour")] and client.messages == []  # not chat
    assert ch.state.message_runs == {}
    client.next_run = 10
    ch.handle(on_signal("done", message_id=2))  # nothing open now: a word to the manager
    assert [m[1] for m in client.messages] == ["done"] and client.replies == [(42, "snooze 1 hour")]


def test_a_bare_label_picks_the_newest_question_and_telegram_keeps_its_buttons(tmp_path):
    ch, _tg, _sg, client = make_two(tmp_path, primary="signal")
    ch.handle_event(event("run.waiting", 42, agent_id=REMINDER, question="Starter?", tool_call_id="tc42"))
    ch.handle_event(event("run.waiting", 5, agent_id=REMINDER, question="Dentist?", tool_call_id="tc5"))
    ch.handle(on_signal("Drop it", message_id=1))
    assert client.replies == [(5, "drop")]
    ch.handle(on_telegram("done", message_id=300))  # on Telegram a bare word is chat; the buttons are there
    assert client.replies == [(5, "drop")] and [m[1] for m in client.messages] == ["done"]


def test_a_sentence_that_merely_contains_a_label_is_chat(tmp_path):
    ch, _tg, _sg, client = make_two(tmp_path, primary="signal")
    ch.handle_event(event("run.waiting", 42, agent_id=REMINDER, question="Starter?", tool_call_id="tc42"))
    ch.handle(on_signal("done with the starter, now book the gym", message_id=1))
    assert client.replies == [] and len(client.messages) == 1
