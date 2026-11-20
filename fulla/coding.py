"""The inner lock on the Bubbo coding tools: whose sandbox is whose.

The coding tools (`provision_environment` and the rest) are served by the
Bubbo worker (nornscode/bubbo), not by this one, so no Fulla tool sits in
front of them and `groupctx` cannot refuse from inside. What this worker
does hold is every LLM call of the manager and of each group's agent: the
model's tool calls pass through `wake.ServingNorns` on their way to Norns.
`CodingGuard.check` reads them there, the way a group tool reads its group
from the run, and refuses before Norns ever dispatches one:

- A group's agent (`fulla-group-<id>`, by the agent id core put on the task,
  never a model argument) may provision only a repository among that
  group's `github_repo` sources, and only while the owner has "coding" on
  for it; the repository goes to Bubbo as the source's own identifier.
- A sandbox, and a coding job, belongs to the scope whose provision or
  start created it, learned from the tool result in that agent's own
  history (`Sandboxes`, `data/sandboxes.json`, first claim wins). A group
  drives only its own; one it has not created is refused. The manager
  drives anything no group created, so sandboxes from before this lock
  still work, and never a group's.
- The sandbox watcher (`fulla-watch`) reads for the manager: only
  `get_environment` and `get_coding_task`, on what the manager may drive,
  and it claims nothing.
- Any other agent (the direct-chat social agent, the reader) is refused
  every coding call; their allowlists do not hold them anyway.

A refusal ends the turn with a fixed sentence in place of the calls, so the
group hears that and nothing else: no repository, id or reason of Bubbo's.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from fulla import groupctx
from fulla.groups import GROUP_AGENT_PREFIX, OWNER_ID, GroupStore

# Served by the Bubbo worker: coding agents in throwaway sandboxes. Like a
# machine's tools, Norns offers them only while that worker is connected.
CODING_TOOLS = ["provision_environment", "get_environment", "start_coding_agent",
                "get_coding_task", "cancel_coding_task", "destroy_environment",
                "open_pull_request"]
PROVISION = "provision_environment"
START = "start_coding_agent"
ENVIRONMENT_TOOLS = frozenset({"get_environment", START, "destroy_environment", "open_pull_request"})
JOB_TOOLS = frozenset({"get_coding_task", "cancel_coding_task"})
# What the watcher may call: reads, never a change.
WATCH_TOOLS = frozenset({"get_environment", "get_coding_task"})

# What a group hears. Fixed sentences: never an id, a repository or Bubbo's words.
NOT_HERE = groupctx.NO_GROUP
REPO_NOT_HERE = "I can only open a sandbox on a repository connected to this group."
NOT_YOURS = "That sandbox isn't this group's."
# What the owner hears from the manager.
A_GROUPS = "That sandbox belongs to one of your groups, so I can't drive it from here."


def _text(content) -> str:
    return content if isinstance(content, str) else json.dumps(content, default=str)


def _field(content, key: str) -> str | None:
    """`key`'s string value in a tool result, JSON or not."""
    m = re.search(rf'"?{key}"?\s*[:=]\s*"?([A-Za-z0-9_.:-]+)', _text(content))
    return m.group(1) if m else None


class Sandboxes:
    """Which scope (a group's id, or "owner") each sandbox and job belongs to.
    Re-read on every call: the worker's LLM calls run on more than one task."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if self.path.exists():
            raw = json.loads(self.path.read_text() or "{}")
            return {"environments": raw.get("environments", {}), "jobs": raw.get("jobs", {})}
        return {"environments": {}, "jobs": {}}

    def owner_of(self, kind: str, ident: str) -> str | None:
        return self._load()[kind].get(str(ident))

    def claim(self, kind: str, ident: str, scope: str) -> str:
        """Record `ident` as `scope`'s unless someone already has it; the holder."""
        with self._lock:
            data = self._load()
            held = data[kind].get(str(ident))
            if held is None:
                data[kind][str(ident)] = held = scope
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=2))
                tmp.replace(self.path)
            return held


