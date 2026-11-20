"""A group's Linear, through the inner lock, the way `group_github.py` is
its GitHub: the group from the run, the key from the group's credentials,
the teams and projects from the group's sources (`linear_team` by key,
`linear_project` by id or slug). Titles, descriptions and comments come
inside the untrusted marker; a new issue is a card for the owner
(linear_cards.py); every failure is a fixed sentence.

The `linear_edit_tools` are the writes a group may do at once when the
owner switched "linear_edit" on for it (and the owner always may, over
their own directory): assign, update state, priority, title or labels,
comment, create. Each fetches the issue first and refuses one outside the
group's teams and projects the way the reads do; names (an assignee, a
state, a label) are resolved against what Linear holds, never trusted as
ids; nothing deletes or archives."""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx

from fulla import groupctx, linear_cards
from fulla.groups import OWNER_ID, GroupRecord, GroupStore, Source
from fulla.tooldef import tool
from fulla.untrusted import untrusted
from norns_app import linear as ln
from norns_app.linear import Linear, LinearError

logger = logging.getLogger("fulla.tools.group_linear")

NO_KEY = "This group has no Linear key yet; the owner can set one."
NO_ANSWER = "Linear didn't answer that."
NO_TEAM = "No Linear team is connected to this group."
WHICH_TEAM = "Say which team: "
NO_CARD = "I can't ask the owner from here."
NOT_EDITABLE = "Changing Linear isn't switched on here."
NOTHING_TO_CHANGE = "Say what to change: state, priority, title or labels."
NOBODY = "Nobody in Linear matches "
WHICH_ONE = "Which one: "
NO_STATE = "That team has no state called "
NO_LABEL = "That team has no label called "
UNASSIGN = {"", "nobody", "no one", "noone", "unassigned", "none"}
TYPES = ("linear_team", "linear_project")
PRIORITIES = {0: "none", 1: "urgent", 2: "high", 3: "medium", 4: "low"}

_store: GroupStore | None = None
_propose: Callable[[str, dict], object] | None = None
_transport: httpx.BaseTransport | None = None


def init(*, store: GroupStore | None, propose: Callable[[str, dict], object] | None = None,
         transport: httpx.BaseTransport | None = None):
    global _store, _propose, _transport
    _store, _propose, _transport = store, propose, transport


def _client(record: GroupRecord) -> Linear:
    key = _store.credential(record.id, "linear") if _store is not None else None
    if not key:
        raise groupctx.Refused(NO_KEY)
    return Linear(key, transport=_transport)


def _scoped(record: GroupRecord, source: str) -> list[Source]:
    """The group's Linear sources, teams and projects both: the one named,
    or all of them."""
    if source.strip():
        for type_ in TYPES:
            s = groupctx.source(record, type_, source)
            if s is not None:
                return [s]
        raise groupctx.Refused(groupctx.NOT_CONNECTED)
    own = [s for s in record.sources if s.type in TYPES]
    if not own:
        raise groupctx.Refused(groupctx.NOTHING_CONNECTED)
    return own


def _failed(e: LinearError) -> str:
    logger.warning("linear for a group: %s", e)
    return f"Linear said {e.status}." if e.status else NO_ANSWER


def _run(do: Callable[[], str]) -> str:
    try:
        return do()
    except groupctx.Refused as e:
        return str(e)
    except LinearError as e:
        return _failed(e)


def _mine(record: GroupRecord, issue: dict) -> bool:
    """Whether an issue Linear returned belongs to one of the group's teams
    or projects; what the key can reach beyond them is refused."""
    team = ((issue.get("team") or {}).get("key") or "").lower()
    project = issue.get("project") or {}
    ids = {str(project.get("id") or "").lower(), str(project.get("slugId") or "").lower()} - {""}
    for s in record.sources:
        if s.type == "linear_team" and s.identifier.lower() == team:
            return True
        if s.type == "linear_project" and s.identifier.lower() in ids:
            return True
    return False


def _line(issue: dict) -> tuple[str, str]:
    state = (issue.get("state") or {}).get("name", "?")
    who = (issue.get("assignee") or {}).get("name") or "unassigned"
    fact = f"{issue.get('identifier', '?')} ({state}, {who}, updated {str(issue.get('updatedAt', ''))[:10]})"
    return fact, f"{issue.get('identifier', '?')}: {ln.text(issue.get('title'), 200)}"


