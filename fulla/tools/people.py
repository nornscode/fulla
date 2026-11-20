"""The owner's say over who else may talk to Fulla. Manager-only tools, so
only the owner ever calls them; a friend talking to the social agent has
no way to let anyone else in."""

# Module state is set by init() before any tool runs; it is None only before then.
# pyright: reportOptionalMemberAccess=false

from __future__ import annotations

from collections.abc import Callable

from fulla.people import PeopleStore
from fulla.tooldef import tool

_store: PeopleStore | None = None
_on_group_allowed: Callable[[], None] | None = None


def init(*, store: PeopleStore, on_group_allowed: Callable[[], None] | None = None):
    """`on_group_allowed` runs after a group is allowed from here: the group
    gets its directory and its agent at once (main.push_groups)."""
    global _store, _on_group_allowed
    _store = store
    _on_group_allowed = on_group_allowed


@tool(side_effect=True)
def allow_person(name: str, telegram_user_id: str = "", signal_number: str = "", signal_account_id: str = "") -> str:
    """Let a person talk to you directly and in allowed groups, by name and at least one id:
    their numeric Telegram user id, their Signal number in international form (+1…), or their
    Signal account id (the long code Signal shows when someone hides their number; it is in the
    question you were asked about them, and in `list_people`). Calling it again for the same name
    adds ids, and a person you were asked about and said no to is let in. They get the social you,
    never the owner's tools or facts."""
    ids: dict[str, list[str]] = {}
    if telegram_user_id.strip():
        if not telegram_user_id.strip().lstrip("-").isdigit():
            return "A Telegram user id is a number; @userinfobot tells someone theirs."
        ids["telegram"] = [telegram_user_id.strip()]
    if signal_number.strip():
        number = signal_number.strip().replace(" ", "").replace("-", "")
        if not (number.startswith("+") and number[1:].isdigit()):
            return "A Signal number is international, like +16045550100."
        ids.setdefault("signal", []).append(number)
    if signal_account_id.strip():
        ids.setdefault("signal", []).append(signal_account_id.strip())
    try:
        person = _store.allow_person(name, ids)
    except ValueError as e:
        return str(e)
    return f"{person.name} may talk to you: " + ", ".join(
        f"{t} {' / '.join(vs)}" for t, vs in sorted(person.ids.items()))


@tool(side_effect=True)
def drop_person(name: str) -> str:
    """Stop a person from talking to you, by name. They are forgotten; if they address you in a
    group again you will ask about them again. To ignore someone for good, answer no to that question
    and leave them on the list."""
    return f"Dropped {name}." if _store.drop_person(name) else f"Nobody named {name!r} is on the list."


@tool
def list_people() -> str:
    """Who may talk to you besides the owner, and which groups you may speak in."""
    return _store.render()


@tool(side_effect=True)
def allow_group(group: str) -> str:
    """Let yourself talk in a group you were added to, by its name or number in `list_people`.
    A group is known once the owner has sent a message in it with you present."""
    g = _store.group_named(group)
    if g is None:
        return f"No group matches {group!r}. {_store.render()}"
    _store.allow_group(g.address, True)
    if _on_group_allowed is not None:
        _on_group_allowed()
    return f"You may talk in {g.name} when someone addresses you."


@tool(side_effect=True)
def drop_group(group: str) -> str:
    """Stop talking in a group, by its name or number in `list_people`."""
    g = _store.group_named(group)
    if g is None:
        return f"No group matches {group!r}."
    _store.drop_group(g.address)
    return f"You no longer talk in {g.name}."
