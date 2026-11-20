"""The reader's web tools: the address guard, page text, search, and that the
manager never holds them."""

import ipaddress
import socket

import httpx
import pytest

from fulla.config import load_config
from fulla.directives import DirectiveStore
from fulla.tools import web
from fulla.worker import build_agents

PUBLIC = {"example.com": "93.184.215.14", "other.org": "151.101.1.1", "sneaky.example": "10.0.0.5",
          "v6.example": "::1", "mapped.example": "::ffff:127.0.0.1", "meta.example": "169.254.169.254"}


def fake_resolve(host, port, proto=0):
    try:
        addr = PUBLIC.get(host) or str(ipaddress.ip_address(host))
    except ValueError:
        raise socket.gaierror("unknown host") from None
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET
    return [(family, socket.SOCK_STREAM, proto, "", (addr, port))]


@pytest.fixture
def net(monkeypatch):
    """Requests go to `routes` (url -> response); nothing leaves the machine."""
    routes: dict[str, httpx.Response] = {}
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = str(request.url.copy_with(query=None))
        if url not in routes:
            return httpx.Response(404)
        return routes[url]

    monkeypatch.setattr(web, "_resolve", fake_resolve)
    monkeypatch.setattr(web, "_transport", httpx.MockTransport(handler))
    web.init(brave_api_key="")
    return routes, seen


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/x", "http://localhost:8080/", "http://fulla.internal/",
    "http://norns.flycast/", "http://127.0.0.1/", "http://10.0.0.1/", "http://[::1]/",
    "http://sneaky.example/", "http://v6.example/", "http://mapped.example/",
    "http://meta.example/latest/meta-data", "http://192.168.1.1/", "http://nowhere.invalid/",
])
def test_guard_refuses_private_and_odd_urls(net, url):
    assert web.check_url(url) is not None


def test_guard_allows_public_http_and_https(net):
    assert web.check_url("https://example.com/about") is None
    assert web.check_url("http://other.org:8080/x") is None


def test_fetch_reads_main_text_and_marks_it_untrusted(net):
    routes, _ = net
    routes["https://example.com/"] = httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text="""
        <html><head><title>Robin's page</title><style>p{color:red}</style></head><body>
        <nav><a href="/">Home</a> <a href="/blog">Blog</a></nav>
        <header>Site header</header>
        <main><h1>About</h1><p>I build durable agents.</p><p>I live in Toronto &amp; bake bread.
        """ + "More words here. " * 20 + """</p><script>alert('x')</script></main>
        <footer>Copyright</footer></body></html>""")
    out = web.fetch_url.handler("https://example.com/")
    assert out.startswith("[Untrusted web content from https://example.com/.")
    assert out.rstrip().endswith("[End of untrusted web content.]")
    assert "Robin's page" in out and "About\n" in out
    assert "I build durable agents.\n" in out and "Toronto & bake bread" in out
    for chrome in ("Home", "Blog", "Site header", "Copyright", "alert", "color:red"):
        assert chrome not in out


def test_fetch_follows_redirects_only_to_public_addresses(net):
    routes, seen = net
    routes["https://example.com/a"] = httpx.Response(301, headers={"location": "https://other.org/b"})
    routes["https://other.org/b"] = httpx.Response(302, headers={"location": "http://sneaky.example/c"})
    out = web.fetch_url.handler("https://example.com/a")
    assert out.startswith("Not fetched: sneaky.example resolves to 10.0.0.5")
    assert [str(r.url) for r in seen] == ["https://example.com/a", "https://other.org/b"]


def test_fetch_stops_after_too_many_redirects(net):
    routes, _ = net
    routes["https://example.com/loop"] = httpx.Response(302, headers={"location": "/loop"})
    assert "more than 5 redirects" in web.fetch_url.handler("https://example.com/loop")


def test_fetch_refuses_binary_and_reports_errors(net):
    routes, _ = net
    routes["https://example.com/x.pdf"] = httpx.Response(200, headers={"content-type": "application/pdf"},
                                                         content=b"%PDF")
    assert "application/pdf, not a page" in web.fetch_url.handler("https://example.com/x.pdf")
    assert "answered 404" in web.fetch_url.handler("https://example.com/missing")


def test_fetch_caps_size_and_length(net):
    routes, _ = net
    routes["https://example.com/big.txt"] = httpx.Response(
        200, headers={"content-type": "text/plain"}, content=b"a" * (3 * 1024 * 1024))
    routes["https://example.com/data.json"] = httpx.Response(
        200, headers={"content-type": "application/json"}, content=b'{"name": "Fulla"}')
    out = web.fetch_url.handler("https://example.com/big.txt")
    assert "[truncated]" in out and len(out) < web.MAX_CHARS + 300
    assert '"name": "Fulla"' in web.fetch_url.handler("https://example.com/data.json")


def test_search_is_off_without_a_key(net):
    out = web.web_search.handler("norns durable agents")
    assert "BRAVE_API_KEY" in out


def test_search_formats_web_and_news_results(net):
    routes, seen = net
    web.init(brave_api_key="secret")
    routes[web.BRAVE_WEB] = httpx.Response(200, json={"web": {"results": [
        {"title": "Norns <strong>runtime</strong>", "url": "https://norns.dev/",
         "description": "Durable &amp; boring", "age": "2 days ago"}]}})
    routes[web.BRAVE_NEWS] = httpx.Response(200, json={"results": [
        {"title": "Toronto news", "url": "https://news.example/1", "description": "Snow"}]})
    out = web.web_search.handler("norns")
    assert "1. Norns runtime (2 days ago)\n   https://norns.dev/\n   Durable & boring" in out
    assert out.startswith("[Untrusted web content from a search for 'norns'.")
    assert seen[-1].headers["x-subscription-token"] == "secret"
    assert seen[-1].url.params["q"] == "norns" and "freshness" not in seen[-1].url.params
    assert "Toronto news" in web.web_search.handler("toronto", fresh=True)
    assert seen[-1].url.params["freshness"] == "pw"


def test_only_the_reader_holds_the_web(monkeypatch, tmp_path):
    monkeypatch.setenv("FULLA_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FULLA_TELEGRAM_USER_ID", "7")
    monkeypatch.setenv("NORNS_API_KEY", "k")
    agents = {a.name: a for a in build_agents(load_config(), DirectiveStore(tmp_path / "d.json"))}
    reader, manager = agents["fulla-reader"], agents["fulla"]
    assert set(reader.allowed_tools) == {"now", "web_search", "fetch_url"}
    assert not any(t.side_effect for t in reader.tools)
    assert reader.subagents == {"mode": "disabled"}
    for name, agent in agents.items():
        if name != "fulla-reader":
            assert not {"web_search", "fetch_url"} & set(agent.allowed_tools)
    assert manager.subagents["mode"] == "allowlist"
    assert manager.subagents["allowed_agents"] == ["fulla-reader"]
    assert "fulla-reader" in manager.system_prompt
    for name in ("fulla-reminder", "fulla-mac-task"):
        assert agents[name].subagents == {"mode": "disabled"}
