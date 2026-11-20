"""What a group can see of its own setup: the sources connected to it, by
label. Read-only, and the first tool bound to the run's group rather than
to an argument (`groupctx`)."""

from __future__ import annotations

from fulla import groupctx, runctx
from fulla.tooldef import tool


@tool
def group_sources() -> str:
    """What is connected to this chat: the repositories, pages, teams and projects the owner gave
    this group, by label. In a direct chat there are none. Nothing outside this list is reachable
    from here, whoever asks."""
    if runctx.current_address() is None:
        return groupctx.NO_GROUP
    record = groupctx.current()
    if record is None:
        return "Nothing is connected to a direct chat; sources belong to groups."
    if not record.sources:
        return f"Nothing is connected to {record.name} yet."
    return f"Connected to {record.name}: " + "; ".join(s.shown for s in record.sources) + "."
