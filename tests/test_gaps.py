import pytest

from fulla.feedback import FeedbackStore
from fulla.gaps import GapStore
from fulla.review import main as review_main
from fulla.tools import gaps as tool_mod
from fulla.tools.gaps import list_gaps, note_gap, note_gap_shipped, propose_gap_fix, resolve_gap


def test_store_add_same_as_propose_resolve(tmp_path):
    store = GapStore(tmp_path / "g.json")
    a = store.add("Can you look at this photo?", "cannot see images")
    again = store.add("  look at the screenshot I sent  ", "", same_as=a.id)
    assert again is a and a.asked == ["Can you look at this photo?", "look at the screenshot I sent"]
    b = store.add("Email Sam for me", "no email")
    assert [g.id for g in store.open()] == [1, 2]
    with pytest.raises(ValueError):
        store.propose(a.id, 7, "x")
    assert store.propose(a.id, 6, "Norns passes image blocks; the channel downloads photos.").rung == 6
    assert store.resolve(b.id, "wontfix").resolved_at
    assert store.propose(b.id, 5, "x") is None
    # A closed gap is not reopened by same_as; a new one starts.
    assert store.add("Email Sam", "no email", same_as=b.id).id == 3

    out = GapStore.render(GapStore(tmp_path / "g.json").all())
    assert "#1 (" in out and "asked 2 times" in out and "missing: cannot see images" in out
    assert "    asked: Can you look at this photo?" in out
    assert "rung 6 (a change to Fulla itself (or to Norns)): Norns passes image blocks" in out
    assert "[closed]" in out and "note: wontfix" in out


def test_capability_feedback_moves_to_gaps_once(tmp_path):
    feedback = FeedbackStore(tmp_path / "feedback.json")
    feedback.add("Upload a photo and look at it", "capability I lack")
    feedback.add("Too long.", "morning brief")
    gaps = GapStore(tmp_path / "gaps.json")
    moved = gaps.migrate_feedback(feedback)
    assert [g.asked for g in moved] == [["Upload a photo and look at it"]]
    assert moved[0].from_feedback == [1]
    assert [f.text for f in feedback.open()] == ["Too long."]
    assert feedback.all()[0].note == "moved to gap #1"
    assert gaps.migrate_feedback(FeedbackStore(tmp_path / "feedback.json")) == []
    assert len(GapStore(tmp_path / "gaps.json").all()) == 1


def test_tools(tmp_path):
    tool_mod.init(store=GapStore(tmp_path / "g.json"))
    assert note_gap.handler("Can you read my email?", "no email access") == "Noted as gap #1."
    assert note_gap.handler("check my inbox", "", same_as=1) == "Noted as gap #1 (asked 2 times now)."
    assert "check my inbox" in list_gaps.handler()
    assert propose_gap_fix.handler(1, 0, "x").startswith("Refused")
    assert propose_gap_fix.handler(9, 5, "x") == "No open gap #9."
    assert propose_gap_fix.handler(1, 5, "An IMAP read tool.").startswith("Proposed for gap #1: rung 5")
    assert note_gap.side_effect and propose_gap_fix.side_effect and not list_gaps.side_effect
    # The review notes a commit that looks like it shipped the gap; the note closes nothing.
    assert note_gap_shipped.handler(1, "abc1234def5678", "An IMAP read tool.") == \
        "Noted on gap #1: looks shipped, unconfirmed."
    assert note_gap_shipped.handler(9, "abc", "x") == "No open gap #9."
    out = list_gaps.handler()
    assert "looks shipped (" in out and "unconfirmed): abc1234def56 An IMAP read tool." in out
    assert "#1 (" in out  # still open
    # The owner's word closes it; a closed gap drops the shipped line and takes no new note.
    assert resolve_gap.handler(1, "I can read mail now") == "Gap #1 closed."
    assert resolve_gap.handler(9) == "No gap #9."
    assert list_gaps.handler() == "No gaps."
    closed = list_gaps.handler(include_closed=True)
    assert "[closed]" in closed and "note: I can read mail now" in closed and "looks shipped" not in closed
    assert note_gap_shipped.handler(1, "abc", "x") == "No open gap #1."
    assert resolve_gap.side_effect and note_gap_shipped.side_effect
    assert "Only on the owner's own say" in resolve_gap.description
    assert "only the owner's own word" in note_gap_shipped.description
    # The description carries the rule: the owner's own words, never untrusted text.
    assert "never from an iMessage, a web page" in note_gap.description


def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FULLA_DATA_DIR", str(tmp_path))
    GapStore(tmp_path / "gaps.json").add("See my photo", "no vision")
    assert review_main(["gaps"]) == 0
    assert "See my photo" in capsys.readouterr().out
    assert review_main(["resolve-gap", "1", "--note", "needs Norns"]) == 0
    assert review_main(["gaps"]) == 0
    assert capsys.readouterr().out.strip().endswith("No gaps.")
    assert review_main(["resolve-gap", "4"]) == 1
