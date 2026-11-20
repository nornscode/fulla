"""`fulla-eval`: the manager's regression set, run against the model.

Each case in evals/cases/*.yaml is a short scripted conversation. The harness
builds the LLM task the way production does (the real `worker.manager_prompt`,
the manager's real tool schemas plus Norns's built-ins, the per-turn note from
`wake.with_turn_note`) and sends it through the SDK's own LLM call, so the
model sees what it would see on Fly. Nothing is executed: when the model calls
a tool, the case's stub (or a default) is fed back as the result, until it
replies in text or runs out of steps. Then the checks run on what it said and
which tools it called. No Norns, no messenger.

A case with `agent: social` runs the friends-and-groups agent instead: its
prompt and its tool list (the turn note is a no-op there, as on Fly), with a
`user:` message's `from:` marked as the channel marks it (untrusted unless
it is the owner).

Every fixed bug adds a case. See docs/evals.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from fulla import zones

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "evals" / "cases"
SOUL = ROOT / "evals" / "soul.md"
MAX_STEPS = 6
DEFAULT_NOW = "2026-10-19T09:30:00"  # a Monday
# The cases name their owner, so the evals run as that person whatever
# FULLA_OWNER_NAME says.
OWNER = "Robin"

# A model takes a Norns LLM task (neutral format) and returns the SDK's result:
# {"status": "ok", "content": str, "tool_calls": [{"id", "name", "arguments"}]}.
Model = Callable[[dict], dict]

# Norns offers these to every agent, and no allowlist removes them (see
# norns lib/norns/tools/builtins.ex, which these mirror).
BUILTINS = [
    {"name": "wait", "description": "Pause execution for a specified number of seconds.",
     "parameters": {"type": "object", "properties": {
         "seconds": {"type": "integer", "description": "Number of seconds to wait"},
         "reason": {"type": "string", "description": "Why the agent is waiting"}},
         "required": ["seconds"]}},
    {"name": "ask_human",
     "description": "Ask the human a question and pause until they answer. Use this to confirm "
                    "before taking an action with real side effects, or when a detail is "
                    "ambiguous and guessing would be wrong. The run parks indefinitely until a "
                    "reply arrives.",
     "parameters": {"type": "object", "properties": {
         "question": {"type": "string", "description": "The question to ask the human"}},
         "required": ["question"]}},
    {"name": "launch_agent",
     "description": "Launch another agent as a sub-task and wait for its result. The child agent "
                    "will run asynchronously and the result will be returned when it completes. "
                    "You may optionally pass context to give the child agent relevant history "
                    "and data from the current conversation.",
     "parameters": {"type": "object", "properties": {
         "agent_name": {"type": "string", "description": "Name of the agent to launch"},
         "message": {"type": "string", "description": "Message to send to the child agent"},
         "context": {"type": "object", "description": "Optional context to pass to the child agent",
                     "properties": {
                         "messages": {"type": "array", "items": {"type": "object"},
                                      "description": "Relevant message history to seed the "
                                                     "child agent's conversation"},
                         "data": {"type": "object",
                                  "description": "Structured data the child agent needs (will "
                                                 "be included as a system context message)"}}}},
         "required": ["agent_name", "message"]}},
    {"name": "list_agents", "description": "List all available agents that can be launched as sub-tasks.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
]

def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": {k: {"type": t} for k, t in props.items()}, "required": required}


# The Bubbo worker's coding tools (nornscode/bubbo, bubbo/tools.py), offered
# when a case lists "bubbo" under `connected`. The enabled list is a wildcard,
# as in production, so it names no repository.
CODING_TOOL_DEFS = [
    {"name": "provision_environment",
     "description": "Create an isolated coding sandbox with a fresh clone of a repository. Returns at "
                    "once with state \"provisioning\"; it is usually ready within a minute or two. "
                    "Enabled repositories: nornscode/* (owner/* is any repository of that owner).",
     "parameters": _obj({"repo": "string", "base_ref": "string", "profile": "string",
                         "pull_request": "integer"}, ["repo"])},
    {"name": "get_environment", "description": "State of a sandbox.",
     "parameters": _obj({"environment_id": "string"}, ["environment_id"])},
    {"name": "start_coding_agent", "description": "Start a coding agent in a ready sandbox with a task.",
     "parameters": _obj({"environment_id": "string", "task": "string", "provider": "string"},
                        ["environment_id", "task"])},
    {"name": "get_coding_task", "description": "State and output of a coding job.",
     "parameters": _obj({"job_id": "string", "cursor": "integer"}, ["job_id"])},
    {"name": "cancel_coding_task", "description": "Cancel a coding job.",
     "parameters": _obj({"job_id": "string"}, ["job_id"])},
    {"name": "destroy_environment", "description": "Destroy a sandbox.",
     "parameters": _obj({"environment_id": "string"}, ["environment_id"])},
    {"name": "open_pull_request", "description": "Open a draft pull request from a sandbox's changes.",
     "parameters": _obj({"environment_id": "string", "title": "string", "body": "string"},
                        ["environment_id", "title"])},
]
BUBBO = "bubbo"

# What a tool "returns" when the case gives no stub. Plausible and empty.
DEFAULT_STUBS = {
    "recall": "Nothing stored matches.",
    "calendar": "Nothing booked.",
    "list_calendars": "No calendars connected.",
    "list_reminders": "No open reminders.",
    "list_followups": "No follow-ups pending.",
    "list_mac_queue": "Nothing queued for the Mac.",
    "list_directives": "No standing instructions.",
    "list_feedback": "No feedback yet.",
    "remember": "Remembered.",
    "note_feedback": "Noted for review.",
    "schedule_reminder": "Scheduled (run 101).",
    "drop_reminder": "Closed.",
    "schedule_followup": "Follow-up set (run 102).",
    "queue_for_mac": "Queued for the Mac (run 103).",
    "launch_agent": "The reader found nothing useful.",
    "ask_human": "(no answer)",
    "provision_environment": '{"environment_id": "env_1", "state": "provisioning"}',
}


# --- Cases -------------------------------------------------------------------

AGENTS = {"manager", "social"}
CHECK_KINDS = {"called", "not_called", "first_call", "no_tools", "reply", "not_reply", "judge"}


@dataclass
class Check:
    kind: str
    value: str | bool
    args: dict[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        args = f" {self.args}" if self.args else ""
        return f"{self.kind}: {self.value}{args}"


@dataclass
class Case:
    name: str
    path: Path | None
    messages: list[dict]
    checks: list[Check]
    why: str = ""
    connected: list[str] = field(default_factory=list)  # machines whose tools are in the list
    known: dict[str, list[str]] = field(default_factory=dict)  # machine -> allowed tmux sessions
    purposes: dict[str, str] = field(default_factory=dict)  # machine -> what the owner said it is for
    now: str = DEFAULT_NOW
    stubs: dict[str, str] = field(default_factory=dict)
    directives: list[str] = field(default_factory=list)
    soul: str | None = None
    max_steps: int = MAX_STEPS
    agent: str = "manager"  # or "social": fulla-social, the friends-and-groups agent


class CaseError(ValueError):
    pass


def _messages(raw: list, owner_name: str = "") -> list[dict]:
    """The case's shorthand as Norns neutral messages: `user:` (with optional
    `from:`, who said it, for the social agent), `assistant:` (with optional
    `calls:` of {name, args}), and `tool:`, the result of the next unanswered
    call."""
    out, pending, n = [], [], 0
    for i, item in enumerate(raw or []):
        if not isinstance(item, dict):
            raise CaseError(f"message {i + 1} is not a mapping")
        if "user" in item:
            text = str(item["user"])
            if item.get("from"):
                from fulla.channel import social_body
                text = social_body(str(item["from"]), text, owner_name)
            out.append({"role": "user", "content": text})
        elif "assistant" in item:
            msg = {"role": "assistant", "content": str(item["assistant"] or "")}
            calls = []
            for call in item.get("calls") or []:
                n += 1
                calls.append({"id": f"call_{n}", "name": call["name"],
                              "arguments": call.get("args") or {}})
            if calls:
                msg["tool_calls"] = calls  # pyright: ignore[reportArgumentType]
                pending += [c["id"] for c in calls]
            out.append(msg)
        elif "tool" in item:
            if not pending:
                raise CaseError(f"message {i + 1}: a tool result with no call to answer")
            out.append({"role": "tool", "tool_call_id": pending.pop(0), "content": str(item["tool"])})
        else:
            raise CaseError(f"message {i + 1} needs one of user, assistant, tool")
    if pending:
        raise CaseError("a call in the script has no tool result")
    if not out or out[-1]["role"] not in ("user", "tool"):
        raise CaseError("the script must end with a user message or a tool result")
    return out


def _checks(raw: list) -> list[Check]:
    checks = []
    for item in raw or []:
        kinds = [k for k in item if k in CHECK_KINDS]
        if len(kinds) != 1:
            raise CaseError(f"check {item!r} needs exactly one of {sorted(CHECK_KINDS)}")
        kind = kinds[0]
        args = {k: str(v) for k, v in (item.get("args") or {}).items()}
        for pattern in ([item[kind]] if kind in ("reply", "not_reply") else []) + list(args.values()):
            try:
                re.compile(pattern)
            except re.error as e:
                raise CaseError(f"bad regex {pattern!r}: {e}") from None
        checks.append(Check(kind=kind, value=item[kind], args=args))
    if not checks:
        raise CaseError("a case needs at least one check")
    return checks


def parse_case(data: dict, *, name: str, path: Path | None = None, owner_name: str = OWNER) -> Case:
    agent = str(data.get("agent") or "manager")
    if agent not in AGENTS:
        raise CaseError(f"agent must be one of {sorted(AGENTS)}")
    known = data.get("known") or {}
    connected = [str(m) for m in data.get("connected") or []]
    for m in connected:
        if m != BUBBO:
            known.setdefault(m, [])
    return Case(
        name=name, path=path, why=str(data.get("why") or "").strip(),
        messages=_messages(data.get("messages"), owner_name), checks=_checks(data.get("checks")),  # pyright: ignore[reportArgumentType]
        connected=connected, known={k: list(v or []) for k, v in known.items()},
        purposes={str(k): str(v) for k, v in (data.get("purposes") or {}).items()},
        now=str(data.get("now") or DEFAULT_NOW),
        stubs={k: str(v) for k, v in (data.get("stubs") or {}).items()},
        directives=[str(d) for d in data.get("directives") or []],
        soul=data.get("soul"), max_steps=int(data.get("max_steps") or MAX_STEPS), agent=agent,
    )


def load_cases(directory: Path = CASES, owner_name: str = OWNER) -> list[Case]:
    cases = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            cases.append(parse_case(yaml.safe_load(path.read_text()) or {}, name=path.stem, path=path,
                                    owner_name=owner_name))
        except (CaseError, KeyError, TypeError, yaml.YAMLError) as e:
            raise CaseError(f"{path.name}: {e}") from None
    return cases


# --- The task, as production builds it ---------------------------------------

def _tool_api(t) -> dict:
    return {"name": t.name, "description": t.description, "parameters": t.input_schema}


def _agent_name(case: Case) -> str:
    from fulla.worker import MANAGER_AGENT, SOCIAL_AGENT

    return SOCIAL_AGENT if case.agent == "social" else MANAGER_AGENT


def sent_tools(case: Case) -> list[dict]:
    """Every tool core would send the worker for this case's agent: its own,
    each connected machine's, and the built-ins, before the worker leaves any
    out."""
    from fulla import shell, tmux
    from fulla.mac.tools import mac_tools
    from fulla.tools import manager_tools, social_tools

    if case.agent == "social":
        return [_tool_api(t) for t in social_tools] + BUILTINS
    tools = list(manager_tools)
    machines = [m for m in case.connected if m != BUBBO]
    for machine in machines:
        tools += mac_tools if machine == "mac" else (
            tmux.tmux_tools(machine, list) + shell.shell_tools(machine, lambda: True))
    coding = CODING_TOOL_DEFS if BUBBO in case.connected else []
    return [_tool_api(t) for t in tools] + coding + BUILTINS


def tool_list(case: Case, messages: list[dict] | None = None) -> list[dict]:
    """The tools the model is offered on the turn `messages` end in (the
    case's own, by default): what core sends, less what the worker leaves out
    in production (`wake.offered`)."""
    from fulla.wake import offered

    turn = case.messages if messages is None else messages
    return offered(_agent_name(case), {"tools": sent_tools(case), "messages": turn})["tools"]


def system_prompt(case: Case, config, workdir: Path) -> str:
    from fulla.directives import DirectiveStore
    from fulla.hosts import Host, HostStore
    from fulla.soul import Soul
    from fulla.worker import manager_prompt, social_prompt

    if case.agent == "social":
        return social_prompt(config)
    directives = DirectiveStore(workdir / "directives.json")
    for d in case.directives:
        directives.add(d)
    hosts = HostStore(workdir / "hosts.json")
    for name, sessions in case.known.items():
        hosts.register(Host(name=name, platform="macOS" if name == "mac" else "Linux",
                            sessions=sessions, worker_id=f"fulla-{name}",
                            seen_at="2026-10-18T12:00:00+00:00", shell=True,
                            purpose=case.purposes.get(name, "")))
    soul = Soul(workdir / "soul.md")
    text = case.soul if case.soul is not None else SOUL.read_text()
    if text.strip():
        soul.write(text)
    return manager_prompt(config, directives, hosts, soul)


def build_task(case: Case, config, model: str, messages: list[dict], workdir: Path) -> dict:
    from fulla.wake import for_turn

    task = {
        "model": model,
        "system_prompt": system_prompt(case, config, workdir),
        "tools": sent_tools(case),
        "messages": messages,
        "date": case.now[:10],
    }
    return for_turn(_agent_name(case), task, [m for m in case.connected if m not in ("mac", BUBBO)],
                    config.fulla.owner_name)


def now_stub(case: Case, timezone: str, tz: str = "") -> str:
    """What `now` would say at the case's moment, in the same words."""
    home = ZoneInfo(timezone)
    dt = datetime.fromisoformat(case.now)
    dt = dt.replace(tzinfo=home) if dt.tzinfo is None else dt.astimezone(home)
    out = f"{dt.isoformat(timespec='seconds')} ({dt.strftime('%A')}, {timezone})"
    if not tz:
        return out
    try:
        there = dt.astimezone(zones.zone(tz))
    except ValueError as e:
        return str(e)
    return f"{out}; in {tz}: {there.isoformat(timespec='seconds')} ({there.strftime('%A')})"


def stub(case: Case, call: dict, timezone: str) -> str:
    name = call["name"]
    if name in case.stubs:
        return case.stubs[name]
    if name == "now":
        return now_stub(case, timezone, str((call.get("arguments") or {}).get("tz") or ""))
    return DEFAULT_STUBS.get(name, "OK.")


# --- Running a case ----------------------------------------------------------

@dataclass
class Transcript:
    calls: list[dict] = field(default_factory=list)  # {"name", "arguments"}, in order
    texts: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def reply(self) -> str:
        return "\n".join(t for t in self.texts if t.strip())

    def show(self) -> str:
        lines = [f"  call {c['name']}({json.dumps(c['arguments'], ensure_ascii=False)})"
                 for c in self.calls]
        lines += ["  reply: " + (self.reply.replace("\n", "\n         ") or "(none)")]
        if self.error:
            lines.append(f"  error: {self.error}")
        return "\n".join(lines)


def converse(case: Case, model: Model, config, model_name: str) -> Transcript:
    """Run the manager's turn: call the model, answer its tool calls from
    stubs, until it replies without a call or runs out of steps."""
    out = Transcript()
    messages = [dict(m) for m in case.messages]
    with tempfile.TemporaryDirectory() as tmp:
        for _ in range(case.max_steps):
            task = build_task(case, config, model_name, messages, Path(tmp))
            result = model(task)
            if result.get("status") != "ok":
                out.error = str(result.get("error") or result)
                return out
            content = result.get("content") or ""
            if isinstance(content, str) and content.strip():
                out.texts.append(content)
            calls = result.get("tool_calls") or []
            if not calls:
                return out
            messages.append({"role": "assistant", "content": content, "tool_calls": calls})
            for call in calls:
                out.calls.append({"name": call["name"], "arguments": call.get("arguments") or {}})
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": stub(case, call, config.fulla.timezone)})
    return out


def _args_match(want: dict[str, str], got: dict) -> bool:
    for key, pattern in want.items():
        if key not in got:
            return False
        value = got[key] if isinstance(got[key], str) else json.dumps(got[key])
        if not re.search(pattern, value):
            return False
    return True


JUDGE_PROMPT = """\
You grade one reply from an AI personal manager against one question. Judge \
only what the question asks. Answer with YES or NO on the first line, then one sentence \
saying why."""


def judge(case: Case, transcript: Transcript, question: str, model: Model, model_name: str) -> tuple[bool, str]:
    convo = "\n".join(f"{m['role']}: {m.get('content') or ''}"
                      + (f" [calls {', '.join(c['name'] for c in m['tool_calls'])}]"
                         if m.get("tool_calls") else "")
                      for m in case.messages)
    turn = "\n".join([f"called {c['name']}({json.dumps(c['arguments'], ensure_ascii=False)})"
                      for c in transcript.calls] + [f"said: {transcript.reply or '(nothing)'}"])
    task = {
        "model": model_name,
        "system_prompt": JUDGE_PROMPT,
        "tools": [],
        "max_tokens": 300,
        "messages": [{"role": "user", "content":
                      f"The conversation so far:\n{convo}\n\nThe manager's turn:\n{turn}\n\n"
                      f"Question: {question}"}],
    }
    result = model(task)
    if result.get("status") != "ok":
        return False, f"judge error: {result.get('error')}"
    answer = (result.get("content") or "").strip()
    return answer.upper().startswith("YES"), answer


def check(case: Case, transcript: Transcript, model: Model | None = None,
          model_name: str = "") -> list[str]:
    """The failures, as one line each; empty means the case passed. `model`
    and `model_name` are the judge's, for `judge` checks."""
    if transcript.error:
        return [f"model error: {transcript.error}"]
    failures = []
    for c in case.checks:
        if c.kind == "called":
            if not any(k["name"] == c.value and _args_match(c.args, k["arguments"])
                       for k in transcript.calls):
                failures.append(f"expected a call to {c.value}" + (f" with {c.args}" if c.args else ""))
        elif c.kind == "not_called":
            if any(k["name"] == c.value and _args_match(c.args, k["arguments"]) for k in transcript.calls):
                failures.append(f"called {c.value}, which it must not")
        elif c.kind == "first_call":
            first = transcript.calls[0] if transcript.calls else None
            if not first or first["name"] != c.value or not _args_match(c.args, first["arguments"]):
                failures.append(f"first call should be {c.value}"
                                + (f" with {c.args}" if c.args else "")
                                + f", was {first['name'] if first else 'none'}")
        elif c.kind == "no_tools":
            if c.value and transcript.calls:
                failures.append("expected no tool calls")
        elif c.kind == "reply":
            if not re.search(c.value, transcript.reply):  # pyright: ignore[reportArgumentType, reportCallIssue]
                failures.append(f"reply should match /{c.value}/")
        elif c.kind == "not_reply":
            m = re.search(c.value, transcript.reply)  # pyright: ignore[reportArgumentType, reportCallIssue]
            if m:
                failures.append(f"reply must not match /{c.value}/ (matched {m.group(0)!r})")
        elif c.kind == "judge":
            if model is None:
                failures.append("judge check needs a model")
                continue
            ok, why = judge(case, transcript, str(c.value), model, model_name)
            if not ok:
                failures.append(f"judge said no to {c.value!r}: {why}")
    return failures


@dataclass
class Outcome:
    case: Case
    runs: list[tuple[Transcript, list[str]]]

    @property
    def passed(self) -> int:
        return sum(1 for _, f in self.runs if not f)


def run_case(case: Case, model: Model, config, model_name: str, repeat: int = 1,
             judge_model: Model | None = None, judge_name: str = "") -> Outcome:
    """The case `repeat` times. The judge is the config's model unless given,
    so changing the model under test does not change who grades it."""
    runs = []
    for _ in range(repeat):
        transcript = converse(case, model, config, model_name)
        runs.append((transcript, check(case, transcript, judge_model or model,
                                       judge_name or model_name)))
    return Outcome(case, runs)


# --- The real model ----------------------------------------------------------

def litellm_model(llm_provider: str = "anthropic") -> Model:
    """The SDK's own LLM call, the one the worker makes on Fly: the same
    prompt composition, message conversion, cache points and response parsing."""
    import logging

    from norns import Norns

    for name in ("LiteLLM", "litellm", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)

    def call(task: dict) -> dict:
        worker = Norns("http://eval.invalid", api_key="eval")
        worker._llm_provider = llm_provider
        return asyncio.run(worker._handle_llm_task(task))

    return call


KEY_FOR = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}


