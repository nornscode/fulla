"""A GitHub App's installation tokens, minted as they are needed.

A personal token expires and has to be pasted again. An app has an id and a
private key that do not: with them it signs a short JWT, finds the
installation that covers a repository, and asks for a token for that one
repository, good for an hour. Tokens are kept in memory until shortly
before they lapse and never written anywhere.

`GitHubApp.token(repo)` is what a `GitHub` client takes in place of a
token string. Like the client it checks no allowlist; the caller has
already decided which repository the call may touch.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Callable
from datetime import datetime

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from norns_app.github import API, TIMEOUT, GitHubError, _message, valid_repo

JWT_SECONDS = 540  # GitHub allows ten minutes
EARLY = 300  # a token is dropped this long before it lapses
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
           "User-Agent": "norns-app"}


class NotInstalled(GitHubError):
    """The app has no installation that covers the repository."""

    def __init__(self, repo: str):
        super().__init__(f"the GitHub app is not installed on {repo}", 404)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def load_key(pem: str) -> rsa.RSAPrivateKey:
    """The app's private key from its PEM, as a file's text, with the
    newlines written as \\n (one line in an env file), or base64 of either."""
    pem = pem.strip().replace("\\n", "\n")
    if "BEGIN" not in pem:
        try:
            pem = base64.b64decode(pem, validate=True).decode()
        except ValueError:
            raise ValueError("the GitHub app key is not a PEM private key") from None
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError) as e:
        raise ValueError("the GitHub app key is not a PEM private key") from e
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("the GitHub app key is not an RSA private key")  # noqa: TRY004 (a bad setting, like the rest)
    return key


class GitHubApp:
    def __init__(self, app_id: str, private_key: str, *, transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.time):
        self.app_id = str(app_id).strip()
        self._key = load_key(private_key)
        self._transport = transport
        self._clock = clock
        self._lock = threading.Lock()
        self._installations: dict[str, int] = {}  # by owner, lowercased
        self._tokens: dict[str, tuple[str, float]] = {}  # by repo, lowercased: token, when it lapses

    def _jwt(self) -> str:
        now = int(self._clock())
        # Issued a minute ago, so a clock a little ahead of GitHub's is not refused.
        claims = {"iat": now - 60, "exp": now + JWT_SECONDS, "iss": self.app_id}
        signing = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + "." + _b64(json.dumps(claims).encode())
        return signing + "." + _b64(self._key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256()))

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            with httpx.Client(timeout=TIMEOUT, transport=self._transport, base_url=API,
                              follow_redirects=False) as client:
                return client.request(method, path, headers={**HEADERS, "Authorization": f"Bearer {self._jwt()}"},
                                      **kw)
        except httpx.HTTPError as e:
            raise GitHubError(f"could not reach GitHub: {e}") from e

    def _installation(self, repo: str) -> int:
        owner = repo.split("/")[0].lower()
        if owner not in self._installations:
            resp = self._request("GET", f"/repos/{repo}/installation")
            if resp.status_code == 404:
                raise NotInstalled(repo)
            if resp.status_code != 200:
                raise GitHubError(f"GitHub said {resp.status_code} finding the app's installation: "
                                  f"{_message(resp)}", resp.status_code)
            self._installations[owner] = int(resp.json()["id"])
        return self._installations[owner]

    def token(self, repo: str) -> str:
        """An installation token for `repo` and nothing else the app is
        installed on. NotInstalled when the app does not cover it."""
        if not valid_repo(repo):
            raise GitHubError(f"not a repository: {repo!r}")
        key = repo.lower()
        with self._lock:
            held = self._tokens.get(key)
            if held and held[1] - EARLY > self._clock():
                return held[0]
            installation = self._installation(repo)
            resp = self._request("POST", f"/app/installations/{installation}/access_tokens",
                                 json={"repositories": [repo.split("/")[1]]})
            if resp.status_code in (404, 422):
                # Uninstalled, or installed on some of the owner's repositories and not this one.
                self._installations.pop(key.split("/")[0], None)
                raise NotInstalled(repo)
            if resp.status_code != 201:
                raise GitHubError(f"GitHub said {resp.status_code} minting a token: {_message(resp)}",
                                  resp.status_code)
            data = resp.json()
            try:
                lapses = datetime.fromisoformat(str(data["expires_at"])).timestamp()
            except (KeyError, ValueError):
                lapses = self._clock() + 3600
            self._tokens[key] = (data["token"], lapses)
            return data["token"]
