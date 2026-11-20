"""GitHub writes as confirm cards (see confirm.py): a merge, a new issue,
and a review of a pull request. A group's agent proposes; the card goes to the owner's own chat;
only the owner's tap acts, with the group's own token, and the merge is of
the exact head the card showed, so a push after the card makes GitHub
refuse it. The group hears that the owner was asked, never that it was
done, until the owner says so.
"""

from __future__ import annotations

import logging

import httpx

from fulla.confirm import Approval, Confirmation, ConfirmKind
from fulla.groups import OWNER_ID, GroupStore
from norns_app.github import GitHub, GitHubError

logger = logging.getLogger("fulla.github_cards")

MERGE_KIND = "github_merge"
ISSUE_KIND = "github_issue"
REVIEW_KIND = "github_review"
RULE = "─" * 16


def merge_payload(group_id: str, group_name: str, repo: str, number: int, title: str, sha: str, ci: str,
                  method: str) -> dict:
    return {"group": group_id, "group_name": group_name, "repo": repo, "number": int(number), "title": title,
            "sha": sha, "ci": ci, "method": method}


def asked_in(p: dict) -> str:
    """Where the ask came from, on the card: a group's name, or nothing when
    the owner asked for it themselves."""
    return "" if p.get("group") == OWNER_ID else f" (asked in {p['group_name']})"


def render_merge(p: dict) -> str:
    return "\n".join([
        f"Merge {p['repo']}#{p['number']}?{asked_in(p)}",
        p["title"],
        f"Head: {p['sha'][:7]}",
        f"CI: {p['ci']}",
        f"Method: {p['method']}",
        "Merges only this commit, and only when you choose Merge.",
    ])


def issue_payload(group_id: str, group_name: str, repo: str, title: str, body: str) -> dict:
    return {"group": group_id, "group_name": group_name, "repo": repo, "title": title, "body": body}


def render_issue(p: dict) -> str:
    lines = [f"File an issue in {p['repo']}?{asked_in(p)}", RULE, p["title"]]
    if p.get("body"):
        lines += ["", p["body"]]
    lines += [RULE, "Nothing is filed until you choose File."]
    return "\n".join(lines)


def revise_issue(p: dict, text: str) -> dict:
    """The owner's edit replaces the body; a first line alone replaces the title."""
    title, _, body = text.strip().partition("\n")
    return {**p, "title": title.strip() or p["title"], "body": body.strip()}


EVENT_SHOWN = {"COMMENT": "comment", "APPROVE": "approve", "REQUEST_CHANGES": "request changes"}


def review_payload(group_id: str, group_name: str, repo: str, number: int, title: str, sha: str, event: str,
                   body: str, comments: list[dict] | None = None) -> dict:
    return {"group": group_id, "group_name": group_name, "repo": repo, "number": int(number), "title": title,
            "sha": sha, "event": event, "body": body, "comments": list(comments or [])}


def render_review(p: dict) -> str:
    lines = [f"Review {p['repo']}#{p['number']}?{asked_in(p)}", p["title"],
             f"Event: {EVENT_SHOWN.get(p['event'], p['event'])}", f"Head: {p['sha'][:7]}", RULE]
    if p.get("body"):
        lines.append(p["body"])
    for c in p.get("comments") or []:
        lines += ["", f"{c['path']}:{c['line']}", c["body"]]
    lines += [RULE, "Nothing is posted until you choose Post."]
    return "\n".join(lines)


def revise_review(p: dict, text: str) -> dict:
    """The owner's edit replaces the review's body; event and line comments stay."""
    return {**p, "body": text.strip()}


def kinds(store: GroupStore, *, transport: httpx.BaseTransport | None = None) -> list[ConfirmKind]:
    """The three kinds, acting with the token of the group named in the
    payload. A token gone since the card is a note, not an action."""

    def client(p: dict) -> GitHub | None:
        token = store.github_token(p["group"])
        return GitHub(token, transport=transport) if token else None

    def approve_merge(conf: Confirmation) -> Approval:
        p = conf.payload
        gh = client(p)
        if gh is None:
            return Approval(note="Not merged: the group's GitHub token is gone.")
        return Approval(note=gh.merge(p["repo"], p["number"], p["sha"], p["method"]))

    def approve_issue(conf: Confirmation) -> Approval:
        p = conf.payload
        gh = client(p)
        if gh is None:
            return Approval(note="Not filed: the group's GitHub token is gone.")
        try:
            issue = gh.create_issue(p["repo"], p["title"], p.get("body", ""))
        except GitHubError as e:
            if e.status is None:
                raise  # GitHub could not be asked; the card stays to tap again
            return Approval(note=f"Not filed: GitHub said {e.status}.")
        return Approval(note=f"Filed {p['repo']}#{issue.get('number', '?')}.")

    def approve_review(conf: Confirmation) -> Approval:
        p = conf.payload
        gh = client(p)
        if gh is None:
            return Approval(note="Not posted: the group's GitHub token is gone.")
        try:
            review = gh.create_review(p["repo"], p["number"], p.get("body", ""), p["event"], p.get("sha", ""),
                                      p.get("comments"))
        except GitHubError as e:
            if e.status is None:
                raise  # GitHub could not be asked; the card stays to tap again
            return Approval(note=f"Not posted: GitHub said {e.status}.")
        posted = {"APPROVE": "an approval", "REQUEST_CHANGES": "a request for changes"}.get(p["event"], "a review")
        return Approval(note=f"Posted {posted} on {p['repo']}#{p['number']} (review {review.get('id', '?')}).")

    return [
        ConfirmKind(name=MERGE_KIND, render=render_merge, approve=approve_merge, approve_label="Merge"),
        ConfirmKind(name=ISSUE_KIND, render=render_issue, approve=approve_issue, revise=revise_issue,
                    approve_label="File"),
        ConfirmKind(name=REVIEW_KIND, render=render_review, approve=approve_review, revise=revise_review,
                    approve_label="Post"),
    ]
