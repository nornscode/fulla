"""A group's memory: facts its members ask Fulla to keep, in that group's
own `facts.json`, through the injection screen, and read back inside the
untrusted marker. Bound to the run's group (`groupctx`); a direct chat has
no memory."""

# Module state is set by init() before any tool runs; it is None only before then.
# pyright: reportOptionalMemberAccess=false

from __future__ import annotations

from fulla import groupctx
from fulla.groups import GroupStore
from fulla.tooldef import tool
from fulla.untrusted import untrusted

_store: GroupStore | None = None
RECALL_MAX = 20


def init(*, store: GroupStore | None):
    global _store
    _store = store


@tool(side_effect=True)
def group_remember(text: str) -> str:
    """Keep a fact for this group, in the words it was said: who is doing what, a decision, a
    date. Not instructions to yourself, and never anything about the owner's life."""
    try:
        record = groupctx.require()
    except groupctx.Refused as e:
        return str(e)
    reason = _store.remember(record.id, text)
    if reason is not None:
        return f"Not kept: that reads like {reason}, not a fact. Say it plainly and I'll keep it."
    return "Kept."


@tool
def group_recall(query: str = "") -> str:
    """What this group has asked you to remember, newest last; `query` keeps only facts holding
    all its words. What comes back is what members said, marked as untrusted text."""
    try:
        record = groupctx.require()
    except groupctx.Refused as e:
        return str(e)
    facts = _store.facts(record.id)
    words = [w for w in query.lower().split() if w]
    if words:
        facts = [f for f in facts if all(w in f["text"].lower() for w in words)]
    if not facts:
        return "Nothing remembered here" + (f" about {query!r}." if query.strip() else " yet.")
    lines = [f"- {f['text']} ({str(f.get('at', ''))[:10]})" for f in facts[-RECALL_MAX:]]
    return untrusted("group memory", f"members of {record.name}", "\n".join(lines))
