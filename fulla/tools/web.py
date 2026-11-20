"""The reader's tools: search the web and read a page. Served only to the
`fulla-reader` agent, never to the manager.

Whatever comes back from the web was written by strangers, and the manager
holds hands (tmux_send, iMessage). So the run that reads pages has nothing
side-effecting to call, and every result is wrapped in an untrusted marker.
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from fulla import untrusted as marker
from fulla.tooldef import tool

READER_AGENT = "fulla-reader"
BRAVE_WEB = "https://api.search.brave.com/res/v1/web/search"
BRAVE_NEWS = "https://api.search.brave.com/res/v1/news/search"
RESULTS = 8
TIMEOUT = 10.0
MAX_BYTES = 2 * 1024 * 1024
MAX_CHARS = 20_000
MAX_REDIRECTS = 5
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "application/json")
BLOCKED_SUFFIXES = (".internal", ".flycast", "localhost")
USER_AGENT = "Fulla/0.1 (a personal assistant reading one page for its owner)"

_api_key = ""
# Seams for tests: how a host is resolved, and how requests leave.
_resolve = socket.getaddrinfo
_transport: httpx.BaseTransport | None = None


def init(*, brave_api_key: str):
    global _api_key
    _api_key = brave_api_key


def untrusted(source: str, text: str) -> str:
    return marker.untrusted("web content", source, text)


# ------------------------------------------------------------------- search


def _strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


@tool
def web_search(query: str, fresh: bool = False) -> str:
    """Search the web. `fresh` searches news from the past week instead, for anything
    recent. Returns titles, URLs and snippets; `fetch_url` reads a page in full."""
    if not _api_key:
        return ("Web search is off: no Brave Search API key. Set BRAVE_API_KEY "
                "(https://brave.com/search/api/) to turn it on.")
    params = {"q": query, "count": RESULTS}
    if fresh:
        params["freshness"] = "pw"
    try:
        with httpx.Client(timeout=TIMEOUT, transport=_transport) as client:
            resp = client.get(BRAVE_NEWS if fresh else BRAVE_WEB, params=params, headers={
                "X-Subscription-Token": _api_key, "Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return f"Search failed: {e}"
    results = data.get("results") if fresh else (data.get("web") or {}).get("results")
    if not results:
        return f"No results for {query!r}."
    lines = []
    for i, r in enumerate(results[:RESULTS], 1):
        age = f" ({r['age']})" if r.get("age") else ""
        lines.append(f"{i}. {_strip_tags(r.get('title', ''))}{age}\n   {r.get('url', '')}\n"
                     f"   {_strip_tags(r.get('description', ''))}")
    return untrusted(f"a search for {query!r}", "\n".join(lines))


# --------------------------------------------------------------- the guard


def check_url(url: str) -> str | None:
    """Why this URL may not be fetched, or None if it may.

    Only http and https, and only to addresses on the public internet: never
    Fly's private network, the metadata service, or anything on this machine.
    The host is resolved here and every address it resolves to must be
    global. Redirects are re-checked hop by hop. One gap is left: httpx
    resolves the host again to connect, so a DNS server that answers with a
    public address here and a private one a moment later (rebinding) gets
    past the check. Connecting to the checked address itself would close it."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return f"only http and https URLs, not {parts.scheme or 'none'!r}"
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        return "no host in the URL"
    if host.endswith(BLOCKED_SUFFIXES):
        return f"{host} is a private name"
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        infos = _resolve(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, ValueError, UnicodeError) as e:
        return f"cannot resolve {host}: {e}"
    if not infos:
        return f"cannot resolve {host}"
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%")[0])  # pyright: ignore[reportAttributeAccessIssue]
        mapped = getattr(addr, "ipv4_mapped", None)
        if not addr.is_global or (mapped is not None and not mapped.is_global):
            return f"{host} resolves to {addr}, which is not a public address"
    return None


# ----------------------------------------------------------------- reading

SKIP = {"script", "style", "nav", "header", "footer", "aside", "form", "svg", "noscript",
        "template", "iframe", "button", "select"}
PREFER = {"main", "article"}
BLOCKS = {"p", "div", "section", "br", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3", "h4",
          "h5", "h6", "blockquote", "pre", "dd", "dt", "dl", "hr", "figure", "figcaption",
          "main", "article", "title", "td", "th"}


class _Text(HTMLParser):
    """Visible text, blocks on their own lines. Text inside <main> or <article>
    is also kept apart, since that is the page minus its chrome."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.all: list[str] = []
        self.main: list[str] = []
        self.title: list[str] = []
        self.skip = 0
        self.prefer = 0
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self.skip += 1
        elif tag in PREFER:
            self.prefer += 1
        if tag == "title":
            self.in_title = True
        if tag in BLOCKS:
            self._emit("\n")

    def handle_endtag(self, tag):
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in PREFER:
            self.prefer = max(0, self.prefer - 1)
        if tag == "title":
            self.in_title = False
        if tag in BLOCKS:
            self._emit("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
            return
        if not self.skip:
            self._emit(re.sub(r"\s+", " ", data))

    def _emit(self, text: str):
        self.all.append(text)
        if self.prefer:
            self.main.append(text)


def _tidy(parts: list[str]) -> str:
    lines = (line.strip() for line in "".join(parts).split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(markup: str) -> str:
    """The readable text of a page: its title, then <main>/<article> if it
    has a substantial one, otherwise the whole body minus navigation,
    scripts and forms."""
    parser = _Text()
    parser.feed(markup)
    parser.close()
    main = _tidy(parser.main)
    body = main if len(main) >= 200 else _tidy(parser.all)
    title = " ".join("".join(parser.title).split())
    return f"{title}\n\n{body}".strip() if title else body


def _read_capped(resp: httpx.Response) -> tuple[bytes, bool]:
    chunks, size = [], 0
    for chunk in resp.iter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= MAX_BYTES:
            return b"".join(chunks)[:MAX_BYTES], True
    return b"".join(chunks), False


def _render(body: bytes, ctype: str, encoding: str | None) -> str:
    text = body.decode(encoding or "utf-8", errors="replace")
    if "html" in ctype:
        return html_to_text(text)
    if "json" in ctype:
        try:
            return json.dumps(json.loads(text), indent=1, ensure_ascii=False)
        except ValueError:
            return text
    return text


@tool
def fetch_url(url: str) -> str:
    """Read one web page (HTML, plain text or JSON) and return its text, up to about
    20,000 characters. Only public http(s) addresses."""
    current = url.strip()
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False, transport=_transport,
                          headers={"User-Agent": USER_AGENT}) as client:
            for _ in range(MAX_REDIRECTS + 1):
                refused = check_url(current)
                if refused:
                    return f"Not fetched: {refused}."
                with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            return f"{current} redirected nowhere."
                        current = urljoin(current, location)
                        continue
                    if resp.status_code >= 400:
                        return f"{current} answered {resp.status_code}."
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype and not ctype.startswith(TEXT_TYPES):
                        return f"{current} is {ctype}, not a page I can read."
                    body, capped = _read_capped(resp)
                    text = _render(body, ctype or "text/html", resp.charset_encoding)
                    break
            else:
                return f"Not fetched: more than {MAX_REDIRECTS} redirects."
    except httpx.HTTPError as e:
        return f"Could not fetch {current}: {e}"
    if capped or len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n[truncated]"
    return untrusted(current, text or "(no readable text)")
