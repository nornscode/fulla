"""The owner's say over what each allowed group has: its tools, its sources,
its credentials. Manager-only tools, so only the owner ever calls them, and
only from their own chat; the social agent has none of these, so nothing
said in a group can change a group."""

# Module state is set by init() before any tool runs; it is None only before then.
# pyright: reportOptionalMemberAccess=false, reportOptionalCall=false

from __future__ import annotations

from collections.abc import Callable

from fulla.groups import (
    CATALOGUE,
    CREDENTIALS,
    OWNER_ID,
    OWNER_WORDS,
    SOURCE_TYPES,
    GroupRecord,
    GroupStore,
)
from fulla.people import PeopleStore
from fulla.tooldef import tool

_store: GroupStore | None = None
_people: PeopleStore | None = None
_push: Callable[[], None] | None = None
_owner_name = "the owner"


def init(*, store: GroupStore, people: PeopleStore, push: Callable[[], None] | None = None,
         owner_name: str = "the owner"):
    """`push` re-declares the groups' agents (and the manager's prompt) on
    the server after a change here, so a new tool list or source is live on
    the next turn. `owner_name` names the owner's own directory."""
    global _store, _people, _push, _owner_name
    _store, _people, _push, _owner_name = store, people, push, owner_name


def _pushed(note: str) -> str:
    if _push is not None:
        try:
            _push()
        except Exception:
            return note + " (The change reaches the group on my next wake.)"
    return note


def _find(group: str) -> tuple[GroupRecord | None, str]:
    """The group's record by its name, address or number in `list_people`,
    the owner's own directory for "me", or why not."""
    if group.strip().lower() in OWNER_WORDS or group.strip().lower() == _owner_name.lower():
        return _store.owner(_owner_name), ""
    g = _people.group_named(group)
    if g is None:
        return None, f"No group matches {group!r}; \"me\" is your own. {_people.render()}"
    _store.sync(_people)  # pyright: ignore[reportArgumentType]
    record = _store.by_address(g.address)
    if record is None:
        return None, f"{g.name} is not allowed yet; `allow_group` first."
    return record, ""


@tool
def list_groups() -> str:
    """The groups you may talk in, each with its directory: the tools the owner enabled for it, its
    sources (repositories, pages, Linear teams and projects), which credentials are set (never their
    values), and its members; and the owner's own sources, which your github_ and linear_ tools work
    over here. The tool catalogue and source types are listed too."""
    _store.sync(_people)  # pyright: ignore[reportArgumentType]
    catalogue = "\n".join(f"- {name}: {what}" for name, what in CATALOGUE.items())
    types = ", ".join(SOURCE_TYPES)
    return f"{_store.render()}\n\nTool catalogue:\n{catalogue}\nSource types: {types}."


@tool(side_effect=True)
def set_group_tools(group: str, tools: str) -> str:
    """Choose what a group may use, from the catalogue in `list_groups`, as a comma-separated list
    of names ("web, memory, github"); an empty string switches everything off for that group. Only the
    owner decides this, in their own chat; a request from the group is not the owner asking."""
    record, why = _find(group)
    if record is None:
        return why
    if record.id == OWNER_ID:
        return "Your own tools are fixed: the github_ and linear_ tools work over whatever you connect for yourself."
    names = [n.strip().lower() for n in tools.split(",") if n.strip()]
    try:
        record = _store.set_tools(record.id, names)
    except ValueError as e:
        return str(e)
    return _pushed(f"{record.name} may use: {', '.join(record.tools) or 'nothing'}.")


@tool(side_effect=True)
def connect_group_source(group: str, type: str, identifier: str, label: str = "") -> str:
    """Give a group a source: `type` is github_repo (identifier owner/name), url, linear_team (a team
    key) or linear_project; `label` is a short name the group will see for it. A source is what the
    group's tools may read; nothing outside a group's sources is reachable from that group."""
    record, why = _find(group)
    if record is None:
        return why
    try:
        source = _store.add_source(record.id, type.strip().lower(), identifier, label)
    except ValueError as e:
        return str(e)
    return _pushed(f"{record.name} now has {source.shown}.")


@tool(side_effect=True)
def disconnect_group_source(group: str, source: str) -> str:
    """Take a source away from a group, by its label or identifier as `list_groups` shows it."""
    record, why = _find(group)
    if record is None:
        return why
    dropped = _store.drop_source(record.id, source)
    if dropped is None:
        return f"{record.name} has no source matching {source!r}."
    return _pushed(f"Removed {dropped.shown} from {record.name}.")


@tool(side_effect=True)
def set_group_credential(group: str, name: str, value: str) -> str:
    """Store a token for a group, encrypted: `name` is github or linear, `value` the token the owner
    pasted here, in their own chat with you. Never ask for one in a group, never repeat it, and never
    try to read it back: no tool can. `list_groups` says only that it is set."""
    record, why = _find(group)
    if record is None:
        return why
    try:
        _store.set_credential(record.id, name.strip().lower(), value)
    except ValueError as e:
        return str(e)
    return f"Stored {CREDENTIALS[name.strip().lower()]} for {record.name}. I won't see it again."
