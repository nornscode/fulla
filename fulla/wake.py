"""Fulla's worker as a serverless worker: asleep until Norns wakes it.

Norns POSTs a signed wake (server.py receives it) when a task needs one of
Fulla's tools and no worker holding them is connected. `Incarnations` is the
handshake between that request and the worker: the request asks for an
incarnation and is held open until it ends; `run_incarnations` brings the
ordinary SDK worker up with the declared worker id, serves until it has been
idle for a while, drains, and marks the incarnation ended. On Fly the open
inbound request is what keeps the machine alive; the worker's outbound
WebSocket does not count. See norns/docs/serverless-workers.md.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Sequence

from norns import Norns

from fulla.coding import CodingGuard
from fulla.groups import GROUP_AGENT_PREFIX
from fulla.norns_api import NornsApi
from fulla.runctx import (
    manager_from,
    reset_current_agent,
    reset_current_run,
    set_current_agent,
    set_current_run,
)
from fulla.schedule import MARKER, TURNS
from fulla.tools.followups import FOLLOWUP_AGENT, WATCH_AGENT
from fulla.tools.group_reminders import GROUP_REMINDER_AGENT
from fulla.tools.web import READER_AGENT
from fulla.worker import MANAGER_AGENT, SOCIAL_AGENT, scheduled_playbook, turn_note, turn_offers

log = logging.getLogger("fulla.wake")

# Norns offers its built-ins to every agent and no allowlist removes them.
# These agents run with nobody to answer and a caller waiting, so the worker
# takes the built-ins they must never call out of their LLM calls. The
# channel unsticks a run that calls `ask_human` anyway; this keeps it from
# being offered at all. The social agent talks in conversations other people
# are using: a `wait` there would hold the conversation busy and queue
# everyone's next message behind it, so it never sees `wait` (a timed thing
# is `schedule_group_reminder`); the reader is still reachable.
WITHHELD = {
    # The manager never sleeps: anything later is a reminder or a follow-up,
    # and a manager parked on `wait` refuses every owner message as busy.
    MANAGER_AGENT: frozenset({"wait"}),
    READER_AGENT: frozenset({"ask_human", "wait", "launch_agent", "list_agents"}),
    FOLLOWUP_AGENT: frozenset({"ask_human", "launch_agent", "list_agents"}),
    WATCH_AGENT: frozenset({"ask_human", "launch_agent", "list_agents"}),
    SOCIAL_AGENT: frozenset({"ask_human", "wait", "list_agents"}),
    GROUP_REMINDER_AGENT: frozenset({"ask_human", "launch_agent", "list_agents"}),
}


# A scheduled turn has nobody waiting on it. A manager parked on `ask_human`
# there holds the standing conversation, and whatever arrives next, the
# owner's message or the next timer, is read as the answer. So on those turns
# the manager is not offered it; the channel answers one that asks anyway.
WITHHELD_ON_SCHEDULED = {MANAGER_AGENT: frozenset({"ask_human"})}

# The scheduled turns that are never delivered: they look after memory and
# her own code, and end "[silent]". A mid-turn `send_message` would reach the
# owner past that, so on these it is not offered. The other scheduled turns
# (the brief, a check-in, a question, a follow-up, the owner's own timers)
# are meant to reach the owner when they have something to say, and keep it.
QUIET_TURNS = (TURNS["nightly-review"][0], TURNS["shipping-check"][0])
WITHHELD_ON_QUIET = {MANAGER_AGENT: frozenset({"send_message"})}


# Typing into a terminal runs what is typed, and a timer has nobody behind it:
# on a scheduled turn of the manager's, a follow-up and the owner's own timers
# included, no machine's typing tools are offered. By suffix, since each
# machine names its own.
TYPING = ("_tmux_send", "_tmux_new", "_tmux_rename")


def _turn_message(messages: list[dict] | None) -> str:
    """The newest user message, which started the turn these messages end
    in. An answer to `ask_human` comes back as a tool result, so it never
    starts a turn. A message of the owner's that joins a scheduled run makes
    the rest of it their turn: they are there now, and what they ask for may
    take any tool."""
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            content = msg.get("content")
            return content.lstrip() if isinstance(content, str) else ""
    return ""


def scheduled_turn(messages: list[dict] | None) -> bool:
    """Whether the turn these messages end in was started by a timer: its
    newest user message begins with the marker."""
    return _turn_message(messages).startswith(MARKER)


def quiet_turn(messages: list[dict] | None) -> bool:
    """Whether the turn these messages end in is one that is never
    delivered (`QUIET_TURNS`)."""
    return _turn_message(messages).lower().startswith(QUIET_TURNS)


def withheld_for(name: str, messages: list[dict] | None = None) -> frozenset[str]:
    """What is withheld from an agent by name, on the turn `messages` end in.
    Each group's agent is the social agent drawn per group, and is withheld
    the same."""
    if name.startswith(GROUP_AGENT_PREFIX):
        return WITHHELD[SOCIAL_AGENT]
    withheld = WITHHELD.get(name, frozenset())
    if name in WITHHELD_ON_SCHEDULED and scheduled_turn(messages):
        withheld = withheld | WITHHELD_ON_SCHEDULED[name]
    if name in WITHHELD_ON_QUIET and quiet_turn(messages):
        withheld = withheld | WITHHELD_ON_QUIET[name]
    return withheld


def without_tools(task: dict, names: frozenset[str]) -> dict:
    """The task with the named tools left out of its list. The task core
    sent is not changed."""
    tools = task.get("tools") or []
    kept = [t for t in tools if (t["name"] if isinstance(t, dict) else str(t)) not in names]
    if len(kept) == len(tools):
        return task
    return dict(task, tools=kept)


def _names(task: dict) -> list[str]:
    return [t["name"] if isinstance(t, dict) else str(t) for t in task.get("tools") or []]


def offered(name: str | None, task: dict) -> dict:
    """The task with the tools this agent is offered on the turn its messages
    end in: less what is withheld from it, and for the manager on a scheduled
    turn, less the typing tools and down to that turn's own set where it has
    one (`worker.TURN_TOOLS`). An agent whose name is not known is offered
    what core sent. The task core sent is not changed."""
    if not name or task.get("purpose") == "compact":
        return task
    messages = task.get("messages")
    drop = set(withheld_for(name, messages))
    if name == MANAGER_AGENT and scheduled_turn(messages):
        message = _turn_message(messages)
        drop |= {n for n in _names(task) if n.endswith(TYPING) or not turn_offers(message, n)}
    return without_tools(task, frozenset(drop))


def for_turn(name: str | None, task: dict, hosts: Sequence[str] = (), owner: str = "the owner") -> dict:
    """The LLM task as the model gets it: the tools this agent is offered on
    this turn, in a stable order, and for the manager the per-turn note, which
    is told what core sent so it can tell a machine that is off from one whose
    tools this turn does not carry."""
    return in_stable_order(with_turn_note(offered(name, task), hosts, owner, connected=_names(task)))


def in_stable_order(task: dict) -> dict:
    """The task with its tools sorted by name. Core lists them in the order
    the workers serving them happened to join, which changes from one call to
    the next. The tool list is the front of what the provider caches, so a
    reorder alone has the whole prompt written to the cache again instead of
    read back from it. The task core sent is not changed."""
    tools = task.get("tools") or []
    ordered = sorted(tools, key=lambda t: t["name"] if isinstance(t, dict) else str(t))
    return task if ordered == tools else dict(task, tools=ordered)


def with_turn_note(task: dict, hosts: Sequence[str] = (), owner: str = "the owner",
                   connected: Sequence[str] | None = None) -> dict:
    """The manager's LLM call with what belongs to this turn alone appended to
    its prompt: the per-turn note, and on a scheduled turn that has
    instructions of its own (`worker.PLAYBOOKS`), those. Only for the
    manager's own turns: compaction calls and the reminder agent are left
    alone. The SDK appends the summary and date after this. `hosts` are the
    machines besides the Mac the note reports on; `connected` is every tool
    core sent, when the turn was offered fewer."""
    if task.get("purpose") == "compact" or not task.get("system_prompt"):
        return task
    names = _names(task)
    if not any(n.startswith("mac_") for n in (*names, *(connected or ()))) and "mac_" not in task["system_prompt"]:
        return task
    task = dict(task)
    task["system_prompt"] = task["system_prompt"].rstrip() + "\n\n" + turn_note(names, hosts, connected)
    playbook = scheduled_playbook(_turn_message(task.get("messages")), owner)
    if playbook:
        task["system_prompt"] += "\n\n" + playbook
    return task


def declare_agent(url: str, api_key: str, agent) -> None:
    """Create or update one agent definition on the server now, as a join
    would: the SDK's own upsert, on the worker class that has it (the REST
    client does not). The channel and the owner's group tools use this when
    a group changes between wakes."""
    Norns(url, api_key=api_key)._ensure_agent(agent)


class ServingNorns(Norns):
    """The SDK worker with a pulse: it knows when it last did anything and
    how many tasks it holds, so the watchdog can tell idle from busy. It also
    appends the manager's per-turn note and, on a scheduled turn, that turn's
    instructions (see `with_turn_note`), keeps
    the built-ins in `WITHHELD` from the agents they are withheld from, and,
    with `coding`, holds every Bubbo call the model makes to its scope
    (`coding.CodingGuard`) before Norns sees it."""

    def __init__(self, *args, hosts: Callable[[], list[str]] = list, coding: CodingGuard | None = None,
                 owner: str = "the owner", **kwargs):
        super().__init__(*args, **kwargs)
        self.hosts = hosts  # the machines besides the Mac, read fresh each turn
        self.owner = owner  # their name, for a scheduled turn's instructions
        self.coding = coding
        self._agent_names: dict[int, str] | None = None  # agent id -> name, for what is withheld
        self.last_activity = time.monotonic()
        self.in_flight = 0
        self._pulse = threading.Lock()

    def _begin(self):
        with self._pulse:
            self.in_flight += 1
            self.last_activity = time.monotonic()

    def _end(self):
        with self._pulse:
            self.in_flight -= 1
            self.last_activity = time.monotonic()

    async def _run_llm_task(self, ws, payload):
        self._begin()
        try:
            return await super()._run_llm_task(ws, payload)
        finally:
            self._end()

    async def _handle_llm_task(self, task):
        name = await self._agent_name(task)
        result = await super()._handle_llm_task(for_turn(name, task, self.hosts(), self.owner))
        if self.coding is not None and task.get("purpose") != "compact":
            # The Bubbo tools are not ours to serve, so their scope is held
            # here, on the model's calls; an unknown agent is no scope at all.
            result = self.coding.check(name, task.get("messages"), result)
        return result

    async def _agent_name(self, task: dict) -> str | None:
        """The name of the agent this call is for. Core puts the agent's id
        on every LLM task; the names are resolved once per incarnation, after
        join has upserted the definitions, and again for an id not seen yet
        (a group allowed since). Until that lookup works, the name is unknown:
        nothing is withheld, the channel's unstick is the guard, and the
        coding guard refuses."""
        agent_id = task.get("agent_id")
        if agent_id is None:
            return None
        if self._agent_names is None or agent_id not in self._agent_names:
            try:
                agents = await asyncio.to_thread(self._list_agents)
            except Exception as e:
                log.warning("could not resolve agent ids; withholding nothing this call: %s", e)
                return (self._agent_names or {}).get(agent_id)
            self._agent_names = {a.id: a.name for a in agents}
        return self._agent_names.get(agent_id)

    def _list_agents(self):
        return NornsApi(self.url, self.api_key).list_agents()

    def _is_manager(self, agent_id: int) -> bool:
        """Whether an agent id is the manager's, resolved once per incarnation
        from the server, so a tool can tell the owner's own calls from a
        group's (runctx.current_is_manager). Unknown means no."""
        if getattr(self, "_manager_id", None) is None:
            try:
                self._manager_id = next((a.id for a in self._list_agents() if a.name == MANAGER_AGENT), None)
            except Exception as e:
                log.warning("could not resolve the manager's id: %s", e)
                return False
        return self._manager_id is not None and agent_id == self._manager_id

    async def _run_tool_task(self, ws, payload, tools_by_name):
        self._begin()
        # The task names its run and its agent; a tool that must know where it
        # was called from (runctx.current_run_id, current_is_manager) reads
        # them from the context the SDK's handler thread inherits.
        token = set_current_run(payload.get("run_id"))
        atoken = set_current_agent(payload.get("agent_id"))
        manager_from(self._is_manager)
        try:
            return await super()._run_tool_task(ws, payload, tools_by_name)
        finally:
            reset_current_agent(atoken)
            reset_current_run(token)
            self._end()

    def idle_for(self) -> float:
        with self._pulse:
            return 0.0 if self.in_flight else time.monotonic() - self.last_activity


