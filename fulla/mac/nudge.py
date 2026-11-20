"""Tell queued Mac tasks the Mac is back, once, when this worker connects.

A queued task is a run of `fulla-mac-task` parked on `ask_human`, which
costs nothing and can be answered from outside. Answering it is the nudge:
the run resumes, sees the `mac_` tools in its list, and does its work. So
nothing polls while the lid is shut, and a reconnect wakes every waiting
task exactly once. Nothing here knows about Telegram.

The nudge asks Norns for the runs parked on `fulla-mac-task` by status and
agent, so a task queued long before the Mac reconnects is still found; the
page size is only a cap on how many it answers in one go.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from fulla.norns_api import NornsApi

MAC_TASK_AGENT = "fulla-mac-task"
ANSWER = "The Mac is connected."
RUNS_SCANNED = 500  # the most parked Mac tasks one nudge answers

log = logging.getLogger("fulla.mac.nudge")


def nudge(api: NornsApi, expected_tools: list[str], *, attempts: int = 15, delay: float = 2.0,
          sleep: Callable[[float], None] = time.sleep) -> int:
    """Answer every parked Mac task once the catalog shows this worker's tools.
    Returns how many were answered. Never raises: a failed nudge is a task that
    waits for the next reconnect, not a worker that cannot start."""
    try:
        if not _tools_visible(api, expected_tools, attempts, delay, sleep):
            log.warning("mac tools never appeared in the catalog; not nudging")
            return 0
        agent_id = api.get_agent(MAC_TASK_AGENT).id
    except Exception:
        log.exception("could not check the catalog or find %s", MAC_TASK_AGENT)
        return 0
    answered = 0
    try:
        for run in api.list_runs(limit=RUNS_SCANNED, status="waiting", agent_id=agent_id):
            # Re-checked here so a Norns without the filters still gets it right.
            if run.status != "waiting" or run.agent_id != agent_id or not run.waiting_for:
                continue
            try:
                api.reply(run.run_id, ANSWER)
                answered += 1
            except Exception as e:
                log.warning("could not nudge run %s: %s", run.run_id, e)
    except Exception:
        log.exception("could not list runs")
    if answered:
        log.info("nudged %d queued Mac task(s)", answered)
    return answered


def _tools_visible(api: NornsApi, expected: list[str], attempts: int, delay: float, sleep) -> bool:
    want = set(expected)
    for i in range(attempts):
        if want <= set(api.tool_names()):
            return True
        if i < attempts - 1:
            sleep(delay)
    return False