class CodingGuard:
    def __init__(self, groups: GroupStore | None, sandboxes: Sandboxes, manager: str = "fulla",
                 watcher: str = "fulla-watch"):
        self.groups = groups
        self.sandboxes = sandboxes
        self.manager = manager
        self.watcher = watcher

    def scope_of(self, agent: str | None) -> str | None:
        """OWNER_ID for the manager, a group's id for its agent, None for anyone else."""
        if agent == self.manager:
            return OWNER_ID
        if agent and agent.startswith(GROUP_AGENT_PREFIX):
            return agent[len(GROUP_AGENT_PREFIX):]
        return None

    def learn(self, scope: str, messages: list[dict] | None) -> None:
        """Claim for `scope` every sandbox its own provisions created and every
        job its own starts created, from the results in its history."""
        calls: dict[str, str] = {}
        for msg in messages or []:
            if msg.get("role") == "assistant":
                for call in msg.get("tool_calls") or []:
                    if call.get("name") in (PROVISION, START):
                        calls[str(call.get("id"))] = call["name"]
            elif msg.get("role") == "tool":
                name = calls.get(str(msg.get("tool_call_id", msg.get("id", ""))))
                if name == PROVISION and (env := _field(msg.get("content"), "environment_id")):
                    self.sandboxes.claim("environments", env, scope)
                elif name == START and (job := _field(msg.get("content"), "job_id")):
                    self.sandboxes.claim("jobs", job, scope)

    def refusal(self, scope: str | None, call: dict) -> str | None:
        """Why this coding call may not go, as the sentence to say, or None.
        A provision allowed from a group has its `repo` set to the source's
        identifier, in place."""
        if scope is None:
            return NOT_HERE
        args = call.get("arguments")
        if not isinstance(args, dict):
            args = {}
        name = call["name"]
        group = None
        if scope != OWNER_ID:
            group = self.groups.get(scope) if self.groups is not None else None
            if group is None or "coding" not in group.tools:
                return NOT_HERE
        if name == PROVISION:
            if group is None:
                return None  # the owner's own: Bubbo's enabled list is the bound
            source = groupctx.source(group, "github_repo", str(args.get("repo") or ""))
            if source is None:
                return REPO_NOT_HERE
            args["repo"] = source.identifier
            call["arguments"] = args
            return None
        if name in ENVIRONMENT_TOOLS:
            kind, ident = "environments", args.get("environment_id")
        elif name in JOB_TOOLS:
            kind, ident = "jobs", args.get("job_id")
        else:
            return NOT_HERE
        holder = self.sandboxes.owner_of(kind, str(ident)) if ident else None
        if group is None:
            return A_GROUPS if holder not in (None, OWNER_ID) else None
        return None if holder == scope else NOT_YOURS

    def check(self, agent: str | None, messages: list[dict] | None, result: dict) -> dict:
        """The model's response for `agent`, with its coding calls let through,
        or replaced by the refusal when any one of them may not go."""
        calls = [c for c in result.get("tool_calls") or [] if c.get("name") in CODING_TOOLS]
        if agent == self.watcher:
            # The manager's reads and nothing else, and no claims: it starts nothing.
            for call in calls:
                why = NOT_HERE if call["name"] not in WATCH_TOOLS else self.refusal(OWNER_ID, call)
                if why is not None:
                    return refused(result, why)
            return result
        scope = self.scope_of(agent)
        if scope is not None:
            self.learn(scope, messages)
        for call in calls:
            why = self.refusal(scope, call)
            if why is not None:
                return refused(result, why)
        return result


def refused(result: dict, sentence: str) -> dict:
    """The response as a final answer saying `sentence`, its tool calls dropped."""
    out = {k: v for k, v in result.items() if k != "tool_calls"}
    out.update(content=sentence, finish_reason="stop", final_output=sentence)
    return out
