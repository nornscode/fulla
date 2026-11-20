"""GitHub over REST, with httpx and no SDK: pull requests, CI, reviews,
issues, commits, files, search, one merge, and posting a review.

The client takes a token (a string, or a function from a repository to
one: a GitHub App's `token`, github_app.py) and a transport. It checks no
allowlist: the app that calls it has already decided which repository the
call may touch (Fulla resolves it from the group a run serves, and refuses
the rest before getting here). What comes back is structured where it can
be (numbers, states, logins, shas); the free text a stranger wrote (titles,
bodies, comments, branch and check names) is the caller's to mark as
untrusted. A merge is of one exact sha, so a push after the decision makes
GitHub refuse it.

Errors are `GitHubError` with a short reason; the caller decides how much
of it a model or a chat may see.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

API = "https://api.github.com"
TIMEOUT = 15.0
METHODS = ("squash", "merge", "rebase")
REVIEW_EVENTS = ("COMMENT", "APPROVE", "REQUEST_CHANGES")
TITLE_CHARS = 200
TEXT_CHARS = 2000  # of a body or a comment, before it is cut
MAX_READ_CHARS = 40_000  # of a file page
REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
_REPO_PATH = re.compile(r"^/repos/([^/]+/[^/]+)")


class GitHubError(Exception):
    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.status = status


def valid_repo(repo: str) -> bool:
    return bool(REPO_RE.match(repo or ""))


@dataclass
class Checks:
    state: str  # passing, failing, pending, none, unknown
    passed: int = 0
    failed: list[str] | None = None  # names; written by the PR's workflows, so untrusted
    pending: int = 0

    def summary(self) -> str:
        if self.state == "unknown":
            return "not known (the token cannot read checks)"
        if self.state == "none":
            return "no checks"
        parts = [f"{self.passed} passed"]
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        if self.pending:
            parts.append(f"{self.pending} pending")
        return f"{self.state} ({', '.join(parts)})"


def title(item: dict) -> str:
    t = " ".join(str(item.get("title") or "").split())
    return t if len(t) <= TITLE_CHARS else t[:TITLE_CHARS] + "…"


def text(s: str | None, limit: int = TEXT_CHARS) -> str:
    s = (s or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


def page(content: str, offset: int = 1, limit: int = 0) -> str:
    """One page of a file with a footer saying where it sits in the whole,
    so a long file takes several calls rather than arriving as its first
    page. A negative offset counts back from the end (offset=-80 is the last
    80 lines), the cheap way to the recent end of a log that grows by
    appending. Borrowed from Mimir."""
    lines = content.splitlines()
    total = len(lines)
    if total == 0:
        return "(empty file)"
    start = max(total + offset, 0) if offset < 0 else max(offset, 1) - 1
    if start >= total:
        return f"(offset {offset} is past the end; the file has {total} lines)"
    end = total if limit <= 0 else min(total, start + limit)
    out: list[str] = []
    used = 0
    for i, line in enumerate(lines[start:end]):
        used += len(line) + 1
        if used > MAX_READ_CHARS:
            end = start + i
            break
        out.append(line)
    body = "\n".join(out)
    if start > 0 or end < total:
        footer = f"[lines {start + 1}-{end} of {total}"
        if end < total:
            footer += f"; continue with offset={end + 1}"
        if start > 0:
            footer += "; earlier lines at offset=1"
        footer += "]"
        body = f"{body}\n{footer}" if body else footer
    return body or "(empty file)"


def _message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("message") or resp.text)[:200]
    except ValueError:
        return resp.text[:200]


class GitHub:
    def __init__(self, token: str | Callable[[str], str], *, transport: httpx.BaseTransport | None = None):
        self.token = token
        self._transport = transport

    def _token(self, path: str, repo: str) -> str:
        """The token for a call: the string, or the one made for the
        repository the call is about (from its path, or named for a search)."""
        if not callable(self.token):
            return self.token
        m = _REPO_PATH.match(path)
        repo = repo or (m.group(1) if m else "")
        if not repo:
            raise GitHubError("no repository to get a token for")
        return self.token(repo)

    def _request(self, method: str, path: str, *, repo: str = "", **kw) -> httpx.Response:
        if not self.token:
            raise GitHubError("no token")
        headers = {"Authorization": f"Bearer {self._token(path, repo)}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "norns-app"}
        try:
            with httpx.Client(timeout=TIMEOUT, transport=self._transport, base_url=API,
                              follow_redirects=False) as client:
                return client.request(method, path, headers=headers, **kw)
        except httpx.HTTPError as e:
            raise GitHubError(f"could not reach GitHub: {e}") from e

    def _get(self, path: str, _repo: str = "", **params) -> Any:
        resp = self._request("GET", path, repo=_repo, params=params or None)
        if resp.status_code != 200:
            raise GitHubError(f"GitHub said {resp.status_code} on {path}: {_message(resp)}", resp.status_code)
        return resp.json()

    def _get_unless_refused(self, path: str, **params) -> Any:
        """A GET whose 403 is None: a source of CI a token may not be able to read."""
        try:
            return self._get(path, **params)
        except GitHubError as e:
            if e.status == 403:
                return None
            raise

    # ------------------------------------------------------------ pull requests

    def pulls(self, repo: str, state: str = "open", limit: int = 20) -> list[dict]:
        if state not in ("open", "closed", "all"):
            raise GitHubError("state is open, closed or all")
        return self._get(f"/repos/{repo}/pulls", state=state, per_page=limit, sort="updated", direction="desc")

    def pull(self, repo: str, number: int) -> dict:
        return self._get(f"/repos/{repo}/pulls/{int(number)}")

    def pull_comments(self, repo: str, number: int, limit: int = 10) -> list[dict]:
        return self._get(f"/repos/{repo}/issues/{int(number)}/comments", per_page=limit)

    def review_state(self, repo: str, number: int) -> str:
        """approved, changes requested, or none, from each reviewer's latest say."""
        latest: dict[str, str] = {}
        for r in self._get(f"/repos/{repo}/pulls/{int(number)}/reviews", per_page=100):
            if r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                latest[(r.get("user") or {}).get("login", "")] = r["state"]
        states = set(latest.values())
        if "CHANGES_REQUESTED" in states:
            who = sorted(u for u, s in latest.items() if s == "CHANGES_REQUESTED")
            return f"changes requested by {', '.join(who)}"
        if "APPROVED" in states:
            who = sorted(u for u, s in latest.items() if s == "APPROVED")
            return f"approved by {', '.join(who)}"
        return "no reviews"

    def checks(self, repo: str, sha: str) -> Checks:
        """Check runs and commit statuses on a commit, summed up. A fine-grained
        personal token cannot be given the Checks permission (an app can), so a
        refused check-runs call falls back to the Actions workflow runs for the
        commit (Actions: read), and a refused source is left out rather than
        failing the whole read; with every source refused the state is unknown."""
        passed, pending, failed = 0, 0, []
        sources = 0

        def count(items: list[dict], name_key: str):
            nonlocal passed, pending
            for item in items:
                if item.get("status") != "completed":
                    pending += 1
                elif item.get("conclusion") in ("success", "neutral", "skipped"):
                    passed += 1
                else:
                    failed.append(str(item.get(name_key, "?")))

        runs = self._get_unless_refused(f"/repos/{repo}/commits/{sha}/check-runs", per_page=100)
        if runs is not None:
            sources += 1
            count(runs.get("check_runs", []), "name")
        else:
            workflows = self._get_unless_refused(f"/repos/{repo}/actions/runs", head_sha=sha, per_page=100)
            if workflows is not None:
                sources += 1
                count(workflows.get("workflow_runs", []), "name")
        status = self._get_unless_refused(f"/repos/{repo}/commits/{sha}/status")
        if status is not None:
            sources += 1
            for s in status.get("statuses", []):
                if s.get("state") == "success":
                    passed += 1
                elif s.get("state") == "pending":
                    pending += 1
                else:
                    failed.append(str(s.get("context", "?")))
        if not sources:
            return Checks(state="unknown")
        state = "failing" if failed else "pending" if pending else "passing" if passed else "none"
        return Checks(state=state, passed=passed, failed=failed, pending=pending)

    def merge_methods(self, repo: str) -> list[str]:
        data = self._get(f"/repos/{repo}")
        keys = {"squash": "allow_squash_merge", "merge": "allow_merge_commit", "rebase": "allow_rebase_merge"}
        return [m for m in METHODS if data.get(keys[m], True)]

    def merge(self, repo: str, number: int, sha: str, method: str) -> str:
        """Merge exactly `sha`. A note either way; GitHubError only when
        GitHub could not be asked (so the decision may be retried)."""
        if method not in METHODS:
            return f"Not merged: {method!r} is not a merge method."
        resp = self._request("PUT", f"/repos/{repo}/pulls/{int(number)}/merge",
                             json={"sha": sha, "merge_method": method})
        if resp.status_code == 200 and resp.json().get("merged"):
            merged = str(resp.json().get("sha") or "")[:7]
            return f"Merged {sha[:7]} ({method}){f' as {merged}' if merged else ''}."
        if resp.status_code == 409:
            return "Not merged: the branch moved since this card. Ask for a new one to merge the new head."
        return f"Not merged: GitHub said {resp.status_code}: {_message(resp)}"

    def create_review(self, repo: str, number: int, body: str, event: str, commit: str = "",
                      comments: list[dict] | None = None) -> dict:
        """Post a review on a pull request: `event` is COMMENT, APPROVE or
        REQUEST_CHANGES, `commit` the head it reviews, `comments` line
        comments as {path, line, body}."""
        if event not in REVIEW_EVENTS:
            raise GitHubError(f"{event!r} is not a review event")
        path = f"/repos/{repo}/pulls/{int(number)}/reviews"
        payload: dict = {"body": body, "event": event}
        if commit:
            payload["commit_id"] = commit
        if comments:
            payload["comments"] = [{"path": c["path"], "line": int(c["line"]), "body": c["body"]}
                                   for c in comments]
        resp = self._request("POST", path, json=payload)
        if resp.status_code != 200:
            raise GitHubError(f"GitHub said {resp.status_code} on {path}: {_message(resp)}", resp.status_code)
        return resp.json()

    # ------------------------------------------------------------------ issues

    def issues(self, repo: str, state: str = "open", limit: int = 20) -> list[dict]:
        """Issues, newest activity first; pull requests, which GitHub lists
        among issues, left out."""
        if state not in ("open", "closed", "all"):
            raise GitHubError("state is open, closed or all")
        found = []
        for issue in self._get(f"/repos/{repo}/issues", state=state, sort="updated", direction="desc",
                               per_page=min(100, limit * 2)):
            if issue.get("pull_request") is None:
                found.append(issue)
            if len(found) >= limit:
                break
        return found

    def issue(self, repo: str, number: int) -> dict:
        return self._get(f"/repos/{repo}/issues/{int(number)}")

    def issue_comments(self, repo: str, number: int, limit: int = 10) -> list[dict]:
        return self._get(f"/repos/{repo}/issues/{int(number)}/comments", per_page=limit)

    def create_issue(self, repo: str, title_: str, body: str = "") -> dict:
        path = f"/repos/{repo}/issues"
        resp = self._request("POST", path, json={"title": title_, "body": body})
        if resp.status_code != 201:
            raise GitHubError(f"GitHub said {resp.status_code} on {path}: {_message(resp)}", resp.status_code)
        return resp.json()

    # ----------------------------------------------------------- commits, files

    def commits(self, repo: str, branch: str = "", since: str = "", limit: int = 20) -> list[dict]:
        params: dict = {"per_page": limit}
        if branch:
            params["sha"] = branch
        if since:
            try:
                params["since"] = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=UTC).isoformat()
            except ValueError:
                raise GitHubError(f"since is a date, YYYY-MM-DD, not {since!r}") from None
        return self._get(f"/repos/{repo}/commits", **params)

    def contents(self, repo: str, path: str, ref: str = "") -> dict | list:
        """A file (with its text decoded) or a directory listing."""
        params = {"ref": ref} if ref else {}
        parts = path.strip("/").split("/")
        if any(p in (".", "..") for p in parts):
            # httpx would resolve the dot segments, and the request would
            # leave this repository for any other the token reaches.
            raise GitHubError(f"not a path in the repository: {path!r}")
        data = self._get(f"/repos/{repo}/contents/{quote('/'.join(parts), safe='/')}", **params)
        if isinstance(data, dict) and data.get("encoding") == "base64":
            import base64
            try:
                data["text"] = base64.b64decode(data.get("content") or "").decode("utf-8")
            except UnicodeDecodeError:
                data["text"] = None
        return data

    def branches(self, repo: str, limit: int = 30) -> list[dict]:
        return self._get(f"/repos/{repo}/branches", per_page=limit)

    def default_branch(self, repo: str) -> str:
        return str(self._get(f"/repos/{repo}").get("default_branch") or "")

    # ------------------------------------------------------------------ search

    # A query may carry qualifiers of its own, and GitHub ORs a second
    # `repo:` with ours, so what comes back is kept to `repo` here.

    def search_code(self, repo: str, query: str, limit: int = 5) -> list[dict]:
        data = self._get("/search/code", repo, q=f"{query} repo:{repo}", per_page=limit)
        return [item for item in data.get("items", [])
                if str((item.get("repository") or {}).get("full_name") or "").lower() == repo.lower()]

    def search_issues(self, repo: str, query: str, limit: int = 5) -> list[dict]:
        data = self._get("/search/issues", repo, q=f"{query} repo:{repo}", per_page=limit)
        own = f"{API}/repos/{repo}".lower()
        return [item for item in data.get("items", []) if str(item.get("repository_url") or "").lower() == own]
