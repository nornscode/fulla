"""Reminders for a friends' chat or a group: runs of their own, like the
owner's, so the social agent never sleeps.

The social agent answers in a conversation that other people are talking
in. If it called `wait`, the conversation would be busy until the timer
ended and everyone's next message would queue behind it; to the group she
would have gone deaf. So a timed thing in a group is a run of
`fulla-group-reminder`, which sleeps and then finishes with one line, and
the channel posts that line back into the group the reminder came from.

Finding the group back is the one trick: a tool does not know which
conversation called it, but Norns names the run in every tool task, and
`runctx` keeps it. The index records the reminder's run against the run
that scheduled it; the channel knows where that run's answers go.

Where the index lives: in the group's own directory (`reminders.json` under
`data/groups/<id>/`) when the channel says the run is a group's and the
group has a directory; otherwise, for a friend's one-to-one chat, in the
shared `group_reminders.json`. `known` looks in all of them.

A check-back (`schedule_group_followup`, for a group with "coding" on) rides
the same carrier: a `fulla-group-reminder` run indexed with `followup`, which
the channel answers not by posting a line but by sending "[scheduled]
follow-up" into the group's own conversation, so the group's agent looks
again (at a coding job) without ever sleeping.
"""

# Module state is set by init() before any tool runs; it is None only before then.
# pyright: reportOptionalMemberAccess=false

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from norns import NornsClient

from fulla import runctx, zones
from fulla.groups import GroupStore
from fulla.people import PeopleStore
from fulla.tooldef import tool

GROUP_REMINDER_AGENT = "fulla-group-reminder"

_client: NornsClient | None = None
_index: Path | None = None
_groups: GroupStore | None = None
_people: PeopleStore | None = None
_tz = "UTC"


def init(*, client: NornsClient, data_dir: Path, timezone: str, groups: GroupStore | None = None,
         people: PeopleStore | None = None):
    global _client, _index, _groups, _people, _tz
    _client = client
    _index = data_dir / "group_reminders.json"
    _groups = groups
    _people = people
    _tz = timezone


def _load(path: Path | None = None) -> dict[str, dict]:
    path = path or _index
    if path and path.exists():
        return json.loads(path.read_text() or "{}")
    return {}


def _save(index: dict[str, dict], path: Path | None = None):
    path = path or _index
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, indent=2))
    tmp.replace(path)  # pyright: ignore[reportArgumentType]


def _indexes() -> list[Path]:
    """Every place a reminder may be indexed: each group's own file, then the shared one."""
    paths = [_groups.reminders_path(r.id) for r in _groups.all()] if _groups is not None else []
    return paths + ([_index] if _index is not None else [])


def _index_for_current_run() -> Path:
    """The group's own index when the channel says this run is a group's
    and the group has a directory; the shared one otherwise."""
    address = runctx.current_address()
    if _groups is not None and address:
        record = _groups.by_address(address)
        if record is not None:
            return _groups.reminders_path(record.id)
    return _index  # pyright: ignore[reportReturnType]


def known(run_id: int) -> dict | None:
    """The reminder Fulla scheduled under this run id, with the run that
    scheduled it (`parent`), or None: a run of the agent started some other
    way is not hers and is not relayed."""
    for path in _indexes():
        entry = _load(path).get(str(run_id))
        if entry is not None:
            return entry
    return None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "reminder"


def _member(name: str) -> dict | None:
    """A member of the group this run is answering, by name: someone the
    channel has heard speak there, found in the people store through the
    run's address. The id is the one Signal shows (an account id) when the
    person has one, else their number."""
    address = runctx.current_address()
    if _people is None or not address:
        return None
    transport = address.split(":", 1)[0]
    for person in _people.members(address):
        if person.name.lower() == name and person.ids.get(transport):
            ids = person.ids[transport]
            return {"name": person.name, "transport": transport,
                    "user_id": next((i for i in ids if not i.startswith("+")), ids[0])}
    return None


def _asker(name: str) -> dict | None:
    """Who the reminder is for: the one person who spoke to this run, or,
    when several did or someone else is meant, the one `name` picks out of
    those who spoke or of the group's members. Never anyone the channel
    has not heard in this chat; a name that matches nobody means no tag."""
    speakers = runctx.current_speakers()
    wanted = name.strip().lower()
    if wanted:
        spoke = next((s for s in speakers if s.get("name", "").lower() == wanted), None)
        return spoke or _member(wanted)
    return speakers[0] if len(speakers) == 1 else None


