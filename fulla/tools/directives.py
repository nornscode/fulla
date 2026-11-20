"""Standing instructions, changed from chat and pushed into the prompt at once."""

# Module state is set by init() before any tool runs; it is None only before then.
# pyright: reportOptionalMemberAccess=false, reportOptionalCall=false

from __future__ import annotations

import logging
from collections.abc import Callable

from fulla.directives import DirectiveStore
from fulla.tooldef import tool

logger = logging.getLogger("fulla.directives")

_store: DirectiveStore | None = None
_push: Callable[[], None] | None = None


def init(*, store: DirectiveStore, push_prompt: Callable[[], None]):
    """`push_prompt` re-renders the manager's system prompt and sends it to
    Norns, so a change is live on the next turn rather than the next deploy."""
    global _store, _push
    _store = store
    _push = push_prompt


def _pushed(note: str) -> str:
    try:
        _push()
    except Exception as e:
        logger.exception("pushing the prompt failed")
        return f"{note} (saved, but the prompt update failed: {e}; it will apply on the next restart)"
    return note


@tool(side_effect=True)
def set_directive(text: str) -> str:
    """Add a standing instruction about how to behave from now on, in the owner's words.
    It is placed in your system prompt immediately and stays until dropped."""
    d = _store.add(text)
    return _pushed(f"Standing instruction #{d.id} set: {d.text}")


@tool(side_effect=True)
def drop_directive(directive_id: int) -> str:
    """Remove a standing instruction by its number. To change one, drop it and set the new wording."""
    if not _store.remove(directive_id):
        return f"No standing instruction #{directive_id}."
    return _pushed(f"Dropped standing instruction #{directive_id}.")


@tool
def list_directives() -> str:
    """The standing instructions currently in force, numbered."""
    return _store.render()
