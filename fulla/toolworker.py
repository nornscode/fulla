"""A tools-only Norns worker, shared by `fulla-mac` and `fulla-host`.

It joins Norns with capabilities ["tools"] and no agent definitions, so it
never serves an LLM call and never touches the manager's definition on the
server; that is owned by the Fly worker. The SDK groups tools by Agent, so
the tools ride on a carrier Agent that exists only in this process.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from norns import Agent, Norns

from fulla.runctx import reset_current_run, set_current_run


class ToolWorker(Norns):
    """Serves tools and nothing else. `on_join`, if given, runs in a thread
    after every successful join (the Mac worker uses it to nudge the tasks
    that queued up while it was away), so the worker's own loop is not held up.
    Each tool task's run id is lent to the tool through runctx, as the Fly
    worker does, so the shell can ask Norns what started the run it serves;
    without this every shell call looked like no run at all and was refused."""

    def __init__(self, *args, on_join: Callable[[], None] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_join = on_join

    def _check_join_reply(self, resp_data):
        super()._check_join_reply(resp_data)
        if self._on_join is not None:
            threading.Thread(target=self._on_join, daemon=True).start()

    async def _run_tool_task(self, ws, payload, tools_by_name):
        token = set_current_run(payload.get("run_id"))
        try:
            return await super()._run_tool_task(ws, payload, tools_by_name)
        finally:
            reset_current_run(token)

    def _join_payload(self, agents, worker_id: str) -> dict:
        payload = super()._join_payload(agents, worker_id)
        payload["capabilities"] = ["tools"]
        payload.pop("agents", None)
        return payload

    def _ensure_agent(self, agent: Agent):
        # The carrier is a local grouping of tools, not an agent anyone can message.
        return
