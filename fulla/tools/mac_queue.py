"""Mac tasks queued while the Mac is off are runs, like reminders.

The Mac worker's tools exist in the catalog only while the Mac is connected,
and a tool call queued in Norns lives minutes, not a night with the lid shut.
So a task for an absent Mac becomes a run of the `fulla-mac-task` agent: it
sleeps on `wait`, which costs nothing, looks at its tool list again when it
wakes, and does the task the first time the `mac_` tools are there. The
manager never waits itself.
"""

# Module state is set by init() before any tool runs; it is None only before then.
# pyright: reportOptionalMemberAccess=false

from __future__ import annotations

import json
import re
from pathlib import Path

from norns import NornsClient

from fulla.norns_api import run_gone
from fulla.tooldef import tool
from fulla.tools.clock import local_now

MAC_TASK_AGENT = "fulla-mac-task"
TERMINAL = {"completed", "failed", "error", "cancelled"}

_client: NornsClient | None = None
_index: Path | None = None


def init(*, client: NornsClient, data_dir: Path):
    global _client, _index
    _client = client
    _index = data_dir / "mac_queue.json"


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
    """Whether Fulla queued this run; the channel relays Mac task runs only if she did."""
    return str(run_id) in _load()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "task"


@tool(side_effect=True)
def queue_for_mac(task: str) -> str:
    """Queue something for the owner's Mac to do when it is next connected: a Things to-do,
    an Apple reminder, a notification, an AppleScript action. Never an iMessage: draft
    that with `draft_imessage`. `task` is the instruction in the owner's words, with every detail the Mac tool
    will need. Only when no `mac_` tool is in your list; if one is, do it now instead."""
    queued_at = local_now()
    key = f"mac:{_slug(task)}:{int(queued_at.timestamp())}"
    result = _client.send_message(
        MAC_TASK_AGENT,
        f"Task: {task}\nQueued: {queued_at.isoformat(timespec='minutes')}",
        conversation_key=key,
    )
    index = _load()
    index[str(result.run_id)] = {"task": task, "queued_at": queued_at.isoformat(timespec="minutes")}
    _save(index)
    return f"Queued for the Mac (run {result.run_id}): {task}"


@tool
def list_mac_queue() -> str:
    """Tasks waiting for the owner's Mac to come back, oldest first."""
    index = _load()
    if not index:
        return "Nothing queued for the Mac."
    lines, still_open = [], {}
    for run_id, entry in index.items():
        try:
            run = _client.get_run(int(run_id))
        except Exception as e:
            if run_gone(e):
                # No such run: it will never do the task. Said once, here, then off the list.
                lines.append(f"run {run_id}: {entry['task']} (queued {entry['queued_at']}, LOST: Norns no "
                             f"longer has this run, so it will never be done; it is off the list after this)")
                continue
            still_open[run_id] = entry
            lines.append(f"run {run_id}: {entry['task']} (queued {entry['queued_at']}, status unknown)")
            continue
        if run.status in TERMINAL:
            continue
        still_open[run_id] = entry
        lines.append(f"run {run_id}: {entry['task']} (queued {entry['queued_at']})")
    if still_open != index:
        _save(still_open)
    return "\n".join(lines) if lines else "Nothing queued for the Mac."


@tool(side_effect=True)
def drop_mac_task(run_id: int) -> str:
    """Cancel a queued Mac task by its run id (from `list_mac_queue`), when the owner no
    longer wants it done."""
    index = _load()
    if str(run_id) not in index:
        return f"No queued Mac task with run id {run_id}."
    try:
        _client.reply(run_id, "drop")
    except Exception as e:
        return f"Could not drop run {run_id}: {e}"
    entry = index.pop(str(run_id))
    _save(index)
    return f"Dropped: {entry['task']}"
