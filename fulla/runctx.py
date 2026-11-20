"""Which run a tool is serving right now, and who spoke to it.

Norns names the run in every tool task it dispatches. `wake.ServingNorns`
puts it here before handing the task to the SDK, and the SDK runs the
handler in a thread that inherits the context, so a tool can ask. A tool
that needs to know where it was called from (a group reminder that must
find its way back to the group) reads this and refuses when it is unset,
rather than guessing.

The speaker is the channel's knowledge: it sent the run, and it kept who
wrote the message. It lends that lookup here at startup (`speakers_from`),
so a tool can ask who asked without a model argument saying so. Depends on
nothing, so tools can import it freely.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar

_run: ContextVar[int | None] = ContextVar("fulla_current_run", default=None)
_speaker_of: Callable[[int], list[dict]] | None = None


def set_current_run(run_id: int | None):
    return _run.set(int(run_id) if run_id is not None else None)


def reset_current_run(token) -> None:
    _run.reset(token)


def current_run_id() -> int | None:
    return _run.get()


def speakers_from(lookup: Callable[[int], list[dict]] | None) -> None:
    """Where to ask who spoke to a run: a function of run id returning
    [{"name", "transport", "user_id"}], in order, several when a busy
    chat's messages were bundled into one run."""
    global _speaker_of
    _speaker_of = lookup


def current_speakers() -> list[dict]:
    """Who wrote the messages the current run is answering, if the channel
    knows; empty otherwise."""
    run_id = current_run_id()
    if run_id is None or _speaker_of is None:
        return []
    return list(_speaker_of(run_id) or [])


_origin_of: Callable[[int], str | None] | None = None


def origins_from(lookup: Callable[[int], str | None] | None) -> None:
    """Where to ask which chat a social run is answering: a function of run
    id returning the channel's address for it ("signal:group:…"), or None."""
    global _origin_of
    _origin_of = lookup


def current_address() -> str | None:
    """The chat the current run is answering, if the channel knows. This,
    never a model argument, is how a tool learns which group it serves."""
    run_id = current_run_id()
    if run_id is None or _origin_of is None:
        return None
    return _origin_of(run_id)


_agent: ContextVar[int | None] = ContextVar("fulla_current_agent", default=None)
_is_manager: Callable[[int], bool] | None = None


def set_current_agent(agent_id: int | None):
    return _agent.set(int(agent_id) if agent_id is not None else None)


def reset_current_agent(token) -> None:
    _agent.reset(token)


def current_agent_id() -> int | None:
    return _agent.get()


def manager_from(lookup: Callable[[int], bool] | None) -> None:
    """Where to ask whether an agent id is the manager's: the worker knows,
    from the definitions it joined with."""
    global _is_manager
    _is_manager = lookup


def current_is_manager() -> bool:
    """Whether the current tool call is the manager's, by the agent id core
    put on the task, never by anything the model said."""
    agent_id = current_agent_id()
    if agent_id is None or _is_manager is None:
        return False
    return bool(_is_manager(agent_id))