@tool
def linear_issues(source: str = "", state: str = "", limit: int = 20) -> str:
    """Issues in this group's Linear teams and projects, newest activity first; `source` (a team
    key, a project, or its label) narrows to one; `state` to a state name ("In Progress"). Identifiers,
    states, assignees and dates are facts; titles come marked as untrusted text."""
    def do():
        record = groupctx.require()
        client = _client(record)
        out = []
        for s in _scoped(record, source):
            issues = client.issues(team_key=s.identifier if s.type == "linear_team" else "",
                                   project=s.identifier if s.type == "linear_project" else "",
                                   state=state, limit=limit)
            issues = [i for i in issues if _mine(record, i)]
            if not issues:
                out.append(f"{s.shown}: no{' ' + state if state else ''} issues.")
                continue
            facts, texts = zip(*(_line(i) for i in issues))
            out.append(f"{s.shown}:\n" + "\n".join(facts) + "\n\n"
                       + untrusted("issue text", f"Linear ({s.identifier})", "\n".join(texts)))
        return "\n\n".join(out)
    return _run(do)


@tool
def linear_issue(identifier: str) -> str:
    """One Linear issue in full, by identifier ("FUL-12"): state, assignee, team, project, and its
    description and latest comments, which come marked as untrusted text. Only issues in this
    group's teams and projects."""
    def do():
        record = groupctx.require()
        client = _client(record)
        issue = client.issue(identifier.strip())
        if issue is None or not _mine(record, issue):
            raise groupctx.Refused(groupctx.NOT_CONNECTED)
        fact, _ = _line(issue)
        team = (issue.get("team") or {}).get("key", "?")
        project = (issue.get("project") or {}).get("name") or "none"
        lines = [fact, f"Team: {team}; project: {project}; priority {issue.get('priority', '?')}"]
        texts = [f"Title: {ln.text(issue.get('title'), 200)}", "", ln.text(issue.get("description")) or "(no description)"]
        for c in ((issue.get("comments") or {}).get("nodes") or []):
            who = (c.get("user") or {}).get("name") or "?"
            texts.append(f"\n{who} ({str(c.get('createdAt', ''))[:10]}): {ln.text(c.get('body'), 600)}")
        return "\n".join(lines) + "\n\n" + untrusted("issue text", f"Linear ({team})", "\n".join(texts))
    return _run(do)


@tool(side_effect=True)
def propose_linear_issue(title: str, description: str = "", team: str = "") -> str:
    """Ask the owner to file an issue in one of this group's Linear teams. Files nothing: the
    owner sees it on a card with File, Edit and Cancel; only their File files it. `team` is the
    team key or label, needed when the group has more than one. Say the owner has been asked,
    never that it was filed."""
    def do():
        record = groupctx.require()
        client = _client(record)
        if _propose is None:
            raise groupctx.Refused(NO_CARD)
        if not title.strip():
            return "An issue needs a title."
        chosen, team_id = _team(record, client, team)
        _propose(linear_cards.KIND, linear_cards.payload(record.id, record.name, chosen.identifier.upper(), team_id,
                                                         title.strip(), description.strip()))
        return (f"The owner has been asked to file \"{title.strip()}\" in {chosen.identifier.upper()}. Nothing is "
                "filed until they choose File; say they were asked, not that it was filed.")
    return _run(do)


def _team(record: GroupRecord, client: Linear, team: str) -> tuple[Source, str]:
    """The group's team to file in (the one named, or its only one) and
    Linear's id for it."""
    teams = [s for s in record.sources if s.type == "linear_team"]
    if team.strip():
        chosen = groupctx.source(record, "linear_team", team)
        if chosen is None:
            raise groupctx.Refused(groupctx.NOT_CONNECTED)
    elif len(teams) == 1:
        chosen = teams[0]
    elif not teams:
        raise groupctx.Refused(NO_TEAM)
    else:
        raise groupctx.Refused(WHICH_TEAM + ", ".join(s.shown for s in teams) + ".")
    team_id = next((t.get("id") for t in client.teams()
                    if str(t.get("key", "")).lower() == chosen.identifier.lower()), None)
    if not team_id:
        raise groupctx.Refused(f"Linear has no team {chosen.identifier} for this key.")
    return chosen, team_id


linear_tools = [linear_issues, linear_issue, propose_linear_issue]


# --- the writes, behind the owner's "linear_edit" switch

def _editable() -> tuple[GroupRecord, Linear]:
    """The inner lock on a write: the run's group, with the owner's switch
    on for it (the owner's own directory needs no switch), and its client.
    The allowlist keeps these tools off a group without the switch; this is
    the same answer from inside, should a definition lag."""
    record = groupctx.require()
    if record.id != OWNER_ID and "linear_edit" not in record.tools:
        raise groupctx.Refused(NOT_EDITABLE)
    return record, _client(record)


def _held(record: GroupRecord, client: Linear, identifier: str) -> dict:
    """The issue, fetched, and the group's; anything else is refused with
    the same sentence whether it exists or not."""
    issue = client.issue(identifier.strip())
    if issue is None or not _mine(record, issue):
        raise groupctx.Refused(groupctx.NOT_CONNECTED)
    return issue


