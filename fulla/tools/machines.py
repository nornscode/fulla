"""The owner's machines, as the manager may list and describe them: what
Fulla knows from each worker's announcement (hosts.py), plus what the owner
said a machine is for, so "the dev server" resolves to a name. Forgetting a
machine drops it from the prompt, the allowlist and the per-turn note; a
worker that announces again is known again. Owner-only by being the
manager's: the social and group agents never hold these.
"""

# Module state is set by init() before any tool runs; it is None only before then.
# pyright: reportOptionalMemberAccess=false, reportOptionalCall=false

from __future__ import annotations

import logging
from collections.abc import Callable

from fulla.hosts import MAC, HostStore
from fulla.tooldef import tool

logger = logging.getLogger("fulla.tools.machines")

_store: HostStore | None = None
_push: Callable[[], None] | None = None


def init(*, store: HostStore, push_prompt: Callable[[], None]):
    """`push_prompt` re-renders the manager's prompt and allowlist on Norns,
    so a description or a forgotten machine is live on the next turn."""
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


def _label(name: str) -> str:
    return "the Mac" if name == MAC else name


@tool
def list_machines() -> str:
    """The owner's machines Fulla has met, by the prefix of their tools: what each is for
    (if the owner said), its platform, its allowed tmux sessions, whether its shell is on,
    and when its worker last started. Whether one is connected right now is in the
    "This turn" note, not here."""
    hosts = _store.all()
    if not hosts:
        return "No machine has announced itself yet."
    lines = []
    for h in hosts:
        purpose = f": {h.purpose}" if h.purpose else " (nothing said about what it is for)"
        when = h.seen_at[:16].replace("T", " ")
        lines.append(f"- `{h.prefix}` {_label(h.name)}{purpose}. {h.platform}; tmux sessions: "
                     f"{', '.join(h.sessions) or 'none'}; shell: {'on' if h.shell else 'off'}; "
                     f"worker last started {when} UTC.")
    return "\n".join(lines)


@tool(side_effect=True)
def describe_machine(name: str, purpose: str) -> str:
    """Record what a machine is for, in the owner's own words ("my dev server with the
    widgets checkout"), so you can route a plain reference to it. `name` is the machine's
    name (the prefix of its tools without the underscore: mac, box). Only from the owner's
    own words."""
    name = name.strip().lower().rstrip("_")
    if not purpose.strip():
        return "Say what the machine is for."
    if not _store.describe(name, purpose):
        known = ", ".join(h.name for h in _store.all()) or "none yet"
        return f"No machine named {name!r}; known: {known}."
    return _pushed(f"Noted: {_label(name)} is {purpose.strip()}.")


@tool(side_effect=True)
def forget_machine(name: str) -> str:
    """Forget a machine the owner says is gone, renamed or rebuilt: its tools leave your
    list and the prompt until a worker announces that name again, and the name is free for
    the next machine that does (a rebuilt one has a new id). The Mac included: forgotten,
    it is known again the next time its worker starts. Only on the owner's word."""
    name = name.strip().lower().rstrip("_")
    if not _store.forget(name):
        return f"No machine named {name!r} to forget."
    if name == MAC:
        return _pushed("Forgot the Mac. The next time its worker starts it is known again, with its "
                       "iMessage list and sessions; a rebuilt Mac can take the name now.")
    return _pushed(f"Forgot {name}. If its worker is still running it will announce itself again; "
                   "stop it there, or give it another name, if that is not wanted.")
