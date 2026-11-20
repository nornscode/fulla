"""Remember, recall, forget."""

# Module state is set by init() before any tool runs; it is None only before then.
# pyright: reportOptionalMemberAccess=false

from __future__ import annotations

from fulla.facts import FactStore
from fulla.tooldef import tool

_store: FactStore | None = None


def init(*, store: FactStore):
    global _store
    _store = store


@tool(side_effect=True)
def remember(fact: str) -> str:
    """Store one fact the owner told you: a person, a date, a preference, a plan. Keep their words."""
    f = _store.add(fact)
    return f"Remembered #{f.id}: {f.text}"


@tool
def recall(query: str = "") -> str:
    """Search stored facts. Every word in the query must match. Empty query lists everything."""
    found = _store.search(query)
    if not found:
        return "Nothing stored matches."
    return "\n".join(f"#{f.id} {f.text}" for f in found)


@tool(side_effect=True)
def forget(fact_id: int) -> str:
    """Delete a stored fact by its id."""
    return f"Forgot #{fact_id}." if _store.remove(fact_id) else f"No fact #{fact_id}."
