"""`send_message`: a message to the owner now, before the turn ends.

A turn's final reply goes out as one message when the run completes. This
lets the manager send more than one, mid-turn: a report split into one
message per item (one per tmux session), or a line before long work. The
channel delivers it the way it delivers the final reply (`Channel.say`):
to the address the run answers, or the primary one when nobody started it.

Owner-only twice: it is in `manager_tools` and nowhere else, so no other
agent is allowed it, and it refuses unless the run serving it is the
manager's by the agent id core put on the task (`runctx.current_is_manager`),
never by anything the model said. It is withheld from the scheduled turns
that are not delivered (`wake.WITHHELD_ON_QUIET`). A final reply of
"[silent]" drops only that reply; what was already sent stays sent.
"""

from __future__ import annotations

from collections.abc import Callable

from norns import tool

from fulla import runctx

_say: Callable[[int, str], bool] | None = None


def init(*, say: Callable[[int, str], bool] | None):
    global _say
    _say = say


@tool(side_effect=True)
def send_message(text: str) -> str:
    """Send the owner a message right now, before this turn ends, as its own message in
    their chat. Use it to split a report into one message per item (one per tmux session,
    one per repository), or to say something before a long piece of work. Your final reply
    is still sent after it; end with exactly "[silent]" when everything was already sent."""
    text = (text or "").strip()
    if not text:
        return "Refused: the message is empty."
    run_id = runctx.current_run_id()
    if run_id is None or not runctx.current_is_manager():
        return "Refused: only your own turns with the owner can send them a message."
    if _say is None:
        return "Not sent: there is no chat to send it to."
    try:
        fresh = _say(run_id, text)
    except Exception:
        return "Not sent: the message could not be delivered. Put it in your reply instead."
    return "Sent." if fresh else "Already sent; not sent again."
