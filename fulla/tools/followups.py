"""Fulla's own check-backs are runs, like reminders, but they never reach the owner.

A follow-up run of the `fulla-followup` agent sleeps on `wait` until due, then
`deliver_followup` posts "[scheduled] follow-up: <what>" into the manager's
standing conversation and the run finishes. The manager takes it from there:
looks at the thing, and tells the owner only if it is worth saying. The
channel relays nothing of the follow-up run itself; it is not an agent the
channel knows. The manager never sleeps.

A watch is a follow-up with no due time: `watch_environment` starts a run of
`fulla-watch`, a small model that polls one Bubbo sandbox or coding job with
`get_environment` / `get_coding_task` and `wait`, and delivers the same way
only once it has changed (ready, finished, failed, the pull request opened).
The manager is woken once per job instead of once per poll: each of its
turns resends its whole prompt, the watcher's few lines do not. All it adds
to the message is the state it saw, reduced to two words, so nothing the
coding agent wrote reaches the manager through it.
"""

# Module state is set by init() before any tool runs; it is None only before then.
# pyright: reportOptionalMemberAccess=false

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from norns import NornsClient, tool

from fulla import runctx, zones

FOLLOWUP_AGENT = "fulla-followup"
WATCH_AGENT = "fulla-watch"
MANAGER_AGENT = "fulla"
TERMINAL = {"completed", "failed", "error", "cancelled"}

_client: NornsClient | None = None
_index: Path | None = None
_tz = "UTC"
_conversation_key = ""


def init(*, client: NornsClient, data_dir: Path, timezone: str, conversation_key: str):
    global _client, _index, _tz, _conversation_key
    _client = client
    _index = data_dir / "followups.json"
    _tz = timezone
    _conversation_key = conversation_key


def _load() -> dict[str, dict]:
    if _index and _index.exists():
        return json.loads(_index.read_text() or "{}")
    return {}


def _save(index: dict[str, dict]):
    _index.parent.mkdir(parents=True, exist_ok=True)
    tmp = _index.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, indent=2))
    tmp.replace(_index)  # pyright: ignore[reportArgumentType]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "followup"


@tool(side_effect=True)  # pyright: ignore[reportCallIssue]
def schedule_followup(what: str, due_at: str) -> str:
    """Schedule your own check-back: at `due_at` you get a "[scheduled] follow-up" message
    saying `what`, and nothing reaches the owner unless you say so then. `what` says what to
    look at and why; `due_at` is an ISO 8601 local datetime (use `now`). Not for the owner's
    commitments; those are `schedule_reminder`."""
    try:
        due = zones.parse(due_at, "", ZoneInfo(_tz))
    except ValueError as e:
        return str(e)

    key = f"followup:{_slug(what)}:{int(due.timestamp())}"
    result = _client.send_message(
        FOLLOWUP_AGENT,
        f"Key: {key}\nDue: {due.isoformat(timespec='seconds')}",
        conversation_key=key,
    )
    index = _load()
    index[str(result.run_id)] = {"what": what, "due_at": due.isoformat(timespec="minutes"), "key": key}
    _save(index)
    return f"Follow-up set (run {result.run_id}): {what} at {zones.show(due)}"


@tool(side_effect=True)  # pyright: ignore[reportCallIssue]
def watch_environment(what: str, environment_id: str, job_id: str = "") -> str:
    """Have a coding sandbox watched instead of checking it yourself: you get one
    "[scheduled] follow-up" message saying `what`, with the state it reached, once it
    changes. With `job_id`: when that coding job ends (succeeded, failed, cancelled).
    Without: when the environment is no longer provisioning and no pull request is still
    being opened. `what` says what to do then, with the ids and anything you will need
    (the brief for the job)."""
    environment_id, job_id = environment_id.strip(), job_id.strip()
    if not environment_id:
        return "Give the environment id."
    key = f"watch:{_slug(job_id or environment_id)}:{int(time.time())}"
    result = _client.send_message(
        WATCH_AGENT,
        f"Key: {key}\nEnvironment: {environment_id}\nJob: {job_id or 'none'}",
        conversation_key=key,
    )
    index = _load()
    index[str(result.run_id)] = {"what": what, "due_at": "when it changes", "key": key,
                                 "watch": job_id or environment_id}
    _save(index)
    return f"Watching (run {result.run_id}): {what}"


@tool
def list_followups() -> str:
    """Your own check-backs still to come: what, and when."""
    index = _load()
    lines, still_open = [], {}
    for run_id, entry in index.items():
        if entry.get("delivered"):
            continue
        try:
            status = _client.get_run(int(run_id)).status
        except Exception:
            status = "unknown"
        if status in TERMINAL:
            continue
        still_open[run_id] = entry
        lines.append(f"run {run_id}: {entry['what']} at {entry['due_at']}")
    if still_open != index:
        _save(still_open)
    return "\n".join(lines) if lines else "No follow-ups pending."


@tool(side_effect=True)  # pyright: ignore[reportCallIssue]
def drop_followup(run_id: int) -> str:
    """Call off a follow-up by its run id (from `list_followups`), when it is no longer needed."""
    index = _load()
    entry = index.pop(str(run_id), None)
    if entry is None:
        return f"No pending follow-up with run id {run_id}."
    _save(index)
    return f"Dropped follow-up: {entry['what']}"


def _state(state: str) -> str:
    """A state as at most two plain words, or nothing: the watcher read it
    from tool output, so nothing longer gets through."""
    return " ".join(re.sub(r"[^a-z ]+", " ", state.lower()).split()[:2])


@tool(side_effect=True)  # pyright: ignore[reportCallIssue]
def deliver_followup(key: str, state: str = "") -> str:
    """Post this follow-up into the manager's conversation. `key` is the Key line of your
    message; `state`, for a watch, is the state you saw. Once only: the index says whether
    it has gone, so a retry cannot post twice."""
    index = _load()
    # The run delivering is the entry's own (the index is keyed by it), so the key the
    # model copied is only a fallback: a watcher that mangled it still delivers.
    run_id = str(runctx.current_run_id() or "")
    entry = index.get(run_id)
    if entry is None:
        run_id, entry = next(((r, e) for r, e in index.items() if e.get("key") == key), (None, None))
    if entry is None:
        return "Dropped: this follow-up was called off. Finish."
    if entry.get("delivered"):
        return "Already delivered. Finish."
    try:
        text = f"[scheduled] follow-up: {entry['what']}"
        if entry.get("watch") and (seen := _state(state)):
            text += f"\n(watched: {entry['watch']} is {seen})"
        _client.send_message(MANAGER_AGENT, text,
                             conversation_key=_conversation_key)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 409:
            return "Busy: the conversation has a turn in flight. Wait 60 seconds and try again."
        raise
    entry["delivered"] = True
    index[run_id] = entry  # pyright: ignore[reportArgumentType]
    _save(index)
    return f"Delivered: {entry['what']}"