class Incarnations:
    """One worker at a time. A wake either starts an incarnation or finds one
    running; either way the request is held until that incarnation ends."""

    def __init__(self):
        self.requested = threading.Event()
        self.ended = threading.Condition()
        self.generation = 0
        self.awake = False

    def wait_for_end_of_current(self):
        with self.ended:
            gen = self.generation
            self.requested.set()
            while self.generation == gen:
                self.ended.wait()

    def finish(self):
        self.requested.clear()
        with self.ended:
            self.generation += 1
            self.ended.notify_all()


def watchdog(norns: ServingNorns, started: float, idle: float, max_lifetime: float, stop: threading.Event):
    while not stop.wait(2):
        if norns.idle_for() >= idle:
            log.info("idle for %.0fs; draining", idle)
            norns.shutdown()
            return
        if time.monotonic() - started >= max_lifetime:
            log.info("up for %.0fs; draining", max_lifetime)
            norns.shutdown()
            return


def run_incarnations(incarnations: Incarnations, *, url: str, api_key: str, agents: Callable[[], list],
                     worker_id: str, idle: float, max_lifetime: float,
                     hosts: Callable[[], list[str]] = list, coding: CodingGuard | None = None,
                     owner: str = "the owner") -> None:
    """Block forever, bringing a worker up for each requested incarnation.

    `agents` builds the definitions, and is called afresh for each incarnation:
    the SDK upserts every definition on join, so a list built once at process
    start would put the prompt as it was then back on the server, undoing a
    soul or directive change pushed over REST in the meantime."""
    log.info("asleep as %s (idle %.0fs, max %.0fs)", worker_id, idle, max_lifetime)
    while True:
        incarnations.requested.wait()
        incarnations.awake = True
        norns = ServingNorns(url, api_key=api_key, hosts=hosts, coding=coding, owner=owner)
        stop = threading.Event()
        threading.Thread(
            target=watchdog, args=(norns, time.monotonic(), idle, max_lifetime, stop), daemon=True
        ).start()
        log.info("worker up")
        try:
            # Blocks until drained. A long shutdown timeout so a task in
            # flight when the cap lands still gets to report its result.
            norns.run(agents(), worker_id=worker_id, shutdown_timeout=120)
        except Exception:
            log.exception("worker loop failed")
        finally:
            stop.set()
            incarnations.awake = False
            incarnations.finish()
        log.info("worker stopped; asleep again")
