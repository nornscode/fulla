"""`draft_imessage`: the manager's only way toward an iMessage.

It sends nothing. It hands the draft to the channel, which shows it to the
owner with Send, Edit and Cancel (see imessage.py and confirm.py); only the
owner's tap sends it, from the Mac. The recipient must be in the Mac's
FULLA_IMESSAGE_ALLOW, which the Mac announces with itself (hosts.py).
"""

# Module state is set by init() before any tool runs; it is None only before then.
# pyright: reportOptionalMemberAccess=false, reportOptionalCall=false

from __future__ import annotations

from collections.abc import Callable

from fulla import imessage
from fulla.mac.tools import _normalize_handle, resolve_recipient
from fulla.tooldef import tool

_propose: Callable[[str, dict], object] | None = None
_allow: Callable[[], dict[str, str]] = dict


def init(*, propose: Callable[[str, dict], object], allow: Callable[[], dict[str, str]]):
    global _propose, _allow
    _propose = propose
    _allow = allow


@tool(side_effect=True)
def draft_imessage(to: str, text: str, why: str = "") -> str:
    """Draft an iMessage for the owner to approve. Sends nothing: the owner sees the draft
    in the chat with Send, Edit and Cancel, and only their Send sends it, exactly as shown.
    `to` is a name or handle from the people the owner allows; `text` is the whole message
    as it should be sent; `why` is one line on why you drafted it. Works whether or not the
    Mac is connected."""
    allow = _allow()
    if not allow:
        return ("Refused: no one is allowed for iMessage, or the Mac has not said who since it "
                "last started. The owner can allow people with FULLA_IMESSAGE_ALLOW on the Mac.")
    handle = resolve_recipient(to, allow)
    if handle is None:
        return f"Refused: {to} is not someone Fulla may message. Allowed: {', '.join(allow)}."
    if not text.strip():
        return "Refused: the message is empty."
    name = next((n for n, h in allow.items() if _normalize_handle(h) == handle), to)
    _propose(imessage.KIND, imessage.payload(name, handle, text.strip(), why.strip()))
    return (f"Drafted for {name} ({handle}) and shown to the owner with Send, Edit and Cancel. "
            "Nothing is sent until they choose Send; do not say it was sent.")
