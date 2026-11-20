"""Which group a tool serves: from the run, never from an argument.

This is the inner of the two locks on a group. The outer one is Norns's
allowlist, drawn per group agent (`worker.group_agent`); this one is the
tool itself refusing to look past its own group. The channel records which
chat each social run answers (`social_origins`), lends that lookup to
`runctx`, and the worker sets the run on every tool call. A group tool asks
here for its group, refuses with a fixed sentence when there is none, and
refuses any repository, team or host that is not among that group's
sources, even when the model names one. With no source named it works on
the group's own, never on "all". Whatever a tool says to a group is a fixed
sentence: never an exception, a path, a host or a URL of Fulla's own.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fulla import runctx
from fulla.groups import GroupRecord, GroupStore, Source

NO_GROUP = "I can only do that in a group that is set up for it."
NOT_CONNECTED = "That isn't connected here."
NOTHING_CONNECTED = "Nothing of that kind is connected here."


class Refused(Exception):
    """A fixed sentence for the group; `str(e)` is all a tool may say."""


_store: GroupStore | None = None
_owner_name = "the owner"


def init(*, store: GroupStore | None, owner_name: str = "the owner"):
    global _store, _owner_name
    _store = store
    _owner_name = owner_name


def current() -> GroupRecord | None:
    """The scope whose run is calling: the group the channel sent the run
    for, or the owner's own directory when the run is the manager's (core
    names the agent on every tool task; `runctx.current_is_manager`). None
    in a friend's direct chat or outside any run, and for any other agent."""
    if _store is None:
        return None
    address = runctx.current_address()
    if address:
        return _store.by_address(address)
    if runctx.current_is_manager():
        return _store.owner(_owner_name)
    return None


def require() -> GroupRecord:
    record = current()
    if record is None:
        raise Refused(NO_GROUP)
    return record


def _host(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return (parsed.hostname or "").lower()


def source(record: GroupRecord, type: str, identifier: str) -> Source | None:
    """The group's source that `identifier` names, by identifier or label,
    or by host for a URL; None when the group has no such source."""
    wanted = identifier.strip().lower()
    if not wanted:
        return None
    for s in record.sources:
        if s.type != type:
            continue
        if s.identifier.lower() == wanted or (s.label and s.label.lower() == wanted):
            return s
        if type == "url" and _host(s.identifier) == _host(wanted):
            return s
    return None


def scoped(record: GroupRecord, type: str, identifier: str = "") -> list[Source]:
    """The sources a tool may work on: the one named, if it is the group's,
    or all of the group's of that type when none is named. Anything else
    is refused, whoever named it."""
    if identifier.strip():
        s = source(record, type, identifier)
        if s is None:
            raise Refused(NOT_CONNECTED)
        return [s]
    own = [s for s in record.sources if s.type == type]
    if not own:
        raise Refused(NOTHING_CONNECTED)
    return own
