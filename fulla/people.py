"""Who, besides the owner, may talk to Fulla, and where.

Fulla's first rule is that she never acts in public. This store is the one
exception the owner can open: a list of people, each with their ids on the
messengers, and a list of groups the owner has let her speak in. Everyone
and everywhere else is dropped at the door. The owner changes the lists from
chat (`allow_person`, `allow_group` and the rest in tools/people.py); a group
gets on the list by the owner adding Fulla to it and answering her private
question about it, and a person the owner has not listed who addresses her
in an allowed group gets the same treatment: Fulla asks the owner once, and
remembers the answer either way.

A person may go by several ids on one messenger: Signal shows a sender's
account id, and the number only when they share it, so a friend allowed by
number may arrive as an id. Both are kept.

A conversation with anyone but the owner, or in any group, runs on the
social agent (`fulla-social`), which holds none of the owner's tools or
facts; that, not a prompt rule, is what keeps the owner's life private.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class Person:
    name: str
    ids: dict[str, list[str]] = field(default_factory=dict)  # transport -> ids as strings
    allowed: bool = True  # False: the owner said no (or has not answered yet; see `asked`)
    asked: str = ""  # ref of the private question to the owner about this person, "" if never asked
    added_at: str = ""
    id: str = ""  # Fulla's own short id, stable while names and messenger ids change

    def has(self, transport: str, user_id) -> bool:
        return str(user_id) in self.ids.get(transport, [])


@dataclass
class Group:
    address: str  # the channel's address for it: "telegram:-100…", "signal:group:<id>"
    name: str
    allowed: bool = False  # asked and answered yes
    asked: str = ""  # ref of the private question to the owner, "" until asked
    added_at: str = ""
    members: list[str] = field(default_factory=list)  # ids of the people heard in it (the owner is not a Person)

    @property
    def transport(self) -> str:
        return self.address.split(":", 1)[0]


def _new_id() -> str:
    return secrets.token_hex(4)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clean(ids: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for transport, values in ids.items():
        if isinstance(values, str):
            values = [values]
        kept = [str(v).strip() for v in values if str(v).strip()]
        if kept:
            out[transport] = kept
    return out


class PeopleStore:
    """people.json. Re-read on every lookup: the tools write it from the
    worker's thread and the channel reads it from the server's, in one
    process in production and in local dev alike."""

    def __init__(self, path: Path):
        self.path = path
        self._people: list[Person] = []
        self._groups: list[Group] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._people, self._groups = [], []
            return
        raw = json.loads(self.path.read_text() or "{}")
        self._people = [Person(**{**p, "ids": _clean(p.get("ids") or {})}) for p in raw.get("people", [])]
        self._groups = [Group(**g) for g in raw.get("groups", [])]
        # People listed before ids existed get one now, and keep it.
        if any(not p.id for p in self._people):
            for p in self._people:
                p.id = p.id or _new_id()
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"people": [asdict(p) for p in self._people],
                                   "groups": [asdict(g) for g in self._groups]}, indent=2))
        tmp.replace(self.path)

    # --- people

    def people(self) -> list[Person]:
        self._load()
        return list(self._people)

    def person_for(self, transport: str, user_id) -> Person | None:
        """The listed, allowed person this id belongs to."""
        self._load()
        return next((p for p in self._people if p.allowed and p.has(transport, user_id)), None)

    def anyone_for(self, transport: str, user_id) -> Person | None:
        """Allowed or not: anyone the store has heard of under this id."""
        self._load()
        return next((p for p in self._people if p.has(transport, user_id)), None)

    def person_named(self, name: str) -> Person | None:
        self._load()
        wanted = name.strip().lower()
        return next((p for p in self._people if p.name.lower() == wanted), None)

    def allow_person(self, name: str, ids: dict) -> Person:
        """Add a person, or add ids to one already listed by that name, and
        allow them."""
        self._load()
        ids = _clean(ids)
        if not name.strip():
            raise ValueError("a person needs a name")
        if not ids:
            raise ValueError("a person needs at least one id: a Telegram user id, a Signal number or account id")
        person = self.person_named(name)
        if person is None:
            # An id may already be known under a placeholder name from a group ask.
            person = next((p for p in self._people
                           if any(p.has(t, v) for t, vs in ids.items() for v in vs)), None)
        if person is None:
            person = Person(name=name.strip(), ids={}, added_at=_now(), id=_new_id())
            self._people.append(person)
        person.name = name.strip()
        for transport, values in ids.items():
            known = person.ids.setdefault(transport, [])
            known += [v for v in values if v not in known]
        person.allowed = True
        self._save()
        return person

    def remember_person(self, transport: str, user_id, name: str) -> Person:
        """Someone unlisted who spoke up: kept, not allowed, until the owner says."""
        self._load()
        person = self.anyone_for(transport, user_id)
        if person is None:
            person = Person(name=name.strip() or str(user_id), ids={transport: [str(user_id)]},
                            allowed=False, added_at=_now(), id=_new_id())
            self._people.append(person)
            self._save()
        return person

    def person_by_id(self, person_id: str) -> Person | None:
        self._load()
        return next((p for p in self._people if p.id == person_id), None)

    def saw_in_group(self, address: str, transport: str, user_id, name: str) -> Person | None:
        """Someone spoke in a group: they are a member of it from now, listed
        or not (an unlisted speaker is remembered, not allowed). None when the
        group is not on the list."""
        self._load()
        group = self.group(address)
        if group is None:
            return None
        person = self.remember_person(transport, user_id, name)
        self._load()
        group = self.group(address)
        if person.id not in group.members:  # pyright: ignore[reportOptionalMemberAccess]
            group.members.append(person.id)  # pyright: ignore[reportOptionalMemberAccess]
            self._save()
        return person

    def members(self, address: str) -> list[Person]:
        self._load()
        group = self.group(address)
        if group is None:
            return []
        by_id = {p.id: p for p in self._people}
        return [by_id[m] for m in group.members if m in by_id]

    def mark_person_asked(self, transport: str, user_id, ref: str) -> None:
        self._load()
        person = self.anyone_for(transport, user_id)
        if person is not None:
            person.asked = ref
            self._save()

    def person_for_ask(self, ref: str) -> Person | None:
        self._load()
        return next((p for p in self._people if p.asked == ref), None)

    def decide_person(self, transport: str, user_id, allowed: bool) -> Person | None:
        self._load()
        person = self.anyone_for(transport, user_id)
        if person is None:
            return None
        person.allowed = allowed
        self._save()
        return person

    def drop_person(self, name: str) -> bool:
        self._load()
        before = len(self._people)
        self._people = [p for p in self._people if p.name.lower() != name.strip().lower()]
        if len(self._people) == before:
            return False
        self._save()
        return True

    # --- groups

    def groups(self) -> list[Group]:
        self._load()
        return list(self._groups)

    def group(self, address: str) -> Group | None:
        self._load()
        return next((g for g in self._groups if g.address == address), None)

    def group_named(self, name: str) -> Group | None:
        """By name, or by address, or by its number in `render`."""
        self._load()
        wanted = name.strip().lower()
        for i, g in enumerate(self._groups, 1):
            if wanted in (g.name.lower(), g.address.lower(), str(i)):
                return g
        return None

    def remember_group(self, address: str, name: str) -> Group:
        """A group Fulla was added to, not yet allowed. Idempotent."""
        self._load()
        group = self.group(address)
        if group is None:
            group = Group(address=address, name=name or address, added_at=_now())
            self._groups.append(group)
            self._save()
        elif name and group.name != name and group.name == group.address:
            group.name = name
            self._save()
        return group

    def mark_asked(self, address: str, ref: str) -> None:
        self._load()
        group = self.group(address)
        if group is not None:
            group.asked = ref
            self._save()

    def group_for_ask(self, ref: str) -> Group | None:
        self._load()
        return next((g for g in self._groups if g.asked == ref), None)

    def allow_group(self, address: str, allowed: bool = True) -> Group | None:
        self._load()
        group = self.group(address)
        if group is None:
            return None
        group.allowed = allowed
        self._save()
        return group

    def drop_group(self, address: str) -> bool:
        self._load()
        before = len(self._groups)
        self._groups = [g for g in self._groups if g.address != address]
        if len(self._groups) == before:
            return False
        self._save()
        return True

    # --- for the owner

    def render(self) -> str:
        self._load()
        lines = []
        allowed = [p for p in self._people if p.allowed]
        declined = [p for p in self._people if not p.allowed]
        if allowed:
            lines.append("People who may talk to you:")
            lines += [f"- {p.name}: {_ids(p)}" for p in allowed]
        else:
            lines.append("Nobody but the owner may talk to you.")
        if declined:
            lines.append("People you have not let in (asked, or said no to):")
            lines += [f"- {p.name}: {_ids(p)}" for p in declined]
        if self._groups:
            lines.append("Groups:")
            by_id = {p.id: p for p in self._people}
            for i, g in enumerate(self._groups, 1):
                state = "allowed" if g.allowed else ("asked, not answered" if g.asked else "seen, not asked")
                heard = [by_id[m].name for m in g.members if m in by_id]
                members = f"; heard: {', '.join(heard)}" if heard else ""
                lines.append(f"{i}. {g.name} ({g.address}): {state}{members}")
        else:
            lines.append("No groups.")
        return "\n".join(lines)


def _ids(p: Person) -> str:
    return ", ".join(f"{t} {' / '.join(vs)}" for t, vs in sorted(p.ids.items()))
