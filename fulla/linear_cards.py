"""A new Linear issue as a confirm card (see confirm.py and github_cards.py):
a group's agent proposes, the card goes to the owner's own chat, and only
the owner's File creates it, with the group's own key."""

from __future__ import annotations

import httpx

from fulla.confirm import Approval, Confirmation, ConfirmKind
from fulla.github_cards import asked_in
from fulla.groups import GroupStore
from norns_app.linear import Linear, LinearError

KIND = "linear_issue"
RULE = "─" * 16


def payload(group_id: str, group_name: str, team_key: str, team_id: str, title: str, description: str) -> dict:
    return {"group": group_id, "group_name": group_name, "team_key": team_key, "team_id": team_id,
            "title": title, "description": description}


def render(p: dict) -> str:
    lines = [f"File a Linear issue in {p['team_key']}?{asked_in(p)}", RULE, p["title"]]
    if p.get("description"):
        lines += ["", p["description"]]
    lines += [RULE, "Nothing is filed until you choose File."]
    return "\n".join(lines)


def revise(p: dict, text: str) -> dict:
    title, _, description = text.strip().partition("\n")
    return {**p, "title": title.strip() or p["title"], "description": description.strip()}


def kind(store: GroupStore, *, transport: httpx.BaseTransport | None = None) -> ConfirmKind:
    def approve(conf: Confirmation) -> Approval:
        p = conf.payload
        key = store.credential(p["group"], "linear")
        if not key:
            return Approval(note="Not filed: the group's Linear key is gone.")
        try:
            issue = Linear(key, transport=transport).create_issue(p["team_id"], p["title"], p.get("description", ""))
        except LinearError as e:
            if e.status is None:
                raise  # Linear could not be asked; the card stays to tap again
            return Approval(note=f"Not filed: {e}.")
        return Approval(note=f"Filed {issue.get('identifier', '?')}.")

    return ConfirmKind(name=KIND, render=render, approve=approve, revise=revise, approve_label="File")
