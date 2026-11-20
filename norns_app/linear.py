"""Linear over its GraphQL API, with httpx: teams, projects, issues, and
the writes to one issue an app may do: create, update (assignee, state,
priority, title, labels), comment. Nothing deletes or archives.

Like `github.py`, the client takes a key and a transport and checks no
allowlist: the app decides which teams and projects a call may touch. What
comes back is structured where it can be (identifiers, states, names,
dates); titles, descriptions and comments are the caller's to mark as
untrusted. Errors are `LinearError` with a short reason.
"""

from __future__ import annotations

import httpx

API = "https://api.linear.app/graphql"
TIMEOUT = 15.0
TEXT_CHARS = 2000


class LinearError(Exception):
    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.status = status


def text(s: str | None, limit: int = TEXT_CHARS) -> str:
    s = (s or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _quoted(s: str) -> str:
    """A string literal for a query: GraphQL double-quoted, quotes stripped."""
    return '"' + s.replace('"', "") + '"'


_ISSUE_FIELDS = ("id identifier title priority createdAt updatedAt url state { name } assignee { name } "
                 "team { id key name } project { id slugId name } labels { nodes { id name } }")


class Linear:
    def __init__(self, key: str, *, transport: httpx.BaseTransport | None = None):
        self.key = key
        self._transport = transport

    def _query(self, query: str, variables: dict | None = None) -> dict:
        if not self.key:
            raise LinearError("no key")
        headers = {"Authorization": self.key, "Content-Type": "application/json", "User-Agent": "norns-app"}
        try:
            with httpx.Client(timeout=TIMEOUT, transport=self._transport) as client:
                resp = client.post(API, headers=headers, json={"query": query, "variables": variables or {}})
        except httpx.HTTPError as e:
            raise LinearError(f"could not reach Linear: {e}") from e
        if resp.status_code != 200:
            raise LinearError(f"Linear said {resp.status_code}", resp.status_code)
        data = resp.json()
        if data.get("errors"):
            message = str((data["errors"][0] or {}).get("message") or "error")[:200]
            raise LinearError(f"Linear said: {message}", 400)
        return data.get("data") or {}

    def teams(self) -> list[dict]:
        return (self._query("{ teams { nodes { id key name } } }").get("teams") or {}).get("nodes", [])

    def projects(self) -> list[dict]:
        return (self._query("{ projects { nodes { id slugId name state } } }").get("projects") or {}).get("nodes", [])

    def issues(self, *, team_key: str = "", project: str = "", state: str = "", limit: int = 20) -> list[dict]:
        """Issues of a team (by key) or a project (by id or slug), newest
        activity first; `state` narrows by state name."""
        filters = []
        if team_key:
            filters.append("team: { key: { eq: " + _quoted(team_key) + " } }")
        if project:
            filters.append("project: { or: [{ id: { eq: " + _quoted(project) + " } }, { slugId: { eq: "
                           + _quoted(project) + " } }] }")
        if state:
            filters.append("state: { name: { eqIgnoreCase: " + _quoted(state) + " } }")
        query = ("{ issues(filter: { " + ", ".join(filters) + " }, first: " + str(max(1, min(int(limit), 50)))
                 + ", orderBy: updatedAt) { nodes { " + _ISSUE_FIELDS + " } } }")
        return (self._query(query).get("issues") or {}).get("nodes", [])

    def issue(self, identifier: str) -> dict | None:
        query = ("{ issue(id: " + _quoted(identifier) + ") { " + _ISSUE_FIELDS
                 + " description comments(first: 10) { nodes { body createdAt user { name } } } } }")
        return self._query(query).get("issue")

    def create_issue(self, team_id: str, title_: str, description: str = "") -> dict:
        query = ("mutation($input: IssueCreateInput!) { issueCreate(input: $input) "
                 "{ success issue { identifier url } } }")
        data = self._query(query, {"input": {"teamId": team_id, "title": title_, "description": description}})
        result = data.get("issueCreate") or {}
        if not result.get("success"):
            raise LinearError("Linear did not create the issue", 400)
        return result.get("issue") or {}

    # --- the writes to an issue. Each takes Linear's own ids (an issue's
    # `id`, never its identifier; a user's, a state's, a label's), which the
    # reads return, so a caller resolves names before it writes.

    def users(self, name: str) -> list[dict]:
        """Active users whose name or display name contains `name`; Linear's
        agent users are users too."""
        query = ("{ users(filter: { active: { eq: true }, or: [{ name: { containsIgnoreCase: " + _quoted(name)
                 + " } }, { displayName: { containsIgnoreCase: " + _quoted(name) + " } }] }) "
                 "{ nodes { id name displayName } } }")
        return (self._query(query).get("users") or {}).get("nodes", [])

    def states(self, team_id: str) -> list[dict]:
        """A team's workflow states: id, name, type."""
        query = ("{ workflowStates(filter: { team: { id: { eq: " + _quoted(team_id) + " } } }) "
                 "{ nodes { id name type } } }")
        return (self._query(query).get("workflowStates") or {}).get("nodes", [])

    def labels(self, team_id: str) -> list[dict]:
        """The labels an issue of this team may carry: the team's own and the
        workspace's."""
        query = ("{ issueLabels(filter: { or: [{ team: { id: { eq: " + _quoted(team_id) + " } } }, "
                 "{ team: { null: true } }] }) { nodes { id name } } }")
        return (self._query(query).get("issueLabels") or {}).get("nodes", [])

    def update_issue(self, issue_id: str, **fields) -> dict:
        """`issueUpdate` with exactly the given fields (assigneeId, stateId,
        priority, title, labelIds); the issue as it is afterwards."""
        query = ("mutation($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, input: $input) "
                 "{ success issue { " + _ISSUE_FIELDS + " } } }")
        data = self._query(query, {"id": issue_id, "input": fields})
        result = data.get("issueUpdate") or {}
        if not result.get("success"):
            raise LinearError("Linear did not update the issue", 400)
        return result.get("issue") or {}

    def comment(self, issue_id: str, body: str) -> dict:
        query = ("mutation($input: CommentCreateInput!) { commentCreate(input: $input) "
                 "{ success comment { id url } } }")
        data = self._query(query, {"input": {"issueId": issue_id, "body": body}})
        result = data.get("commentCreate") or {}
        if not result.get("success"):
            raise LinearError("Linear did not add the comment", 400)
        return result.get("comment") or {}
