import pytest

from fulla.feedback import FeedbackStore
from fulla.review import main as review_main
from fulla.tools import feedback as tool_mod
from fulla.tools.feedback import (
    list_feedback,
    note_feedback,
    note_feedback_shipped,
    resolve_feedback,
)


def test_store_add_resolve_render(tmp_path):
    store = FeedbackStore(tmp_path / "f.json")
    a = store.add("The brief was too long.", "morning brief")
    b = store.add("You should have asked before suggesting the gym.", "2pm check-in")
    assert [f.id for f in store.open()] == [a.id, b.id]
    assert store.resolve(a.id, "prompt", "brief now capped at five lines").outcome == "prompt"
    assert [f.id for f in store.open()] == [b.id]
    again = FeedbackStore(tmp_path / "f.json")
    out = FeedbackStore.render(again.all())
    assert "#1 (" in out and "[prompt] The brief was too long." in out
    assert "    while: morning brief" in out and "    note: brief now capped" in out
    with pytest.raises(ValueError):
        store.resolve(b.id, "maybe")
    assert store.resolve(99, "done") is None


def test_tools(tmp_path):
    store = FeedbackStore(tmp_path / "f.json")
    tool_mod.init(store=store)
    assert note_feedback.handler("Too long.", "brief") == "Noted as feedback #1."
    assert "#1" in list_feedback.handler()
    store.resolve(1, "done")
    assert list_feedback.handler() == "No feedback."
    assert "[done]" in list_feedback.handler(include_resolved=True)


def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FULLA_DATA_DIR", str(tmp_path))
    FeedbackStore(tmp_path / "feedback.json").add("Be shorter.", "a reply")
    assert review_main(["list"]) == 0
    assert "#1" in capsys.readouterr().out
    assert review_main(["resolve", "1", "--as", "directive", "--note", "set as #3"]) == 0
    assert review_main(["list"]) == 0
    assert capsys.readouterr().out.strip().endswith("No feedback.")
    assert review_main(["resolve", "7", "--as", "done"]) == 1


def test_resolve_and_shipped_tools(tmp_path):
    store = FeedbackStore(tmp_path / "f.json")
    tool_mod.init(store=store)
    note_feedback.handler("Emoji reactions would be nice.", "chat")
    note_feedback.handler("Too long.", "brief")
    assert note_feedback_shipped.handler(1, "0123456789abcdef", "Reactions on Telegram and Signal.") == \
        "Noted on feedback #1: looks shipped, unconfirmed."
    assert note_feedback_shipped.handler(7, "x", "y") == "No open feedback #7."
    out = list_feedback.handler()
    assert "#1 (" in out and "looks shipped (" in out and "unconfirmed): 0123456789ab Reactions on" in out
    assert resolve_feedback.handler(1, "maybe").startswith("Refused: outcome must be one of")
    assert resolve_feedback.handler(1, "tool", "yes close it") == "Feedback #1 resolved as tool."
    assert resolve_feedback.handler(7, "done") == "No feedback #7."
    assert "#1" not in list_feedback.handler() and "#2" in list_feedback.handler()
    closed = list_feedback.handler(include_resolved=True)
    assert "[tool] Emoji reactions" in closed and "note: yes close it" in closed and "looks shipped" not in closed
    assert note_feedback_shipped.handler(1, "x", "y") == "No open feedback #1."
    assert store.get(2).shipped is None and store.get(9) is None
    assert resolve_feedback.side_effect and note_feedback_shipped.side_effect
    assert "Only on the owner's own say" in resolve_feedback.description