@tool(side_effect=True)
def schedule_group_reminder(what: str, due_at: str = "", tz: str = "", asked_by: str = "",
                            in_minutes: int = 0) -> str:
    """Post a reminder into this chat at a given time, tagging the person it is for. `what` is
    the reminder in their words. For "in 3 minutes" or "in an hour" give `in_minutes` and no
    `due_at`; otherwise `due_at` is an ISO 8601 datetime worked out from `now` called on this
    turn, never from a time you saw earlier (a time already passed is refused). `asked_by` is the
    name of the person to tag: leave it empty to tag whoever asked, or give a name when more than
    one person's message reached you this turn, or when the reminder is for someone else in this
    group ("remind @Sam to …" tags Sam). Only people heard in this chat can be tagged. Never use
    `wait` for this: a reminder is its own run, and you stay free to talk. Returns the reminder's
    run id, which `drop_group_reminder` takes."""
    parent = runctx.current_run_id()
    if parent is None:
        return "I can't tell which chat this is from here, so I can't set that."
    now = datetime.now(ZoneInfo(_tz))
    if in_minutes > 0:
        due = (now + timedelta(minutes=in_minutes)).replace(second=0, microsecond=0)
    elif not due_at.strip():
        return "Say when: `in_minutes` for a relative time, or `due_at` worked out from `now`."
    else:
        try:
            due = zones.parse(due_at, tz, ZoneInfo(_tz))
        except ValueError as e:
            return str(e)
        if due < now - timedelta(seconds=60):
            # The model did its arithmetic from a clock it read turns ago.
            return (f"{zones.show(due, tz)} has already passed; it is {zones.show(now)}. Call `now` and work "
                    "it out again, or give `in_minutes`.")
    key = f"group-reminder:{_slug(what)}:{int(due.timestamp())}"
    result = _client.send_message(
        GROUP_REMINDER_AGENT,
        f"What: {what}\nDue: {due.isoformat(timespec='minutes')}",
        conversation_key=key,
    )
    asker = _asker(asked_by)  # the channel tags them when it posts
    path = _index_for_current_run()
    index = _load(path)
    index[str(result.run_id)] = {"what": what, "due_at": due.isoformat(timespec="minutes"), "parent": parent,
                                 "asked_by": asker}
    _save(index, path)
    who = f", tagging {asker['name']}" if asker else ""
    return f"Set (run {result.run_id}): {what} at {zones.show(due, tz)}. I'll post it here{who}."


FOLLOWUP_MAX_MINUTES = 60


@tool(side_effect=True)
def schedule_group_followup(what: str, in_minutes: int) -> str:
    """Your own check-back in this chat, for a coding job you cannot wait on: in `in_minutes`
    (1 to 60) you get a "[scheduled] follow-up" message saying `what`, in this conversation, and
    act on it then. `what` says what to look at and what to do next, with the sandbox id, the job id
    and, before a job has started, the whole brief for the coding agent: you will remember nothing
    else. Nothing is posted to the chat by the check-back itself; what you answer then is. Not for
    people's reminders; those are `schedule_group_reminder`. Never `wait` instead."""
    parent = runctx.current_run_id()
    if parent is None or _groups is None or not runctx.current_address() \
            or _groups.by_address(runctx.current_address()) is None:  # pyright: ignore[reportArgumentType]
        return "I can only set a check-back in a group that is set up for it."
    minutes = max(1, min(int(in_minutes or 0), FOLLOWUP_MAX_MINUTES))
    due = (datetime.now(ZoneInfo(_tz)) + timedelta(minutes=minutes)).replace(microsecond=0)
    key = f"group-followup:{_slug(what)}:{int(due.timestamp())}"
    result = _client.send_message(
        GROUP_REMINDER_AGENT,
        f"What: check back: {what}\nDue: {due.isoformat(timespec='seconds')}",
        conversation_key=key,
    )
    path = _index_for_current_run()
    index = _load(path)
    index[str(result.run_id)] = {"what": what, "due_at": due.isoformat(timespec="minutes"), "parent": parent,
                                 "followup": True}
    _save(index, path)
    return f"Check-back set (run {result.run_id}) for {zones.show(due)}: {what}"


@tool(side_effect=True)
def drop_group_reminder(run_id: int) -> str:
    """Cancel a reminder or check-back set in this chat, by the run id it was set with."""
    for path in _indexes():
        index = _load(path)
        entry = index.pop(str(run_id), None)
        if entry is not None:
            break
    else:
        return f"No reminder with run id {run_id} was set here."
    try:
        # NornsClient has no cancel_run (NornsApi does, as reminders.py uses); the
        # fakes in tests do, so only production takes the except below.
        _client.cancel_run(run_id)  # pyright: ignore[reportAttributeAccessIssue]
    except Exception as e:
        return f"I dropped it from my list but Norns would not cancel run {run_id}: {e}"
    _save(index, path)
    return f"Dropped: {entry['what']}."