def _one(found: list[dict], name: str, none: str, keys: tuple[str, ...] = ("name",)) -> dict:
    """Exactly one of `found` by `name`: an exact match on any of `keys`
    (case folded) wins; otherwise the only one containing it, or a refusal
    naming those that do, or `none` when nothing does."""
    wanted = name.strip().lower()
    exact = [f for f in found if wanted in {str(f.get(k) or "").lower() for k in keys}]
    if len(exact) == 1:
        return exact[0]
    candidates = exact or [f for f in found if any(wanted in str(f.get(k) or "").lower() for k in keys)]
    if not candidates:
        raise groupctx.Refused(none + f"{name.strip()!r}.")
    if len(candidates) > 1:
        names = sorted({ln.text(str(f.get("name") or f.get(keys[0]) or "?"), 60) for f in candidates})
        raise groupctx.Refused(WHICH_ONE + ", ".join(names[:8]) + ".")
    return candidates[0]


def _named(name: str) -> str:
    return ln.text(name, 60)


@tool(side_effect=True)
def linear_assign(identifier: str, assignee: str) -> str:
    """Assign a Linear issue in this group's teams or projects to someone, by their name as Linear
    shows it (a person or one of Linear's agents), or to nobody ("nobody" unassigns). Done at once
    with this group's own key; say who it went to."""
    def do():
        record, client = _editable()
        issue = _held(record, client, identifier)
        if assignee.strip().lower() in UNASSIGN:
            client.update_issue(issue["id"], assigneeId=None)
            return f"{issue.get('identifier')} is now unassigned."
        user = _one(client.users(assignee.strip()), assignee, NOBODY, ("name", "displayName"))
        client.update_issue(issue["id"], assigneeId=user["id"])
        return f"{issue.get('identifier')} is now assigned to {_named(str(user.get('name') or '?'))}."
    return _run(do)


@tool(side_effect=True)
def linear_update(identifier: str, state: str = "", priority: int = -1, title: str = "", labels: str = "") -> str:
    """Change a Linear issue in this group's teams or projects, at once, with this group's own key:
    `state` by its name in that team ("Done", "In Progress"), `priority` 0–4 (0 none, 1 urgent, 2
    high, 3 medium, 4 low; -1 leaves it), `title` a new title, `labels` names to add, comma-separated.
    Only what is given changes. Nothing here deletes or archives."""
    def do():
        record, client = _editable()
        issue = _held(record, client, identifier)
        team_id = (issue.get("team") or {}).get("id") or ""
        fields: dict = {}
        changed = []
        if state.strip():
            chosen = _one(client.states(team_id), state, NO_STATE)
            fields["stateId"] = chosen["id"]
            changed.append(f"state {(issue.get('state') or {}).get('name', '?')} → {_named(chosen['name'])}")
        if priority is not None and int(priority) >= 0:
            if int(priority) not in PRIORITIES:
                return "Priority is 0 (none) to 4 (low)."
            fields["priority"] = int(priority)
            changed.append(f"priority {PRIORITIES.get(issue.get('priority'), '?')} → {PRIORITIES[int(priority)]}")  # pyright: ignore[reportArgumentType, reportCallIssue]
        if title.strip():
            fields["title"] = title.strip()
            changed.append("title changed")
        wanted = [n.strip() for n in labels.split(",") if n.strip()]
        if wanted:
            known = client.labels(team_id)
            have = {str(x.get("id")) for x in ((issue.get("labels") or {}).get("nodes") or [])}
            added = []
            for name in wanted:
                label = _one(known, name, NO_LABEL)
                if label["id"] not in have:
                    have.add(label["id"])
                    added.append(_named(label["name"]))
            fields["labelIds"] = sorted(have)
            changed.append("labels + " + ", ".join(added) if added else "labels already there")
        if not fields:
            return NOTHING_TO_CHANGE
        client.update_issue(issue["id"], **fields)
        return f"{issue.get('identifier')}: " + "; ".join(changed) + "."
    return _run(do)


@tool(side_effect=True)
def linear_comment(identifier: str, body: str) -> str:
    """Add a comment to a Linear issue in this group's teams or projects, at once, with this group's
    own key, as the words were given."""
    def do():
        record, client = _editable()
        if not body.strip():
            return "A comment needs some words."
        issue = _held(record, client, identifier)
        client.comment(issue["id"], body.strip())
        return f"Commented on {issue.get('identifier')}."
    return _run(do)


@tool(side_effect=True)
def linear_create_issue(title: str, description: str = "", team: str = "") -> str:
    """File a new issue in one of this group's Linear teams, at once, with this group's own key.
    `team` is the team key or label, needed when the group has more than one. Say which issue it
    became."""
    def do():
        record, client = _editable()
        if not title.strip():
            return "An issue needs a title."
        chosen, team_id = _team(record, client, team)
        issue = client.create_issue(team_id, title.strip(), description.strip())
        return f"Filed {issue.get('identifier', '?')} in {chosen.identifier.upper()}: {issue.get('url', '')}".rstrip(": ")
    return _run(do)


linear_edit_tools = [linear_assign, linear_update, linear_comment, linear_create_issue]
