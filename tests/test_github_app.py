"""The GitHub App in place of a stored token: a fake api.github.com that
checks the app's JWT against its public key and mints installation tokens.
No network."""

import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from fulla import groupctx, runctx
from fulla.groups import GroupStore
from fulla.people import PeopleStore
from fulla.tools import group_github as tools
from norns_app.github import GitHub, GitHubError
from norns_app.github_app import GitHubApp, NotInstalled, load_key

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                        serialization.NoEncryption()).decode()
ALPHA = "signal:group:aaa"


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class FakeGitHub:
    """nornscode is installation 7, with fulla and norns in it; nothing else is."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.minted = []  # the repositories each token was asked for
        self.repos = {"fulla", "norns"}

    def _app(self, request):
        """The claims of the JWT a call to the app's own endpoints carries, verified."""
        signing, _, sig = request.headers["Authorization"].removeprefix("Bearer ").rpartition(".")
        KEY.public_key().verify(_unb64(sig), signing.encode(), padding.PKCS1v15(), hashes.SHA256())
        header, claims = (json.loads(_unb64(p)) for p in signing.split("."))
        assert header == {"alg": "RS256", "typ": "JWT"} and claims["iss"] == "4242"
        assert claims["iat"] <= self.clock() < claims["exp"] <= self.clock() + 600
        return claims

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        if path.endswith("/installation"):
            self._app(request)
            owner = path.split("/")[2].lower()
            return httpx.Response(200, json={"id": 7}) if owner == "nornscode" else httpx.Response(404, json={})
        if (method, path) == ("POST", "/app/installations/7/access_tokens"):
            self._app(request)
            names = [n.lower() for n in json.loads(request.content)["repositories"]]
            if not set(names) <= self.repos:
                return httpx.Response(422, json={"message": "not in the installation"})
            self.minted.append(names)
            return httpx.Response(201, json={
                "token": f"ghs_{names[0]}_{len(self.minted)}",
                "expires_at": "2026-11-20T13:00:00Z" if len(self.minted) == 1 else "2026-11-20T15:00:00Z"})
        token = request.headers["Authorization"].removeprefix("Bearer ")
        if path.startswith("/search/"):
            return httpx.Response(200, json={"items": [], "seen": token})
        repo = path.split("/")[3]
        assert token.startswith(f"ghs_{repo}_"), "a token minted for another repository"
        if path.endswith("/pulls"):
            return httpx.Response(200, json=[])
        if method == "POST" and path.endswith("/issues"):
            return httpx.Response(201, json={"number": 77})
        return httpx.Response(404, json={"message": "Not Found"})


NOON = 1795176000.0  # 2026-11-20T12:00:00Z


@pytest.fixture
def world(tmp_path):
    now = [NOON]
    fake = FakeGitHub(lambda: now[0])
    transport = httpx.MockTransport(fake.handler)
    app = GitHubApp("4242", PEM, transport=transport, clock=lambda: now[0])
    people = PeopleStore(tmp_path / "people.json")
    people.remember_group(ALPHA, "Alpha")
    people.allow_group(ALPHA)
    store = GroupStore(tmp_path / "groups", secret="s", github_app=app)
    [a] = store.sync(people)
    store.set_tools(a.id, ["github"])
    store.add_source(a.id, "github_repo", "nornscode/fulla", "Fulla")
    groupctx.init(store=store)
    tools.init(store=store, propose=None, transport=transport)
    runctx.origins_from({9: ALPHA}.get)
    token = runctx.set_current_run(9)
    yield fake, app, store, a, now, transport
    runctx.reset_current_run(token)
    runctx.origins_from(None)
    groupctx.init(store=None)
    tools.init(store=None)


def test_a_token_is_minted_for_one_repository_kept_and_made_again_before_it_lapses(world):
    fake, app, _store, _a, now, _transport = world
    assert app.token("nornscode/fulla") == "ghs_fulla_1" == app.token("NornsCode/Fulla")
    assert fake.minted == [["fulla"]]
    assert app.token("nornscode/norns") == "ghs_norns_2"  # its own token, the installation looked up once
    assert [p for _, p in fake.calls].count("/repos/nornscode/fulla/installation") == 1
    assert "/repos/nornscode/norns/installation" not in [p for _, p in fake.calls]
    now[0] = NOON + 3600 - 200  # the first lapses at 13:00; five minutes before, it is not used
    assert app.token("nornscode/fulla") == "ghs_fulla_3"


def test_a_repository_the_app_does_not_cover_gets_no_token(world):
    fake, app, _store, _a, _now, _transport = world
    with pytest.raises(NotInstalled):
        app.token("someone/else")  # no installation on that owner
    with pytest.raises(NotInstalled):
        app.token("nornscode/private")  # the owner's, but not in the installation
    with pytest.raises(GitHubError):
        app.token("not a repo")
    assert fake.minted == []


def test_a_group_gets_a_token_only_for_its_own_sources_and_needs_none_stored(world):
    fake, _app, store, a, _now, transport = world
    assert store.credential(a.id, "github") is None
    assert "credentials set: github (the app)" in store.render()
    mint = store.github_token(a.id)
    with pytest.raises(GitHubError, match="not among the group's sources"):
        mint("nornscode/norns")  # the app covers it; the group does not hold it
    assert fake.calls == []
    assert tools.github_prs.handler() != tools.NO_TOKEN
    assert ("GET", "/repos/nornscode/fulla/pulls") in fake.calls and fake.minted == [["fulla"]]
    store.set_credential(a.id, "github", "ghp_old")  # a token stored before the app is left unused
    assert callable(store.github_token(a.id))
    GitHub(store.github_token(a.id), transport=transport).search_code("nornscode/fulla", "x")  # a search names its repo
    assert fake.minted == [["fulla"]]


def test_a_card_acts_with_the_apps_token(world):
    from fulla import github_cards
    from fulla.confirm import Confirmation
    _fake, _app, store, a, _now, transport = world
    issue = next(k for k in github_cards.kinds(store, transport=transport) if k.name == github_cards.ISSUE_KIND)
    payload = github_cards.issue_payload(a.id, "Alpha", "nornscode/fulla", "A bug", "")
    conf = Confirmation(id="c", kind=issue.name, payload=payload, digest="d", address="x", message_id="1")
    assert issue.approve(conf).note == "Filed nornscode/fulla#77."


def test_a_repository_without_the_app_is_a_fixed_sentence(world):
    _fake, _app, store, a, _now, _transport = world
    store.add_source(a.id, "github_repo", "someone/else", "Else")
    assert tools.github_prs.handler("someone/else") == tools.NOT_INSTALLED


def test_the_key_loads_from_a_pem_one_line_or_base64_and_nothing_else():
    for form in (PEM, PEM.replace("\n", "\\n"), base64.b64encode(PEM.encode()).decode()):
        assert load_key(form).key_size == 2048
    with pytest.raises(ValueError, match="not a PEM private key"):
        load_key("ghp_a_personal_token")
