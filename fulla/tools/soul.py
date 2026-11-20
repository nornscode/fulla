"""Who Fulla is, read and rewritten from chat, and pushed into the prompt at once."""

# Module state is set by init() before any tool runs; it is None only before then.
# pyright: reportOptionalMemberAccess=false, reportOptionalCall=false

from __future__ import annotations

import logging
from collections.abc import Callable

from fulla.soul import Soul
from fulla.tooldef import tool

logger = logging.getLogger("fulla.soul")

_soul: Soul | None = None
_push: Callable[[], None] | None = None


def init(*, soul: Soul, push_prompt: Callable[[], None]):
    """`push_prompt` re-renders the manager's system prompt and sends it to
    Norns, so a change is live on the next turn rather than the next restart."""
    global _soul, _push
    _soul = soul
    _push = push_prompt


@tool
def show_soul() -> str:
    """Who you are, in your own words: the page your system prompt carries. Empty until written."""
    return _soul.text if _soul.written else "(unwritten)"


@tool(side_effect=True)
def write_soul(text: str) -> str:
    """Replace the whole page that says who you are: first person, a page at most,
    written with the owner or revised one line at a time by the nightly review.
    It is placed in your system prompt immediately."""
    try:
        _soul.write(text)
    except ValueError as e:
        return f"Not written: {e}"
    try:
        _push()
    except Exception as e:
        logger.exception("pushing the prompt failed")
        return f"Written (saved, but the prompt update failed: {e}; it will apply on the next restart)"
    return "Written. It is in your prompt from your next reply."
