"""Each allowed group's own state: a directory of Fulla's, `data/groups/<id>/`.

`PeopleStore` keeps the door: who is heard, and which groups Fulla may
speak in. This store owns what a group has once it is through the door,
in a directory named by a short id of Fulla's own, because a group's
address can change (Signal group ids and Telegram chat ids both have):

- `group.json`: the address and name as the people store knows them, the
  members (person ids), the admin (the owner, until groups are tenants of
  their own), the `tools` the owner enabled for this group from the fixed
  catalogue below, the `sources` in Mimir's shape (`type`, `identifier`,
  `label`), the project name if the group is one, and a daily cap of its own.
- `facts.json`: what the group remembers, written only through the
  injection screen (`fulla.injection.scan`).
- `reminders.json`: the group's reminders, indexed by the run that carries
  each one (`tools/group_reminders.py`).
- `credentials.json`: the group's tokens, encrypted with `FULLA_SECRET`. A
  credential is set from the owner's own chat, never typed in a group, and
  never read back by any tool: `render` says which are set and nothing else.
  With a GitHub App configured (`FULLA_GITHUB_APP_ID`, `FULLA_GITHUB_APP_KEY`)
  no group needs a GitHub token here: `github_token` mints one for a
  repository among the group's own sources, good for an hour, and a stored
  GitHub token is left unused.

Only the manager's tools write here (`set_group_tools`,
`connect_group_source`, `disconnect_group_source`, `set_group_credential`),
so only the owner does. The group's own tools, when they arrive, read a
group's sources and credentials through the run that called them, never
through an argument. When groups become tenants the directory moves and the
admin changes; nothing else does.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from fulla import injection
from fulla.people import PeopleStore
from norns_app.github import GitHubError
from norns_app.github_app import GitHubApp

# What the owner may switch on for a group. The names are the owner's
# vocabulary; which Norns tools each one unlocks is the group agent's
# business (the next step), and nothing here grants a tool.
CATALOGUE = {
    "web": "read the web through the reader",
    "memory": "remember things for the group and recall them, behind the injection screen",
    "github": "read the group's GitHub repositories (its github_repo sources)",
    "linear": "read issues in the group's Linear teams and projects, and ask the owner to file one",
    "linear_edit": "also change issues there at once, with the group's own key: assign, state, priority, "
                   "labels, comments, new issues; never delete or archive (switches linear on too)",
    "coding": "coding agents in Bubbo sandboxes on the group's github_repo sources only, for anyone in "
              "the group: provision, brief, check back, and open a draft pull request at once",
}
# An entry that only makes sense with another: switching it on switches that one on too.
IMPLIES = {"linear_edit": "linear"}
SOURCE_TYPES = {
    "github_repo": "a GitHub repository, as owner/name",
    "url": "a web page",
    "linear_team": "a Linear team, by key",
    "linear_project": "a Linear project, by id or slug",
}
CREDENTIALS = {"github": "a GitHub token", "linear": "a Linear API key"}
FACTS_MAX = 200
# Each allowed group gets a Norns agent of its own, named by its id, whose
# allowlist is exactly the group's tools (worker.group_agent).
GROUP_AGENT_PREFIX = "fulla-group-"
# The owner's own directory: sources and credentials the manager works over.
OWNER_ID = "owner"
# How the owner names it to the tools ("connect nornscode/fulla for me").
OWNER_WORDS = frozenset({"me", "mine", "myself", "my own", "owner", "you", "yourself", "us"})


def group_agent_name(record: GroupRecord) -> str:
    return GROUP_AGENT_PREFIX + record.id


@dataclass
class Source:
    type: str
    identifier: str
    label: str = ""

    @property
    def shown(self) -> str:
        return f"{self.label} ({self.type} {self.identifier})" if self.label else f"{self.type} {self.identifier}"


@dataclass
class GroupRecord:
    id: str
    address: str
    name: str
    members: list[str] = field(default_factory=list)
    admin: str = "owner"
    tools: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    project: str = ""
    daily_cap: int | None = None
    created_at: str = ""

    def source_named(self, which: str) -> Source | None:
        wanted = which.strip().lower()
        return next((s for s in self.sources if wanted in (s.label.lower(), s.identifier.lower())), None)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _key(secret: str) -> bytes:
    """A Fernet key from whatever string the owner set: any secret will do,
    and the same one always makes the same key."""
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())


class GroupStore:
    """The directories under `root`, re-read on every call: tools write
    from the worker's thread and the channel reads from the server's."""

    def __init__(self, root: Path, secret: str = "", github_app: GitHubApp | None = None):
        self.root = root
        self.github_app = github_app
        self._fernet = Fernet(_key(secret)) if secret else None

    # --- records

    def _dir(self, group_id: str) -> Path:
        return self.root / group_id

    def _read(self, path: Path) -> GroupRecord | None:
        if not path.exists():
            return None
        raw = json.loads(path.read_text() or "{}")
        raw["sources"] = [Source(**s) for s in raw.get("sources", [])]
        return GroupRecord(**raw)

    def _write(self, record: GroupRecord) -> None:
        d = self._dir(record.id)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "group.json.tmp"
        tmp.write_text(json.dumps(asdict(record), indent=2))
        tmp.replace(d / "group.json")

    def all(self) -> list[GroupRecord]:
        """Every group's record, the owner's own directory left out: it is
        no group, gets no agent, and is reached only through `owner`."""
        if not self.root.exists():
            return []
        records = [self._read(p / "group.json") for p in sorted(self.root.iterdir())
                   if p.is_dir() and p.name != OWNER_ID]
        return sorted((r for r in records if r is not None), key=lambda r: r.created_at)

    def owner(self, name: str) -> GroupRecord:
        """The owner's own directory, `data/groups/owner/`: sources and
        credentials the manager works over, by the same rule as a group's.
        Made on first use; no tools of its own, since the manager's are fixed."""
        record = self.get(OWNER_ID)
        if record is None:
            record = GroupRecord(id=OWNER_ID, address=OWNER_ID, name=name, created_at=_now())
            self._write(record)
        elif record.name != name:
            record.name = name
            self._write(record)
        return record

    def get(self, group_id: str) -> GroupRecord | None:
        return self._read(self._dir(group_id) / "group.json")

    def by_address(self, address: str) -> GroupRecord | None:
        return next((r for r in self.all() if r.address == address), None)

    def sync(self, people: PeopleStore) -> list[GroupRecord]:
        """Every group the owner has allowed gets a directory, once, and its
        name and members follow the people store. A group the owner dropped
        keeps its directory: what it had is not thrown away by a door closing."""
        records = []
        for group in people.groups():
            if not group.allowed:
                continue
            record = self.by_address(group.address)
            if record is None:
                record = GroupRecord(id=secrets.token_hex(4), address=group.address, name=group.name,
                                     members=list(group.members), created_at=_now())
                self._write(record)
            elif (record.name, record.members) != (group.name, list(group.members)):
                record.name, record.members = group.name, list(group.members)
                self._write(record)
            records.append(record)
        return records

    # --- the owner's settings

    def set_tools(self, group_id: str, names: list[str]) -> GroupRecord:
        record = self._must(group_id)
        unknown = [n for n in names if n not in CATALOGUE]
        if unknown:
            raise ValueError(f"No such tool: {', '.join(unknown)}. The catalogue is " + ", ".join(CATALOGUE) + ".")
        names = names + [IMPLIES[n] for n in names if n in IMPLIES]
        record.tools = [n for n in CATALOGUE if n in names]
        self._write(record)
        return record

    def add_source(self, group_id: str, type: str, identifier: str, label: str = "") -> Source:
        record = self._must(group_id)
        if type not in SOURCE_TYPES:
            raise ValueError(f"No such source type: {type}. One of " + ", ".join(SOURCE_TYPES) + ".")
        identifier = identifier.strip()
        if not identifier:
            raise ValueError("A source needs an identifier.")
        if type == "github_repo" and identifier.count("/") != 1:
            raise ValueError("A GitHub repository is owner/name.")
        existing = next((s for s in record.sources if (s.type, s.identifier) == (type, identifier)), None)
        if existing is not None:
            if label.strip():
                existing.label = label.strip()
                self._write(record)
            return existing
        source = Source(type=type, identifier=identifier, label=label.strip())
        record.sources.append(source)
        self._write(record)
        return source

    def drop_source(self, group_id: str, which: str) -> Source | None:
        record = self._must(group_id)
        source = record.source_named(which)
        if source is None:
            return None
        record.sources = [s for s in record.sources if s is not source]
        self._write(record)
        return source

    def _must(self, group_id: str) -> GroupRecord:
        record = self.get(group_id)
        if record is None:
            raise KeyError(group_id)
        return record

    # --- credentials: in, never out through a tool

    def set_credential(self, group_id: str, name: str, value: str) -> None:
        """Store a token for the group, encrypted. Refuses without a secret
        to encrypt with: a token on disk in the clear is worse than none."""
        self._must(group_id)
        if name not in CREDENTIALS:
            raise ValueError(f"No such credential: {name}. One of " + ", ".join(CREDENTIALS) + ".")
        if not value.strip():
            raise ValueError("The credential is empty.")
        if self._fernet is None:
            raise ValueError("FULLA_SECRET is not set, so I have nothing to encrypt it with; set it first.")
        path = self._dir(group_id) / "credentials.json"
        stored = json.loads(path.read_text() or "{}") if path.exists() else {}
        stored[name] = self._fernet.encrypt(value.strip().encode()).decode()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(stored, indent=2))
        tmp.replace(path)

    def credential(self, group_id: str, name: str) -> str | None:
        """For a group's own tools, in the run that is the group's. None when
        unset, or when the secret has changed since it was stored."""
        path = self._dir(group_id) / "credentials.json"
        if self._fernet is None or not path.exists():
            return None
        token = (json.loads(path.read_text() or "{}")).get(name)
        if not token:
            return None
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            return None

    def credential_names(self, group_id: str) -> list[str]:
        path = self._dir(group_id) / "credentials.json"
        if not path.exists():
            return []
        return [n for n in CREDENTIALS if n in json.loads(path.read_text() or "{}")]

    def github_token(self, group_id: str) -> str | Callable[[str], str] | None:
        """What a group's GitHub client is made with. With the app: a function
        that mints a token for one repository, and only for one among the
        group's own sources as they are when it is called, so a token for
        anything else the app is installed on is never made. Without it: the
        token the owner stored, or None."""
        app = self.github_app
        if app is None:
            return self.credential(group_id, "github")

        def mint(repo: str) -> str:
            record = self.get(group_id)
            held = {s.identifier.lower() for s in (record.sources if record else ()) if s.type == "github_repo"}
            if repo.lower() not in held:
                raise GitHubError(f"{repo} is not among the group's sources", 403)
            return app.token(repo)

        return mint

    def _credentials_shown(self, group_id: str) -> str:
        names = self.credential_names(group_id)
        if self.github_app is not None:
            names = ["github (the app)"] + [n for n in names if n != "github"]
        return ", ".join(names) or "none"

    # --- memory, behind the screen

    def remember(self, group_id: str, text: str) -> str | None:
        """Keep a fact for the group; the reason it was refused, or None."""
        self._must(group_id)
        text = text.strip()
        if not text:
            return "nothing to remember"
        reason = injection.scan(text)
        if reason is not None:
            return reason
        path = self._dir(group_id) / "facts.json"
        facts = self.facts(group_id)
        facts.append({"text": text, "at": _now()})
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(facts[-FACTS_MAX:], indent=2))
        tmp.replace(path)
        return None

    def facts(self, group_id: str) -> list[dict]:
        path = self._dir(group_id) / "facts.json"
        return json.loads(path.read_text() or "[]") if path.exists() else []

    def reminders_path(self, group_id: str) -> Path:
        return self._dir(group_id) / "reminders.json"

    # --- for the owner

    def render(self) -> str:
        lines = []
        mine = self.get(OWNER_ID)
        if mine is not None:
            sources = "; ".join(s.shown for s in mine.sources) or "none"
            creds = self._credentials_shown(OWNER_ID)
            lines.append(f"Your own sources, for you here: {sources}; credentials set: {creds}.")
        records = self.all()
        if not records:
            lines.append("No group has a directory yet: allow one and it gets one.")
            return "\n".join(lines)
        lines.append("Groups, each with its own directory:")
        for i, r in enumerate(records, 1):
            tools = ", ".join(r.tools) or "none"
            sources = "; ".join(s.shown for s in r.sources) or "none"
            creds = self._credentials_shown(r.id)
            project = f"; project {r.project}" if r.project else ""
            lines.append(f"{i}. {r.name} ({r.address}; id {r.id}{project}): {len(r.members)} members; "
                         f"tools: {tools}; sources: {sources}; credentials set: {creds}")
        return "\n".join(lines)
