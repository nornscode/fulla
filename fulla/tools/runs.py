"""One Norns run, looked up by id, for the owner who asks what happened in it.

Owner-only by being the manager's. It reads through the SDK client the
other run tools hold (`get_run`, `get_events`, `get_agent`), so it sees
what the tenant's key sees and nothing else: a run of another tenant is a
404, the same as a run that never was. What the run said, called and got
back was written by models, tools and, in a group's run, strangers, so all
of it comes inside the untrusted marker; status, agent and times are facts.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from norns import NornsClient

from fulla import zones
from fulla.tooldef import tool
from fulla.untrusted import untrusted

TERMINAL = {"completed", "failed", "error", "cancelled"}
NO_CLIENT = "I can't reach Norns from here."
NO_ANSWER = "Norns didn't answer."
SNIPPET = 160  # chars kept of any one piece of the run's text
LINES = 12  # lines of the run's own text, the newest kept
EVENTS = 500  # events read, the newest kept
TOOLS = 10  # distinct tool names listed, the most called first
BODY = 3000  # chars of the run's own text, all told

_client: NornsClient | None = None
_tz = "UTC"


def init(*, client: NornsClient, timezone: str):
    global _client, _tz
    _client, _tz = client, timezone


def _clip(text, n: int = SNIPPET) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _when(stamp: str | None) -> str:
    if not stamp:
        return "not yet"
    try:
        dt = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp
    return zones.show(dt.astimezone(ZoneInfo(_tz))) if dt.tzinfo else stamp


def _tool_calls(event_type: str, payload: dict, seq: int) -> list[tuple[str, str]]:
    """(call key, tool name) for each tool call this event names. Norns keeps the
    name under `name`, and one call shows up in several events (the model's
    `tool_calls`, then the call, then its result), so the key is the call's
    `tool_call_id` wherever Norns gives one; an event without one counts only
    if it is the call itself."""
    calls = []
    for call in payload.get("tool_calls") or []:
        if isinstance(call, dict) and isinstance(call.get("name"), str) and call["name"]:
            calls.append((str(call.get("id") or call.get("tool_call_id") or f"{seq}:{len(calls)}"), call["name"]))
    name = payload.get("name")
    if event_type.startswith("tool") and isinstance(name, str) and name:
        key = payload.get("tool_call_id") or payload.get("id")
        if key:
            calls.append((str(key), name))
        elif event_type == "tool_call":
            calls.append((f"{seq}:call", name))
    return calls


def _error(event_type: str, payload: dict) -> str | None:
    err = payload.get("error")
    if isinstance(err, dict):
        err = err.get("message") or err
    if err:
        return _clip(err)
    if payload.get("is_error") or "fail" in event_type or "error" in event_type:
        return _clip(payload.get("content") or payload.get("reason") or payload.get("message") or event_type)
    return None


def _text(payload: dict) -> str | None:
    for key in ("content", "text", "message", "question", "result", "output"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(value)
    return None


@tool
def norns_run(run_id: int) -> str:
    """What happened in one Norns run, by its id ("what happened in run 938?"): its status,
    which agent ran it, when it was created, started and finished, which tools it called,
    any errors, and the last of what it said. The run's own text comes marked as untrusted."""
    if _client is None:
        return NO_CLIENT
    try:
        run = _client.get_run(int(run_id))
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (403, 404):
            return f"No run {run_id} on my Norns."
        return f"Norns said {e.response.status_code}."
    except (httpx.HTTPError, ValueError):
        return NO_ANSWER
    try:
        agent = _client.get_agent(run.agent_id).name
    except Exception:
        agent = f"agent {run.agent_id}"
    try:
        events = sorted(_client.get_events(run.run_id), key=lambda e: e.sequence)
    except Exception:
        events = None
    total = len(events or [])
    # The SDK's get_events takes no limit, so the bound is here: only the
    # newest EVENTS are read, and the count says how many were left.
    kept = (events or [])[-EVENTS:]

    calls: dict[str, str] = {}
    errors: list[str] = []
    said: list[str] = []
    for e in kept:
        payload = e.payload if isinstance(e.payload, dict) else {}
        for key, name in _tool_calls(e.event_type, payload, e.sequence):
            calls.setdefault(key, name)
        err = _error(e.event_type, payload)
        if err:
            errors.append(f"{e.event_type}: {err}")
        elif (text := _text(payload)) is not None:
            said.append(f"{e.event_type}: {text}")
    started = events[0].inserted_at if events else None
    finished = events[-1].inserted_at if events and run.status in TERMINAL else None

    lines = [f"Run {run.run_id}: {run.status}, agent {agent}, started by {run.trigger_type}.",
             f"Created {_when(run.inserted_at)}; started {_when(started)}; finished {_when(finished)}."]
    if events is None:
        lines.append("Its events could not be loaded, so I can't say which tools it called or whether it hit errors.")
    else:
        lines.append(f"{total} events" + (f", the newest {EVENTS} read." if total > EVENTS else "."))
        if not errors:
            lines.append("No errors" + (" in those." if total > EVENTS else "."))
    if run.waiting_for:
        lines.append("Waiting on a question.")

    # Everything below came from the run's events or output, which models,
    # tools and strangers wrote: tool names included.
    body = []
    if events is not None:
        tools = Counter(calls.values()).most_common()
        shown = ", ".join(f"{_clip(n, 60)} ×{c}" if c > 1 else _clip(n, 60) for n, c in tools[:TOOLS])
        more = f", +{len(tools) - TOOLS} more" if len(tools) > TOOLS else ""
        body.append(f"Tools called: {shown or 'none'}{more}.")
    if errors:
        body += ["Errors:"] + errors[-5:]
    if said:
        skipped = len(said) - LINES
        body += (["Last of what it said:"] + ([f"({skipped} earlier lines left out)"] if skipped > 0 else [])
                 + said[-LINES:])
    if run.output:
        body.append(f"Output: {_clip(run.output, 400)}")
    text = "\n".join(body)
    if len(text) > BODY:
        text = text[: BODY - 1] + "…"
    if text:
        lines.append(untrusted("run text", f"Norns run {run.run_id}", text))
    return "\n".join(lines)
