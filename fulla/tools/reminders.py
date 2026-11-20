"""Commitments are runs. Scheduling one starts a run of the reminder agent that
sleeps until it's due, asks, and closes when the owner says so.

The manager never waits on these itself: a parked run treats the next message
as its answer, so the thing that chats must not be the thing that sleeps.
"""

# Module state is set by init() before any tool runs; it is None only before then.
# pyright: reportOptionalMemberAccess=false

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from norns import NornsClient

from fulla import zones
from fulla.norns_api import NornsApi, run_gone
from fulla.tooldef import tool

logger = logging.getLogger(__name__)

REMINDER_AGENT = "fulla-reminder"
TERMINAL = {"completed", "failed", "error", "cancelled"}

_client: NornsClient | None = None
_api: NornsApi | None = None
_index: Path | None = None
_tz = "UTC"


def init(*, client: NornsClient, data_dir: Path, timezone: str, api: NornsApi | None = None):
    """`api` is the REST client, for the one thing the SDK's cannot do: cancel a run."""
    global _client, _api, _index, _tz
    _client = client
    _api = api
    _index = data_dir / "reminders.json"
    _tz = timezone


def _load() -> dict[str, dict]:
    if _index and _index.exists():
        return json.loads(_index.read_text() or "{}")
    return {}


def _save(index: dict[str, dict]):
    _index.parent.mkdir(parents=True, exist_ok=True)
    tmp = _index.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, indent=2))
    tmp.replace(_index)  # pyright: ignore[reportArgumentType]


def known(run_id: int) -> bool:
    """Whether Fulla scheduled this run. The channel relays reminder runs only
    if she did; a run of the reminder agent started some other way is not hers."""
    return str(run_id) in _load()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "reminder"


@tool(side_effect=True)
def schedule_reminder(what: str, due_at: str, tz: str = "") -> str:
    """Schedule a reminder. `what` is the commitment in the owner's words; `due_at` is an
    ISO 8601 datetime (use `now` to work it out), read in the owner's timezone. When the
    owner names another zone ("11am PT", "3pm London time"), give the time as they said it
    and pass the zone as `tz`, an IANA name like America/Vancouver; never convert it
    yourself. Check the time in the reply. Returns the reminder's run id."""
    try:
        due = zones.parse(due_at, tz, ZoneInfo(_tz))
    except ValueError as e:
        return str(e)

    key = f"reminder:{_slug(what)}:{int(due.timestamp())}"
    result = _client.send_message(
        REMINDER_AGENT,
        f"What: {what}\nDue: {due.isoformat(timespec='minutes')}",
        conversation_key=key,
    )
    index = _load()
    index[str(result.run_id)] = {"what": what, "due_at": due.isoformat(timespec="minutes")}
    _save(index)
    return f"Scheduled (run {result.run_id}): {what} at {zones.show(due, tz)}"


@tool
def list_reminders() -> str:
    """List open reminders: what, when, and whether each is sleeping or waiting for an answer.
    One marked LOST will never ask and is shown this once only."""
    index = _load()
    if not index:
        return "No open reminders."
    lines, still_open = [], {}
    for run_id, entry in index.items():
        try:
            run = _client.get_run(int(run_id))
        except Exception as e:
            if run_gone(e):
                # No such run: it can never ask. Said once, here, then off the list.
                logger.warning("reminder run %s (%s, due %s) is gone from Norns; dropping it",
                               run_id, entry["what"], entry["due_at"])
                lines.append(f"run {run_id}: {entry['what']} at {entry['due_at']} (LOST: Norns no longer "
                             f"has this run, so it will never ask; it is off the list after this)")
                continue
            still_open[run_id] = entry
            lines.append(f"run {run_id}: {entry['what']} at {entry['due_at']} (status unknown)")
            continue
        if run.status in TERMINAL:
            continue
        still_open[run_id] = entry
        state = "waiting for your answer" if run.status == "waiting" else "sleeping"
        lines.append(f"run {run_id}: {entry['what']} at {entry['due_at']} ({state})")
    if still_open != index:
        _save(still_open)
    return "\n".join(lines) if lines else "No open reminders."


@tool(side_effect=True)
def drop_reminder(run_id: int) -> str:
    """Close a reminder by its run id (from `list_reminders`), when the owner says it is
    done or no longer wanted. It stops asking."""
    index = _load()
    if str(run_id) not in index:
        return f"No open reminder with run id {run_id}."
    try:
        _api.cancel_run(run_id)  # False when Norns has no such run, which is closed enough
    except Exception as e:
        return f"Could not close run {run_id}: {e}"
    entry = index.pop(str(run_id))
    _save(index)
    return f"Closed: {entry['what']}"