def provider(model_name: str) -> str:
    """LiteLLM's provider: the prefix, or Anthropic, as the SDK defaults."""
    return model_name.split("/", 1)[0] if "/" in model_name else "anthropic"


def missing_key(model_name: str) -> str | None:
    """The env var the model needs and lacks, if any."""
    var = KEY_FOR.get(provider(model_name))
    return var if var and not os.environ.get(var) else None


def load_config():
    from fulla.config import load_config as load

    # The config names Norns's key; the evals never talk to Norns.
    os.environ.setdefault("NORNS_API_KEY", "eval")
    return load(ROOT / "config.yaml")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fulla-eval", description="Run the manager's regression set.")
    p.add_argument("-k", dest="match", default="", help="only cases whose name contains this")
    p.add_argument("--model", default="", help="model to evaluate (default: fulla.model in config.yaml)")
    p.add_argument("--judge-model", default="",
                   help="model for judge checks (default: fulla.model in config.yaml)")
    p.add_argument("--repeat", type=int, default=1, help="run each case N times, to see flakiness")
    p.add_argument("-j", "--jobs", type=int, default=4, help="cases run in parallel")
    p.add_argument("--cases", type=Path, default=CASES, help="directory of case files")
    p.add_argument("-v", "--verbose", action="store_true", help="show the transcript of passing runs too")
    args = p.parse_args(argv)

    os.environ["FULLA_OWNER_NAME"] = OWNER
    config = load_config()
    model_name = args.model or config.fulla.model
    judge_name = args.judge_model or config.fulla.model
    var = missing_key(model_name) or missing_key(judge_name)
    if var:
        print(f"fulla-eval: {var} is not set; skipping.")
        return 0
    cases = [c for c in load_cases(args.cases, config.fulla.owner_name) if args.match in c.name]
    if not cases:
        print("fulla-eval: no cases.")
        return 0
    model, judge_model = litellm_model(provider(model_name)), litellm_model(provider(judge_name))
    print(f"{len(cases)} cases against {model_name} (judge {judge_name}), {args.repeat}x each\n")

    def one(c: Case) -> Outcome:
        return run_case(c, model, config, model_name, args.repeat, judge_model, judge_name)

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        outcomes = list(pool.map(one, cases))

    failed = flaky = 0
    for o in outcomes:
        n = len(o.runs)
        label = "PASS" if o.passed == n else "FAIL" if o.passed == 0 else "FLAKY"
        failed += label == "FAIL"
        flaky += label == "FLAKY"
        print(f"{label:5} {o.case.name}" + (f"  {o.passed}/{n}" if n > 1 else ""))
        for i, (transcript, failures) in enumerate(o.runs):
            if not failures and not args.verbose:
                continue
            if n > 1:
                print(f"  run {i + 1}:")
            for f in failures:
                print(f"  - {f}")
            print(transcript.show())
    total = len(outcomes)
    print(f"\n{total - failed - flaky} passed, {flaky} flaky, {failed} failed")
    return 1 if failed or flaky else 0


if __name__ == "__main__":
    sys.exit(main())
