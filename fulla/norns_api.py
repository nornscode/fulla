"""The slice of the Norns REST API the channel needs, without the SDK.

The SDK imports LiteLLM at import time, which is most of the worker's memory
and none of the channel's business. These few calls are what the channel
uses; the shapes mirror norns.models so the channel code reads the same.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from fulla.schedule import Trigger


class Busy(Exception):
    """Norns answered 409: the conversation already has a run in flight."""


@dataclass
class WaitingFor:
    question: str
    tool_call_id: str


@dataclass
class Run:
    run_id: int
    status: str
    output: str | None
    agent_id: int
    trigger_type: str
    inserted_at: str
    waiting_for: WaitingFor | None = None
    conversation_id: int | None = None


@dataclass
class Agent:
    id: int
    name: str


@dataclass
class MessageResult:
    run_id: int
    status: str
    output: str | None


def _parse_trigger(data: dict) -> Trigger:
    return Trigger(id=data["id"], agent_id=data["agent_id"], name=data["name"], cron=data["cron"],
                   message=data["message"], conversation_key=data.get("conversation_key"),
                   enabled=bool(data.get("enabled", True)), last_fired_at=data.get("last_fired_at"))


def _parse_run(data: dict) -> Run:
    wf = data.get("waiting_for")
    return Run(
        run_id=data["id"],
        status=data["status"],
        output=data.get("output"),
        agent_id=data["agent_id"],
        trigger_type=data.get("trigger_type", "message"),
        inserted_at=data["inserted_at"],
        waiting_for=WaitingFor(question=wf["question"], tool_call_id=wf["tool_call_id"]) if wf else None,
        conversation_id=data.get("conversation_id"),
    )


def run_gone(e: Exception) -> bool:
    """Whether Norns answered 404 for a run: it does not exist on this tenant
    (deleted, or started before a move to another one), so it will never ask,
    wake or finish. Anything else (a timeout, a 500) says nothing about the run."""
    return isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 404


class NornsApi:
    def __init__(self, url: str, api_key: str):
        self._http = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        resp = self._http.request(method, path, **kwargs)
        resp.raise_for_status()
        return resp

    def list_agents(self) -> list[Agent]:
        data = self._request("GET", "/api/v1/agents").json()["data"]
        return [Agent(id=a["id"], name=a["name"]) for a in data]

    def get_agent(self, name: str) -> Agent:
        for a in self.list_agents():
            if a.name == name:
                return a
        raise ValueError(f"Agent not found: {name}")

    def conversation_id(self, agent: str, key: str) -> int | None:
        """The id of an agent's conversation by key, or None if it has not been
        created yet (the first message creates it)."""
        agent_id = self.get_agent(agent).id
        resp = self._http.get(f"/api/v1/agents/{agent_id}/conversations/{key}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()["data"]["id"]

    def tool_names(self) -> list[str]:
        """Names of every tool the tenant can call right now."""
        return [t["name"] for t in self._request("GET", "/api/v1/tools").json()["data"]]

    def delete_conversation(self, agent: str, key: str) -> bool:
        """Delete an agent's conversation by key, so the next message starts a
        fresh one under the same key. False if there was none."""
        agent_id = self.get_agent(agent).id
        resp = self._http.delete(f"/api/v1/agents/{agent_id}/conversations/{key}")
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True

    def update_agent_prompt(self, name: str, system_prompt: str) -> None:
        """Replace an agent's system prompt. The agent process re-reads its
        definition every step, so this is live on the next turn."""
        agent_id = self.get_agent(name).id
        self._request("PUT", f"/api/v1/agents/{agent_id}", json={"system_prompt": system_prompt})

    def update_agent(self, name: str, *, system_prompt: str, allowed_tools: list[str]) -> None:
        """Replace an agent's system prompt and tool allowlist together. The
        server takes model_config whole, so the rest of it is read back and
        sent unchanged."""
        agent_id = self.get_agent(name).id
        current = self._request("GET", f"/api/v1/agents/{agent_id}").json()["data"]
        model_config = dict(current.get("model_config") or {})
        model_config["tools"] = {"mode": "allowlist", "allowed_tools": list(allowed_tools)}
        self._request("PUT", f"/api/v1/agents/{agent_id}",
                      json={"system_prompt": system_prompt, "model_config": model_config})

    def list_runs(self, limit: int = 50, *, status: str | None = None,
                  agent_id: int | None = None) -> list[Run]:
        """The tenant's runs, newest first. `status` and `agent_id` narrow the
        query on the server before `limit` applies (nornscode/norns#62), so
        asking for `status="waiting"` finds every parked run, not the parked
        ones among the newest N."""
        params: dict = {"limit": limit}
        if status is not None:
            params["status"] = status
        if agent_id is not None:
            params["agent_id"] = agent_id
        data = self._request("GET", "/api/v1/runs", params=params).json()["data"]
        return [_parse_run(r) for r in data]

    def get_run(self, run_id: int) -> Run:
        return _parse_run(self._request("GET", f"/api/v1/runs/{run_id}").json()["data"])

    def last_event_type(self, run_id: int) -> str:
        """The type of the run's newest event ("waiting_for_timer" while it
        sleeps in `wait`), or "" if there are none or Norns would not say."""
        body = self._request("GET", f"/api/v1/runs/{run_id}/events").json()
        events = body.get("data", body) if isinstance(body, dict) else body
        if not isinstance(events, list) or not events:
            return ""
        newest = max(events, key=lambda e: e.get("sequence", 0))
        return str(newest.get("event_type") or newest.get("type") or "")

    def cancel_run(self, run_id: int) -> bool:
        """Stop a run. False if Norns answered 404: the run is gone, or this
        Norns has no cancel endpoint yet."""
        resp = self._http.post(f"/api/v1/runs/{run_id}/cancel")
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True

    def reply(self, run_id: int, answer: str) -> None:
        self._request("POST", f"/api/v1/runs/{run_id}/reply", json={"answer": answer})

    # Triggers: Norns's timers, which the manager's schedule tools manage for
    # the owner (schedule.py). `cron` is UTC, five fields or an alias.

    def list_triggers(self, agent_id: int | None = None) -> list[Trigger]:
        params = {"agent_id": agent_id} if agent_id is not None else {}
        data = self._request("GET", "/api/v1/triggers", params=params).json()["data"]
        return [_parse_trigger(t) for t in data]

    def create_trigger(self, *, agent_id: int, name: str, cron: str, message: str,
                       conversation_key: str, enabled: bool = True) -> Trigger:
        body = {"agent_id": agent_id, "name": name, "cron": cron, "message": message,
                "conversation_key": conversation_key, "enabled": enabled}
        return _parse_trigger(self._request("POST", "/api/v1/triggers", json=body).json()["data"])

    def update_trigger(self, trigger_id: int, **fields) -> Trigger:
        """Change what is given: cron, message, conversation_key, enabled, name."""
        return _parse_trigger(
            self._request("PATCH", f"/api/v1/triggers/{trigger_id}", json=fields).json()["data"])

    def delete_trigger(self, trigger_id: int) -> bool:
        """False if Norns answered 404: it was already gone."""
        resp = self._http.delete(f"/api/v1/triggers/{trigger_id}")
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True

    def send_message(
        self,
        agent: str,
        content: str,
        *,
        conversation_key: str | None = None,
        wait: bool = False,
        timeout: float = 30,
        interject: bool = False,
    ) -> MessageResult:
        """Send a message. With wait=True, poll until the run finishes or parks on
        a question; raise TimeoutError if it does neither in time. Raise Busy if
        the conversation is in the middle of a run, unless `interject`: then
        Norns takes the message into that run before its next model call (or
        makes it the next run's message, if the run ends first), and the
        result names the run under way."""
        agent_id = self.get_agent(agent).id
        body: dict = {"content": content}
        if conversation_key is not None:
            body["conversation_key"] = conversation_key
        if interject:
            body["interject"] = True
        resp = self._http.post(f"/api/v1/agents/{agent_id}/messages", json=body)
        if resp.status_code == 409:
            raise Busy(resp.text)
        resp.raise_for_status()
        data = resp.json()
        run_id = data["run_id"]
        if not wait:
            return MessageResult(run_id=run_id, status=data.get("status", "accepted"), output=None)

        deadline = time.monotonic() + timeout
        interval = 0.5
        while time.monotonic() < deadline:
            run = self.get_run(run_id)
            if run.status in ("completed", "failed", "error", "waiting"):
                return MessageResult(run_id=run_id, status=run.status, output=run.output)
            time.sleep(interval)
            interval = min(interval * 1.5, 3.0)
        raise TimeoutError(f"Run {run_id} did not complete within {timeout}s")
