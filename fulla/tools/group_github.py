"""A group's GitHub, through the inner lock. Every tool here takes its
group from the run (`groupctx`), its token from that group's credentials
(or minted by the GitHub App for one of the group's repositories),
and its repositories from that group's sources: a repository the model
names is checked against them, and with none named the group's own are
used, never "all". What GitHub returns that a stranger wrote (titles,
bodies, comments, branch and check names, file contents) comes inside the
untrusted marker. Writes are confirm cards for the owner (github_cards.py).
What a group hears on failure is a fixed sentence.

Ported from Mimir's github.py and issues.py, and from PR #39's manager
tools, with one change throughout: scope comes from the group, not from
an argument.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx

from fulla import github_cards, groupctx
from fulla.groups import GroupRecord, GroupStore
from fulla.tooldef import tool
from fulla.untrusted import untrusted
from norns_app import github as gh
from norns_app.github import GitHub, GitHubError
from norns_app.github_app import NotInstalled

logger = logging.getLogger("fulla.tools.group_github")

NO_TOKEN = "This group has no GitHub token yet; the owner can set one."
NO_ANSWER = "GitHub didn't answer that."
NOT_INSTALLED = "The owner's GitHub app is not installed on that repository; the owner can install it there."
WHICH_REPO = "Say which repository: "
NO_CARD = "I can't ask the owner from here."
BAD_COMMENT = "Each line comment is {path, line, body}, with line a line number in the diff."

_store: GroupStore | None = None
_propose: Callable[[str, dict], object] | None = None
_transport: httpx.BaseTransport | None = None


def init(*, store: GroupStore | None, propose: Callable[[str, dict], object] | None = None,
         transport: httpx.BaseTransport | None = None):
    global _store, _propose, _transport
    _store, _propose, _transport = store, propose, transport


def _client(record: GroupRecord) -> GitHub:
    token = _store.github_token(record.id) if _store is not None else None
    if not token:
        raise groupctx.Refused(NO_TOKEN)
    return GitHub(token, transport=_transport)


def _repos(record: GroupRecord, repo: str) -> list[str]:
    return [s.identifier for s in groupctx.scoped(record, "github_repo", repo)]


def _one_repo(record: GroupRecord, repo: str) -> str:
    repos = _repos(record, repo)
    if len(repos) > 1:
        raise groupctx.Refused(WHICH_REPO + ", ".join(s.shown for s in record.sources if s.type == "github_repo") + ".")
    return repos[0]


def _failed(e: GitHubError) -> str:
    logger.warning("github for a group: %s", e)
    if isinstance(e, NotInstalled):
        return NOT_INSTALLED
    return f"GitHub said {e.status}." if e.status else NO_ANSWER


def _run(do: Callable[[], str]) -> str:
    """A tool body, with every failure turned into a fixed sentence."""
    try:
        return do()
    except groupctx.Refused as e:
        return str(e)
    except GitHubError as e:
        return _failed(e)


def _wrap(repo: str, what: str, lines: list[str]) -> str:
    return untrusted(what, f"GitHub ({repo})", "\n".join(lines))


def _login(item: dict) -> str:
    return (item.get("user") or {}).get("login", "?")


@tool
def github_prs(repo: str = "", state: str = "open") -> str:
    """Pull requests in this group's repositories, newest activity first; `repo` (owner/name or
    its label) narrows to one. `state` is open, closed or all. Numbers, authors, head commits
    and dates are facts; titles and branches come marked as untrusted text."""
    def do():
        record = groupctx.require()
        client = _client(record)
        out = []
        for name in _repos(record, repo):
            pulls = client.pulls(name, state)
            if not pulls:
                out.append(f"{name}: no {state} pull requests.")
                continue
            facts, texts = [], []
            for pr in pulls:
                flags = [f for f, on in (("draft", pr.get("draft")), ("merged", pr.get("merged_at")),
                                         ("closed", pr.get("state") == "closed" and not pr.get("merged_at"))) if on]
                facts.append(f"#{pr['number']} by {_login(pr)}{' [' + ', '.join(flags) + ']' if flags else ''}, "
                             f"head {str((pr.get('head') or {}).get('sha', ''))[:7]}, updated {pr.get('updated_at', '?')}")
                texts.append(f"#{pr['number']}: {gh.title(pr)} (branch {(pr.get('head') or {}).get('ref', '?')})")
            out.append(f"{name}, {state}:\n" + "\n".join(facts) + "\n\n" + _wrap(name, "pull request text", texts))
        return "\n\n".join(out)
    return _run(do)


@tool
def github_pr(number: int, repo: str = "") -> str:
    """One pull request: state, author, head commit, whether GitHub can merge it, reviews, CI,
    and its description and latest comments, which come marked as untrusted text."""
    def do():
        record = groupctx.require()
        client = _client(record)
        name = _one_repo(record, repo)
        pr = client.pull(name, number)
        sha = pr["head"]["sha"]
        checks = client.checks(name, sha)
        reviews = client.review_state(name, number)
        state = "merged" if pr.get("merged") else pr.get("state", "?")
        if pr.get("draft"):
            state += ", draft"
        lines = [f"{name}#{pr['number']}: {state}", f"Author: {_login(pr)}", f"Head: {sha[:7]}",
                 f"Mergeable: {_mergeable(pr)}", f"Reviews: {reviews}", f"CI: {checks.summary()}"]
        texts = [f"Title: {gh.title(pr)}", f"Branch: {pr['head'].get('ref', '?')}", "",
                 gh.text(pr.get("body")) or "(no description)"]
        if checks.failed:
            texts.append("Failing checks: " + ", ".join(checks.failed[:10]))
        for c in client.pull_comments(name, number):
            texts.append(f"\n{_login(c)} ({str(c.get('created_at', ''))[:10]}): {gh.text(c.get('body'), 600)}")
        return "\n".join(lines) + "\n\n" + _wrap(name, "pull request text", texts)
    return _run(do)


def _mergeable(pr: dict) -> str:
    if pr.get("mergeable") is None:
        return "not known yet"
    if not pr["mergeable"]:
        return "no (conflicts)"
    return {"clean": "yes", "unstable": "yes, with non-required checks failing",  # pyright: ignore[reportReturnType]
            "blocked": "blocked (required reviews or checks)", "behind": "behind its base",
            "has_hooks": "yes"}.get(pr.get("mergeable_state", ""), pr.get("mergeable_state", "?"))


@tool
def github_issues(repo: str = "", state: str = "open", limit: int = 20) -> str:
    """Issues in this group's repositories, newest activity first; `repo` narrows to one; `state`
    is open, closed or all. Pull requests are not issues: `github_prs` has those. Titles and
    labels come marked as untrusted text."""
    def do():
        record = groupctx.require()
        client = _client(record)
        out = []
        for name in _repos(record, repo):
            issues = client.issues(name, state, max(1, min(limit, 50)))
            if not issues:
                out.append(f"{name}: no {state} issues.")
                continue
            facts, texts = [], []
            for issue in issues:
                facts.append(f"#{issue['number']} ({issue.get('state', '?')}, updated "
                             f"{str(issue.get('updated_at', ''))[:10]}) by {_login(issue)}")
                labels = ", ".join(str(lb.get("name", "")) for lb in issue.get("labels") or [])
                texts.append(f"#{issue['number']}: {gh.title(issue)}" + (f" [{labels}]" if labels else ""))
            out.append(f"{name}, {state}:\n" + "\n".join(facts) + "\n\n" + _wrap(name, "issue text", texts))
        return "\n\n".join(out)
    return _run(do)


@tool
def github_issue(number: int, repo: str = "") -> str:
    """One issue in full: state, author, labels, its description and latest comments, which
    come marked as untrusted text."""
    def do():
        record = groupctx.require()
        client = _client(record)
        name = _one_repo(record, repo)
        issue = client.issue(name, number)
        labels = ", ".join(str(lb.get("name", "")) for lb in issue.get("labels") or []) or "none"
        lines = [f"{name}#{issue['number']}: {issue.get('state', '?')}", f"Opened by {_login(issue)}",
                 f"Labels: {labels}"]
        texts = [f"Title: {gh.title(issue)}", "", gh.text(issue.get("body")) or "(no description)"]
        for c in client.issue_comments(name, number):
            texts.append(f"\n{_login(c)} ({str(c.get('created_at', ''))[:10]}): {gh.text(c.get('body'), 600)}")
        return "\n".join(lines) + "\n\n" + _wrap(name, "issue text", texts)
    return _run(do)


@tool
def github_commits(repo: str = "", branch: str = "", since: str = "", limit: int = 20) -> str:
    """Recent commits in this group's repositories; `repo` narrows to one, `branch` to a branch,
    `since` (YYYY-MM-DD) to a date. Shas, dates and authors are facts; messages come marked as
    untrusted text."""
    def do():
        record = groupctx.require()
        client = _client(record)
        out = []
        for name in _repos(record, repo):
            commits = client.commits(name, branch, since, max(1, min(limit, 50)))
            if not commits:
                out.append(f"{name}: no commits found.")
                continue
            facts, texts = [], []
            for c in commits:
                meta = (c.get("commit") or {}).get("author") or {}
                facts.append(f"{str(c.get('sha', ''))[:7]} {str(meta.get('date', ''))[:10]} ({meta.get('name', '?')})")
                texts.append(f"{str(c.get('sha', ''))[:7]}: {gh.text((c.get('commit') or {}).get('message'), 200).splitlines()[0] if (c.get('commit') or {}).get('message') else ''}")
            out.append(f"{name}:\n" + "\n".join(facts) + "\n\n" + _wrap(name, "commit messages", texts))
        return "\n\n".join(out)
    return _run(do)


@tool
def github_file(path: str, repo: str = "", offset: int = 1, limit: int = 0, ref: str = "") -> str:
    """Read a file from one of this group's repositories, or list a directory. `offset` is the
    first line (negative counts from the end: -80 is the last 80 lines), `limit` how many (0 for
    all up to the ceiling); the last line of a cut file says where to continue. `ref` is a branch
    or commit. The contents come marked as untrusted text."""
    def do():
        record = groupctx.require()
        client = _client(record)
        name = _one_repo(record, repo)
        data = client.contents(name, path, ref)
        if isinstance(data, list):
            entries = [f"{'dir' if c.get('type') == 'dir' else 'file'}: {c.get('path', '')}" for c in data]
            return _wrap(name, f"listing of {path}", entries)
        if data.get("text") is None:
            return f"{name}/{path} is not text."
        return _wrap(name, f"file {path}", [gh.page(data["text"], offset, limit)])
    return _run(do)


@tool
def github_search(query: str, repo: str = "") -> str:
    """Search code and issues in this group's repositories; `repo` narrows to one. Paths are
    facts; issue titles come marked as untrusted text."""
    def do():
        record = groupctx.require()
        client = _client(record)
        out = []
        for name in _repos(record, repo):
            code = [f"[code] {name}/{item.get('path', '')}" for item in client.search_code(name, query)]
            issues = client.search_issues(name, query)
            facts = code + [f"[issue #{i.get('number')} {i.get('state', '?')}] {name}" for i in issues]
            if not facts:
                out.append(f"{name}: nothing for {query!r}.")
                continue
            texts = [f"#{i.get('number')}: {gh.title(i)}" for i in issues]
            out.append("\n".join(facts) + ("\n\n" + _wrap(name, "issue text", texts) if texts else ""))
        return "\n\n".join(out)
    return _run(do)


@tool(side_effect=True)
def propose_merge(number: int, repo: str = "", method: str = "") -> str:
    """Ask the owner to merge a pull request. Merges nothing: the owner sees a card in their
    own chat with the PR, its head commit and CI, and Merge and Cancel; only their Merge merges,
    and only that commit. `method` is squash, merge or rebase; empty takes the first the
    repository allows. Say the owner has been asked, never that it was merged."""
    def do():
        record = groupctx.require()
        client = _client(record)
        if _propose is None:
            raise groupctx.Refused(NO_CARD)
        name = _one_repo(record, repo)
        pr = client.pull(name, number)
        if pr.get("merged"):
            return f"{name}#{number} is already merged."
        if pr.get("state") != "open":
            return f"{name}#{number} is closed."
        if pr.get("draft"):
            return f"{name}#{number} is a draft; it must be marked ready first."
        allowed = client.merge_methods(name)
        chosen = method.strip().lower()
        if not chosen:
            if not allowed:
                return f"{name} allows no merge method."
            chosen = allowed[0]
        elif chosen not in gh.METHODS:
            return "The method is squash, merge or rebase."
        elif chosen not in allowed:
            return f"{name} does not allow {chosen} merges; it allows {', '.join(allowed)}."
        sha = pr["head"]["sha"]
        ci = client.checks(name, sha).summary()
        _propose(github_cards.MERGE_KIND,
                 github_cards.merge_payload(record.id, record.name, name, number, gh.title(pr), sha, ci, chosen))
        return (f"The owner has been asked to merge {name}#{number} at {sha[:7]} ({chosen}, CI {ci}). Nothing "
                "is merged until they choose Merge; say they were asked, not that it was merged.")
    return _run(do)


@tool(side_effect=True)
def propose_issue(title: str, body: str = "", repo: str = "") -> str:
    """Ask the owner to file an issue in one of this group's repositories. Files nothing: the
    owner sees the title and body on a card with File, Edit and Cancel; only their File files it.
    Say the owner has been asked, never that it was filed."""
    def do():
        record = groupctx.require()
        _client(record)  # a token must exist for the owner's File to work
        if _propose is None:
            raise groupctx.Refused(NO_CARD)
        name = _one_repo(record, repo)
        if not title.strip():
            return "An issue needs a title."
        _propose(github_cards.ISSUE_KIND,
                 github_cards.issue_payload(record.id, record.name, name, title.strip(), body.strip()))
        return (f"The owner has been asked to file \"{title.strip()}\" in {name}. Nothing is filed until they "
                "choose File; say they were asked, not that it was filed.")
    return _run(do)


def _line_comments(comments: list) -> list[dict]:
    """Line comments as GitHub takes them, each {path, line, body}; anything
    else is refused with a fixed sentence."""
    out = []
    for c in comments or []:
        if not isinstance(c, dict):
            raise groupctx.Refused(BAD_COMMENT)
        path, body, line = str(c.get("path") or "").strip(), str(c.get("body") or "").strip(), c.get("line")
        if not path or not body or isinstance(line, bool):
            raise groupctx.Refused(BAD_COMMENT)
        try:
            line = int(line)  # pyright: ignore[reportArgumentType]
        except (TypeError, ValueError):
            raise groupctx.Refused(BAD_COMMENT) from None
        if line < 1:
            raise groupctx.Refused(BAD_COMMENT)
        out.append({"path": path, "line": line, "body": body})
    return out


@tool(side_effect=True)
def propose_review(number: int, body: str = "", event: str = "COMMENT", repo: str = "",
                   comments: list[dict] = ()) -> str:  # pyright: ignore[reportArgumentType]  # the annotation is the tool schema
    """Ask the owner to post a review on a pull request. Posts nothing: the owner sees a card
    in their own chat with the PR, the event and the whole body, and Post, Edit and Cancel;
    only their Post posts it, on the head commit the card showed. `event` is COMMENT, APPROVE
    or REQUEST_CHANGES; COMMENT and REQUEST_CHANGES need a body. `comments` are optional
    line comments, each {"path": "file/in/repo.py", "line": 12, "body": "..."}, on lines of
    the PR's diff. Say the owner has been asked, never that it was posted."""
    def do():
        record = groupctx.require()
        client = _client(record)
        if _propose is None:
            raise groupctx.Refused(NO_CARD)
        name = _one_repo(record, repo)
        chosen = (event or "COMMENT").strip().upper().replace(" ", "_")
        if chosen not in gh.REVIEW_EVENTS:
            return "The event is COMMENT, APPROVE or REQUEST_CHANGES."
        text = body.strip()
        if not text and chosen != "APPROVE":
            return f"A review with {chosen} needs a body."
        lines = _line_comments(comments)
        pr = client.pull(name, number)
        sha = pr["head"]["sha"]
        _propose(github_cards.REVIEW_KIND,
                 github_cards.review_payload(record.id, record.name, name, number, gh.title(pr), sha, chosen,
                                             text, lines))
        extra = f" with {len(lines)} line comment{'s' if len(lines) != 1 else ''}" if lines else ""
        return (f"The owner has been asked to post a review ({chosen}) on {name}#{number} at {sha[:7]}{extra}. "
                "Nothing is posted until they choose Post; say they were asked, not that it was posted.")
    return _run(do)


github_tools = [github_prs, github_pr, github_issues, github_issue, github_commits, github_file, github_search,
                propose_merge, propose_issue, propose_review]
