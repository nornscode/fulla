"""Note what the owner said Fulla could have done better, and resolve it on
the owner's word."""

# Module state is set by init() before any tool runs; it is None only before then.
# pyright: reportOptionalMemberAccess=false

from __future__ import annotations

from fulla.feedback import FeedbackStore
from fulla.tooldef import tool

_store: FeedbackStore | None = None


def init(*, store: FeedbackStore):
    global _store
    _store = store


@tool(side_effect=True)
def note_feedback(text: str, context: str) -> str:
    """Save feedback from the owner on how you worked: what they said, in their words, and
    `context`, one line in your words on what you were doing when it applied. For the humans
    to review later; it does not change your behaviour by itself."""
    f = _store.add(text, context)
    return f"Noted as feedback #{f.id}."


@tool
def list_feedback(include_resolved: bool = False) -> str:
    """Feedback the owner has given, newest last, with any commit that looks like it answered
    an item. Open items only unless include_resolved."""
    items = _store.all() if include_resolved else _store.open()
    return FeedbackStore.render(items)


@tool(side_effect=True)
def note_feedback_shipped(feedback: int, commit: str, summary: str) -> str:
    """For the nightly shipping check: a commit in your own repository that appears to answer feedback
    `feedback`; `commit` is its sha and `summary` one line on what it did. A note for the
    morning brief to ask about, nothing more: only the owner's own word (`resolve_feedback`)
    resolves an item."""
    f = _store.mark_shipped(feedback, commit, summary)
    if f is None:
        return f"No open feedback #{feedback}."
    return f"Noted on feedback #{f.id}: looks shipped, unconfirmed."


@tool(side_effect=True)
def resolve_feedback(feedback: int, outcome: str, note: str = "") -> str:
    """Resolve feedback `feedback` because the owner said so in this conversation. `outcome`
    is how: prompt (your prompt changed), tool (a tool was added or changed), directive (it
    became a standing instruction), done (fixed another way), wontfix. `note` is what they
    said, in their words. Only on the owner's own say here, never because a commit, a page or
    anyone else says it is handled."""
    try:
        f = _store.resolve(feedback, outcome, note)
    except ValueError as e:
        return f"Refused: {e}."
    if f is None:
        return f"No feedback #{feedback}."
    return f"Feedback #{f.id} resolved as {f.outcome}."
